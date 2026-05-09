"""
orchestrator/loop.py
--------------------
Repo-based evaluation loop for iterative test and Sonar improvement.
"""

import json
import os
import re
import uuid
from pathlib import Path
from xml.etree import ElementTree as ET

from dotenv import load_dotenv

from config.llm_client import CodexClient
from llm_agent.analyzer import AnalyzerAgent
from llm_agent.coder import CoderAgent
from services.docker_runner import DockerRunner
from services.sonar_service import SonarService
from services.workspace_manager import WorkspaceManager

MAX_PATCH_ITERATIONS = 3
MAX_FILE_RETRIES = 3
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPO_PATH = PROJECT_ROOT / "cloned_repo" / "commons-csv"
DEFAULT_CONTEXT_SUBDIR = "src/main/java/org/apache/commons/csv"
SONAR_MAX_BUGS = 0
SONAR_MAX_CODE_SMELLS = 3
SONAR_MAX_COGNITIVE_COMPLEXITY = 25


def _format_detailed_results(results: list[dict]) -> list[dict]:
    """Convert raw loop output into a compact JSON shape."""
    detailed = []
    for result in results:
        iterations = []
        for attempt in result.get("attempt_history", []):
            junit_result = attempt.get("junit") or {}
            passed = junit_result.get("tests_passed", 0)
            run = junit_result.get("tests_run", 0)
            iterations.append(
                {
                    "iteration": attempt.get("iteration"),
                    "passrate": f"{passed}/{run}",
                    "tests_failed": junit_result.get("failures_with_values", []),
                    "test_comparison": attempt.get("test_comparison"),
                    "quality_ok": attempt.get("quality_ok"),
                    "quality_reason": attempt.get("quality_reason"),
                    "baseline_match": attempt.get("baseline_match"),
                    "sonar_metrics": attempt.get("sonar_metrics", {}),
                    "status": attempt.get("status"),
                    "artifact_file": attempt.get("artifact_file"),
                    "file_history": attempt.get("file_history", []),
                }
            )

        detailed.append(
            {
                "repo_path": result.get("repo_path"),
                "context_files": result.get("context_paths", []),
                "iterations": iterations,
                "final_status": result.get("status"),
                "final_iteration": result.get("attempts"),
            }
        )
    return detailed


def _resolve_repo_path(args: list[str] | None = None) -> str:
    """Use the first CLI arg as repo path when it exists, otherwise use the default repo."""
    if args:
        candidate = os.path.abspath(args[0])
        if os.path.isdir(candidate):
            return candidate
        print(f"[loop] ignoring non-path argument '{args[0]}' and using default repo")
    return str(DEFAULT_REPO_PATH)


def build_components(repo_path: str | None = None) -> "EvaluationLoop":
    """Build and wire all runtime components for the repo-based evaluation flow."""
    load_dotenv(override=True)

    llm = CodexClient(model="gpt-5.4-nano")
    coder = CoderAgent(llm)
    analyzer = AnalyzerAgent(llm)
    workspace = WorkspaceManager(base_dir=str(PROJECT_ROOT / "workspace"))
    workspace.set_source_repo(repo_path or str(DEFAULT_REPO_PATH))
    runner = DockerRunner()

    sonar = None
    token = os.getenv("SONAR_TOKEN")
    key = os.getenv("SONAR_PROJECT_KEY")
    host = os.getenv("SONAR_HOST_URL", "http://localhost:9000")
    if token and key:
        sonar = SonarService(token=token, project_key=key, docker_runner=runner, host=host)

    return EvaluationLoop(
        coder=coder,
        analyzer=analyzer,
        workspace=workspace,
        runner=runner,
        sonar=sonar,
        repo_path=workspace.source_repo,
        context_subdir=DEFAULT_CONTEXT_SUBDIR,
    )


def print_summary(results: list[dict]):
    """Print a compact summary for the executed repo experiment."""
    print("\n\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    for result in results:
        junit_result = result.get("junit_result") or {}
        passed = junit_result.get("tests_passed", 0)
        total = junit_result.get("tests_run", 0)
        print(
            f"[{result['status']:4}]  {Path(result['repo_path']).name:<35}  "
            f"{passed}/{total} tests  (iteration {result['attempts']})"
        )


def save_results(results: list[dict], output_path: str = "results.json"):
    """Persist a cleaned, compact JSON report for post-run analysis."""
    ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
    resolved_output_path = Path(output_path)
    if not resolved_output_path.is_absolute():
        resolved_output_path = PROJECT_ROOT / resolved_output_path

    def _clean(value):
        if isinstance(value, str):
            return ansi_escape.sub("", value)
        if isinstance(value, dict):
            return {key: _clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [_clean(item) for item in value]
        return value

    with open(resolved_output_path, "w", encoding="utf-8") as file_handle:
        clean_results = [{key: _clean(value) for key, value in result.items()} for result in results]
        json.dump(_format_detailed_results(clean_results), file_handle, indent=2, ensure_ascii=False)

    print(f"\nFull results saved to {resolved_output_path}")


def run_normal_mode(args: list[str] | None = None) -> list[dict]:
    """Run the repo-based workflow."""
    repo_path = _resolve_repo_path(args)
    loop = build_components(repo_path=repo_path)
    results = [loop.run()]
    print_summary(results)
    save_results(results)
    return results


def run_future_mode(args: list[str] | None = None):
    """Alias to the same repo-based workflow while the CLI still exposes two modes."""
    return run_normal_mode(args)


class EvaluationLoop:
    def __init__(self, coder, analyzer, workspace, runner, sonar=None, repo_path: str = "", context_subdir: str = ""):
        self.coder = coder
        self.analyzer = analyzer
        self.workspace = workspace
        self.runner = runner
        self.sonar = sonar
        self.repo_path = repo_path
        self.context_subdir = context_subdir
        self._run_nonce = uuid.uuid4().hex[:8]

    def run(self) -> dict:
        repo_name = Path(self.repo_path).name
        print(f"\n{'-' * 60}")
        print(f"  Repo: {repo_name}")
        print(f"  Source: {self.repo_path}")
        print(f"{'-' * 60}")

        self.workspace.bootstrap_iteration(0)
        context_paths = self.workspace.list_java_files(0, self.context_subdir)
        if not context_paths:
            raise RuntimeError(f"No Java context files found under {self.context_subdir}")

        print(f"  [context] using {len(context_paths)} Java files from {self.context_subdir}")

        attempt_history = []
        final_sonar_metrics = {}
        final_sonar_issues = []
        final_sonar_file_metrics = {}

        print("\n  -- Iteration 0/{} --".format(MAX_PATCH_ITERATIONS))
        baseline_path = self.workspace.iteration_path(0)
        baseline_test_proc = self.runner.run_repo_tests_for_iteration(0, self.workspace)
        baseline_junit = self._parse_maven_test_result(baseline_path, baseline_test_proc)
        baseline_passed = baseline_junit.get("tests_passed", 0)
        baseline_total = baseline_junit.get("tests_run", 0)
        baseline_failed = baseline_junit.get("tests_failed", 0)
        print(f"  [maven] baseline {baseline_passed}/{baseline_total} passed, {baseline_failed} failed")
        baseline_failures = baseline_junit.get("failures_with_values", [])

        if not baseline_junit["compile_ok"]:
            attempt_history.append(
                {
                    "iteration": 0,
                    "status": "COMPILE_FAILED",
                    "junit": baseline_junit,
                    "sonar_metrics": {},
                    "sonar_issues": [],
                    "analyzer_hints": None,
                    "artifact_file": None,
                    "file_history": [],
                    "test_comparison": "baseline",
                    "quality_ok": False,
                    "quality_reason": "baseline_compile_failed",
                    "baseline_match": False,
                }
            )
            print("\n  FAIL  baseline compilation failed")
            return {
                "repo_path": self.repo_path,
                "status": "FAIL",
                "attempts": 0,
                "attempt_history": attempt_history,
                "junit_result": baseline_junit,
                "sonar_metrics": {},
                "sonar_issues": [],
                "context_paths": context_paths,
            }

        baseline_quality_ok = False
        baseline_quality_reason = "sonar_disabled"
        previous_sonar_metrics = {}
        previous_sonar_issues = []
        previous_sonar_file_metrics = {}
        if self.sonar:
            baseline_component = self._build_sonar_component(repo_name, 0)
            print(f"  [sonar] component_key={baseline_component}")
            previous_sonar_metrics, previous_sonar_issues, previous_sonar_file_metrics = self._run_sonar(0, baseline_component)
            final_sonar_metrics = previous_sonar_metrics
            final_sonar_issues = previous_sonar_issues
            final_sonar_file_metrics = previous_sonar_file_metrics
            baseline_quality_ok, baseline_quality_reason = self._quality_ok(previous_sonar_file_metrics)
            if baseline_quality_ok:
                print("  [sonar] quality OK")
            else:
                print(f"  [sonar] quality not met: {baseline_quality_reason}")

        attempt_history.append(
            {
                "iteration": 0,
                "status": "PASS" if baseline_quality_ok or not self.sonar else "SONAR_FAILED",
                "junit": baseline_junit,
                "sonar_metrics": previous_sonar_metrics,
                "sonar_issues": previous_sonar_issues,
                "analyzer_hints": None,
                "artifact_file": None,
                "file_history": [],
                "test_comparison": "baseline",
                "quality_ok": baseline_quality_ok,
                "quality_reason": baseline_quality_reason,
                "baseline_match": True,
            }
        )

        if not self.sonar or baseline_quality_ok:
            status_label = "PASS"
            print(f"\n  {status_label}  {baseline_passed}/{baseline_total} tests  (best at iteration 0)")
            return {
                "repo_path": self.repo_path,
                "status": status_label,
                "attempts": 0,
                "attempt_history": attempt_history,
                "junit_result": baseline_junit,
                "sonar_metrics": final_sonar_metrics,
                "sonar_issues": final_sonar_issues,
                "context_paths": context_paths,
            }

        last_junit_result = baseline_junit
        last_completed_iteration = 0

        for iteration in range(1, MAX_PATCH_ITERATIONS + 1):
            print(f"\n  -- Iteration {iteration}/{MAX_PATCH_ITERATIONS} --")
            self.workspace.create_iteration_from_previous(iteration)
            issues_by_file = self._group_issues_by_file(previous_sonar_issues)
            attempt_entry = {
                "iteration": iteration,
                "status": "",
                "junit": None,
                "sonar_metrics": {},
                "sonar_issues": [],
                "analyzer_hints": None,
                "artifact_file": None,
                "file_history": [],
                "test_comparison": "",
                "quality_ok": False,
                "quality_reason": "",
                "baseline_match": False,
            }

            for rel_path in context_paths:
                file_issues = issues_by_file.get(rel_path, [])
                file_metrics = previous_sonar_file_metrics.get(rel_path, {})
                file_quality_ok, file_quality_reason = self._file_quality_ok(file_metrics)
                file_entry = {
                    "path": rel_path,
                    "issue_count": len(file_issues),
                    "sonar_metrics_before": file_metrics,
                    "status": "",
                    "artifact_file": None,
                    "retry_count": 0,
                    "kept": False,
                    "attempts": [],
                }

                if file_quality_ok:
                    file_entry["status"] = "SKIPPED_WITHIN_THRESHOLDS"
                    attempt_entry["file_history"].append(file_entry)
                    print(f"  [file] {rel_path} -> skipped ({file_quality_reason or 'within thresholds'})")
                    continue

                original_context_file = self.workspace.collect_context_files(iteration, [rel_path])[0]
                original_content = original_context_file["content"]
                kept_file = False

                for retry_index in range(MAX_FILE_RETRIES):
                    current_file = self.workspace.collect_context_files(iteration, [rel_path])[0]
                    if retry_index == 0:
                        hints = self.analyzer.analyze_repo_sonar(
                            current_file,
                            previous_sonar_metrics,
                            file_issues,
                        )
                        phase = "sonar"
                    else:
                        last_attempt = file_entry["attempts"][-1]
                        if last_attempt.get("compile_ok") is False:
                            hints = self.analyzer.analyze_compile_errors(
                                current_file,
                                last_attempt.get("compile_errors", ""),
                            )
                            phase = "compile_retry"
                        else:
                            hints = self.analyzer.analyze_test_failures(
                                current_file,
                                last_attempt.get("raw_output", ""),
                                baseline_failures,
                            )
                            phase = "test_retry"

                    hints = (
                        "Rewrite only the provided file.\n"
                        "Keep the change minimal and behavior-preserving.\n"
                        "The file can be kept only if compilation succeeds and the exact baseline failing-test set is restored.\n\n"
                        f"{hints}"
                    )
                    attempt_entry["analyzer_hints"] = hints

                    try:
                        generation = self._generate_files(current_file, hints)
                    except RuntimeError as exc:
                        file_entry["status"] = "CODER_FORMAT_ERROR"
                        file_entry["error"] = str(exc)
                        file_entry["attempts"].append(
                            {"retry": retry_index + 1, "phase": phase, "status": "CODER_FORMAT_ERROR", "error": str(exc)}
                        )
                        print(f"  [file] {rel_path} -> coder failed on retry {retry_index + 1}: {exc}")
                        continue

                    artifact_name = (
                        f"iteration_{iteration}_{self._safe_artifact_name(rel_path)}"
                        f".retry_{retry_index + 1}.files.json"
                    )
                    artifact_path = self.workspace.write_json_artifact(
                        generation,
                        iteration,
                        filename=artifact_name,
                    )
                    file_entry["artifact_file"] = artifact_path
                    if attempt_entry["artifact_file"] is None:
                        attempt_entry["artifact_file"] = artifact_path

                    try:
                        self.workspace.apply_files(generation["files"], iteration)
                    except RuntimeError as exc:
                        file_entry["status"] = "FILE_APPLY_FAILED"
                        file_entry["error"] = str(exc)
                        file_entry["attempts"].append(
                            {"retry": retry_index + 1, "phase": phase, "status": "FILE_APPLY_FAILED", "error": str(exc)}
                        )
                        print(f"  [file] {rel_path} -> apply failed on retry {retry_index + 1}: {exc}")
                        continue

                    compile_proc = self.runner.run_repo_compile_for_iteration(iteration, self.workspace)
                    compile_result = self._parse_maven_test_result(self.workspace.iteration_path(iteration), compile_proc)
                    attempt_record = {
                        "retry": retry_index + 1,
                        "phase": phase,
                        "compile_ok": compile_result.get("compile_ok", False),
                        "compile_errors": compile_result.get("compile_errors"),
                        "status": "",
                        "raw_output": compile_result.get("raw_output", ""),
                    }

                    if not compile_result["compile_ok"]:
                        attempt_record["status"] = "compile_failed"
                        file_entry["attempts"].append(attempt_record)
                        print(f"  [file] {rel_path} -> compile failed on retry {retry_index + 1}")
                        continue

                    test_proc = self.runner.run_repo_tests_for_iteration(iteration, self.workspace)
                    test_result = self._parse_maven_test_result(self.workspace.iteration_path(iteration), test_proc)
                    baseline_match = self._baseline_failures_match(baseline_junit, test_result)
                    attempt_record.update(
                        {
                            "tests_failed": test_result.get("tests_failed", 0),
                            "tests_passed": test_result.get("tests_passed", 0),
                            "test_comparison": self._compare_test_results(baseline_junit, test_result),
                            "baseline_match": baseline_match,
                            "status": "kept" if baseline_match else "test_mismatch",
                            "raw_output": test_result.get("raw_output", ""),
                            "failures_with_values": test_result.get("failures_with_values", []),
                        }
                    )
                    file_entry["attempts"].append(attempt_record)

                    if not baseline_match:
                        print(f"  [file] {rel_path} -> tests mismatch on retry {retry_index + 1}")
                        continue

                    kept_file = True
                    file_entry["retry_count"] = retry_index + 1
                    file_entry["kept"] = True
                    file_entry["status"] = "KEPT"
                    print(f"  [file] {rel_path} -> kept on retry {retry_index + 1}")

                    sonar_component = self._build_sonar_component(repo_name, iteration)
                    print(f"  [sonar] component_key={sonar_component}")
                    sonar_metrics, sonar_issues, sonar_file_metrics = self._run_sonar(iteration, sonar_component)
                    previous_sonar_metrics = sonar_metrics
                    previous_sonar_issues = sonar_issues
                    previous_sonar_file_metrics = sonar_file_metrics
                    final_sonar_metrics = sonar_metrics
                    final_sonar_issues = sonar_issues
                    final_sonar_file_metrics = sonar_file_metrics
                    file_entry["sonar_metrics_after_keep"] = sonar_metrics
                    file_entry["sonar_metrics_for_file_after_keep"] = sonar_file_metrics.get(rel_path, {})
                    file_entry["quality_ok_after_keep"], file_entry["quality_reason_after_keep"] = self._file_quality_ok(
                        sonar_file_metrics.get(rel_path, {})
                    )
                    break

                if not kept_file:
                    self.workspace.restore_file(iteration, rel_path, original_content)
                    file_entry["retry_count"] = len(file_entry["attempts"])
                    file_entry["kept"] = False
                    file_entry["status"] = file_entry["status"] or "REVERTED"
                    print(f"  [file] {rel_path} -> reverted after {file_entry['retry_count']} attempts")

                attempt_entry["file_history"].append(file_entry)

            test_proc = self.runner.run_repo_tests_for_iteration(iteration, self.workspace)
            junit_result = self._parse_maven_test_result(self.workspace.iteration_path(iteration), test_proc)
            attempt_entry["junit"] = junit_result
            attempt_entry["test_comparison"] = self._compare_test_results(baseline_junit, junit_result)
            attempt_entry["baseline_match"] = self._baseline_failures_match(baseline_junit, junit_result)

            passed = junit_result.get("tests_passed", 0)
            total = junit_result.get("tests_run", 0)
            failed = junit_result.get("tests_failed", 0)
            print(
                f"  [maven] {passed}/{total} passed, {failed} failed "
                f"({attempt_entry['test_comparison']})"
            )

            if not junit_result["compile_ok"]:
                attempt_entry["status"] = "COMPILE_FAILED"
                attempt_history.append(attempt_entry)
                break

            if not attempt_entry["baseline_match"]:
                attempt_entry["status"] = "TESTS_REDUCED"
                attempt_history.append(attempt_entry)
                break

            attempt_entry["sonar_metrics"] = final_sonar_metrics
            attempt_entry["sonar_issues"] = final_sonar_issues

            quality_ok, quality_reason = self._quality_ok(final_sonar_file_metrics)
            attempt_entry["quality_ok"] = quality_ok
            attempt_entry["quality_reason"] = quality_reason

            if not final_sonar_metrics:
                attempt_entry["status"] = "SONAR_SCAN_FAILED"
                attempt_history.append(attempt_entry)
                break

            last_junit_result = junit_result
            last_completed_iteration = iteration

            if quality_ok:
                print("  [sonar] quality OK")
                attempt_entry["status"] = "PASS"
                attempt_history.append(attempt_entry)
                break

            print(f"  [sonar] quality not met: {quality_reason}")
            attempt_entry["status"] = "SONAR_FAILED"
            attempt_history.append(attempt_entry)

        status_label = "PASS" if attempt_history and attempt_history[-1]["status"] == "PASS" else "FAIL"
        final_passed = last_junit_result.get("tests_passed", 0)
        final_total = last_junit_result.get("tests_run", 0)
        print(f"\n  {status_label}  {final_passed}/{final_total} tests  (last completed iteration {last_completed_iteration})")

        return {
            "repo_path": self.repo_path,
            "status": status_label,
            "attempts": last_completed_iteration,
            "attempt_history": attempt_history,
            "junit_result": last_junit_result,
            "sonar_metrics": final_sonar_metrics,
            "sonar_issues": final_sonar_issues,
            "context_paths": context_paths,
        }

    def _generate_files(self, context_file: dict, hints: str) -> dict:
        try:
            return self.coder.code_repo_files(
                context_file=context_file,
                hints=hints,
            )
        except ValueError as exc:
            raise RuntimeError(
                "Coder did not return a valid file payload. "
                f"Expected JSON with a 'files' array. Details: {exc}"
            ) from exc

    def _run_sonar(self, iteration: int, component: str) -> tuple[dict, list, dict[str, dict]]:
        iter_path = self.workspace.iteration_path(iteration)
        try:
            self.sonar.sonar_scan(iter_path, project_key=component)
            metrics = self.sonar.get_metrics(component=component)
            issues = self.sonar.get_issues(
                component=component,
                in_new_code_period=False,
                retries=8,
                retry_delay=2,
                retry_on_empty=True,
            )
            file_metrics = self.sonar.get_file_metrics(component=component)
            print(f"  [sonar] metrics: {metrics}")
            return metrics, issues, file_metrics
        except Exception as exc:
            print(f"  [sonar] scan failed: {exc}")
            return {}, [], {}

    def _build_sonar_component(self, repo_name: str, iteration: int) -> str:
        if not self.sonar:
            return ""
        base = self.sonar.project_key
        sanitized = re.sub(r"[^a-zA-Z0-9_.:-]", "_", repo_name)
        return f"{base}_{sanitized}_it{iteration}_{self._run_nonce}"

    @staticmethod
    def _group_issues_by_file(issues: list[dict]) -> dict[str, list[dict]]:
        grouped = {}
        for issue in issues:
            component = str(issue.get("component", "")).strip()
            if not component:
                continue
            _, _, rel_path = component.partition(":")
            normalized = (rel_path or component).replace("\\", "/")
            grouped.setdefault(normalized, []).append(issue)
        return grouped

    @staticmethod
    def _baseline_failures_match(baseline: dict, candidate: dict) -> bool:
        if not candidate.get("compile_ok", False):
            return False
        return (baseline.get("failures_with_values", []) or []) == (candidate.get("failures_with_values", []) or [])

    @staticmethod
    def _compare_test_results(baseline: dict, candidate: dict) -> str:
        if not candidate.get("compile_ok", False):
            return "compile_failed"
        if EvaluationLoop._baseline_failures_match(baseline, candidate):
            return "baseline_match"
        if candidate.get("tests_failed", 0) > baseline.get("tests_failed", 0):
            return "reduced"
        return "mismatch"

    @staticmethod
    def _safe_artifact_name(path: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_.-]", "_", path)

    @staticmethod
    def _parse_maven_test_result(iteration_path: str, proc) -> dict:
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        compile_ok = not EvaluationLoop._looks_like_compile_failure(output)

        if not compile_ok:
            return {
                "success": False,
                "compile_ok": False,
                "compile_errors": EvaluationLoop._extract_compile_excerpt(output),
                "tests_run": 0,
                "tests_passed": 0,
                "tests_failed": 0,
                "failures_with_values": [],
                "raw_output": output,
            }

        tests_run, tests_passed, tests_failed, failures = EvaluationLoop._parse_surefire_reports(iteration_path)
        if tests_run == 0:
            tests_run, tests_passed, tests_failed = EvaluationLoop._parse_maven_summary(output)

        return {
            "success": proc.returncode == 0 and tests_failed == 0,
            "compile_ok": True,
            "compile_errors": None,
            "tests_run": tests_run,
            "tests_passed": tests_passed,
            "tests_failed": tests_failed,
            "failures_with_values": failures,
            "raw_output": output,
        }

    @staticmethod
    def _parse_surefire_reports(iteration_path: str) -> tuple[int, int, int, list[str]]:
        reports_dir = Path(iteration_path) / "target" / "surefire-reports"
        if not reports_dir.is_dir():
            return 0, 0, 0, []

        tests_run = 0
        tests_failed = 0
        failures = []

        for report_path in reports_dir.glob("TEST-*.xml"):
            try:
                root = ET.parse(report_path).getroot()
            except ET.ParseError:
                continue

            tests = int(root.attrib.get("tests", "0") or 0)
            errors = int(root.attrib.get("errors", "0") or 0)
            failed = int(root.attrib.get("failures", "0") or 0)
            skipped = int(root.attrib.get("skipped", "0") or 0)

            tests_run += max(tests - skipped, 0)
            tests_failed += failed + errors

            for testcase in root.findall("testcase"):
                failure_node = testcase.find("failure")
                error_node = testcase.find("error")
                node = failure_node if failure_node is not None else error_node
                if node is None:
                    continue

                classname = testcase.attrib.get("classname", "")
                method = testcase.attrib.get("name", "")
                summary = node.attrib.get("message", "") or (node.text or "").strip()
                summary = re.sub(r"\s+", " ", summary).strip()
                label = f"{classname}.{method}" if classname else method
                failures.append(f"{label}: {summary}" if summary else label)

        tests_passed = max(tests_run - tests_failed, 0)
        return tests_run, tests_passed, tests_failed, failures

    @staticmethod
    def _parse_maven_summary(output: str) -> tuple[int, int, int]:
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
        match = re.search(
            r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)",
            clean,
        )
        if not match:
            return 0, 0, 0

        tests_run = int(match.group(1)) - int(match.group(4))
        tests_failed = int(match.group(2)) + int(match.group(3))
        tests_passed = max(tests_run - tests_failed, 0)
        return tests_run, tests_passed, tests_failed

    @staticmethod
    def _looks_like_compile_failure(output: str) -> bool:
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
        upper = clean.upper()
        if "COMPILATION ERROR" in upper:
            return True

        # Restrict this check to actual compiler-plugin failures. Test failures
        # also contain "Failed to execute goal", and the build log includes
        # earlier "compiler:" lines even when compilation succeeded.
        return bool(
            re.search(
                r"FAILED TO EXECUTE GOAL .*MAVEN-COMPILER-PLUGIN",
                upper,
            )
        )

    @staticmethod
    def _extract_compile_excerpt(output: str, max_chars: int = 2000) -> str:
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output).strip()
        return clean[-max_chars:] if len(clean) > max_chars else clean

    @staticmethod
    def _build_test_analysis_input(junit_result: dict) -> str:
        if not junit_result.get("compile_ok", True):
            return (
                "Compilation failed.\n"
                f"ERRORS:\n{junit_result.get('compile_errors', '')}\n"
            )

        failures = junit_result.get("failures_with_values", [])
        failure_block = "\n".join(f"- {item}" for item in failures) if failures else "(no detailed failures found)"
        return (
            f"Tests run: {junit_result.get('tests_run', 0)}\n"
            f"Tests passed: {junit_result.get('tests_passed', 0)}\n"
            f"Tests failed: {junit_result.get('tests_failed', 0)}\n"
            f"FAILURES:\n{failure_block}\n\n"
            f"RAW OUTPUT:\n{junit_result.get('raw_output', '')[-4000:]}"
        )

    @staticmethod
    def _file_quality_ok(metrics: dict) -> tuple[bool, str]:
        if not metrics:
            return True, ""

        bugs = int(metrics.get("bugs", 0))
        smells = int(metrics.get("code_smells", 0))
        cognitive = int(metrics.get("cognitive_complexity", 0))
        if bugs > SONAR_MAX_BUGS:
            return False, f"bugs={bugs}"
        if smells > SONAR_MAX_CODE_SMELLS:
            return False, f"code_smells={smells}"
        if cognitive > SONAR_MAX_COGNITIVE_COMPLEXITY and cognitive > 0:
            return False, f"cognitive_complexity={cognitive}"
        return True, ""

    @staticmethod
    def _quality_ok(file_metrics_by_path: dict[str, dict]) -> tuple[bool, str]:
        if not file_metrics_by_path:
            return False, "missing_file_metrics"

        for path in sorted(file_metrics_by_path):
            ok, reason = EvaluationLoop._file_quality_ok(file_metrics_by_path[path])
            if not ok:
                return False, f"{path}: {reason}"
        return True, ""
