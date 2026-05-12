"""
orchestrator/loop.py
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
SONAR_MAX_CODE_SMELLS = 0
SONAR_MAX_COGNITIVE_COMPLEXITY = 25


def build_components(repo_path=None):
    load_dotenv(override=True)
    llm = CodexClient(model="gpt-5.4-nano")
    workspace = WorkspaceManager(base_dir=str(PROJECT_ROOT / "workspace"))
    workspace.set_source_repo(repo_path or str(DEFAULT_REPO_PATH))
    runner = DockerRunner()
    sonar = None
    token, key = os.getenv("SONAR_TOKEN"), os.getenv("SONAR_PROJECT_KEY")
    host = os.getenv("SONAR_HOST_URL", "http://localhost:9000")
    if token and key:
        sonar = SonarService(token=token, project_key=key, docker_runner=runner, host=host)
    return EvaluationLoop(
        coder=CoderAgent(llm),
        analyzer=AnalyzerAgent(llm),
        workspace=workspace,
        runner=runner,
        sonar=sonar,
        repo_path=workspace.source_repo,
        context_subdir=DEFAULT_CONTEXT_SUBDIR,
    )


def run_normal_mode(args=None):
    result = build_components(DEFAULT_REPO_PATH).run()
    _save_result(result)
    return result


run_future_mode = run_normal_mode


def _save_result(result, output_path="results.json"):
    ansi = re.compile(r"\x1b\[[0-9;]*m")

    def clean(v):
        if isinstance(v, str): return ansi.sub("", v)
        if isinstance(v, dict): return {k: clean(x) for k, x in v.items()}
        if isinstance(v, list): return [clean(x) for x in v]
        return v

    path = PROJECT_ROOT / output_path
    path.write_text(json.dumps(clean(result), indent=2, ensure_ascii=False))
    print(f"\nResults saved to {path}")


class EvaluationLoop:
    def __init__(self, coder, analyzer, workspace, runner, sonar=None, repo_path="", context_subdir=""):
        self.coder = coder
        self.analyzer = analyzer
        self.workspace = workspace
        self.runner = runner
        self.sonar = sonar
        self.repo_path = repo_path
        self.context_subdir = context_subdir
        self._nonce = uuid.uuid4().hex[:8]

    def run(self):
        repo_name = Path(self.repo_path).name
        print(f"\n{'─'*60}\n  Repo: {repo_name}\n{'─'*60}")

        self.workspace.bootstrap_iteration(0)
        context_paths = self.workspace.list_java_files(0, self.context_subdir)
        if not context_paths:
            raise RuntimeError(f"No Java files found under {self.context_subdir}")
        print(f"  [context] {len(context_paths)} Java files from {self.context_subdir}")

        # ── iteration 0: baseline ──────────────────────────────────────
        self.runner.run_repo_tests_for_iteration(0, self.workspace)
        sonar_metrics, sonar_issues, file_metrics = {}, [], {}
        if self.sonar:
            sonar_metrics, sonar_issues, file_metrics = self._sonar_scan(repo_name, 0)
            if self._quality_ok(file_metrics):
                print("  [sonar] baseline quality OK")
                return {"status": "PASS", "iterations": [{"iteration": 0, "sonar_metrics": sonar_metrics, "file_metrics": file_metrics}]}
            print("  [sonar] baseline quality not met, starting iterations")

        iterations = [{"iteration": 0, "sonar_metrics": sonar_metrics, "file_metrics": file_metrics}]

        # ── improvement iterations ─────────────────────────────────────
        for iteration in range(1, MAX_PATCH_ITERATIONS + 1):
            print(f"\n  -- Iteration {iteration}/{MAX_PATCH_ITERATIONS} --")
            self.workspace.create_iteration_from_previous(iteration)
            issues_by_file = self._group_issues_by_file(sonar_issues)
            file_logs = []

            for rel_path in context_paths:
                fq_ok, fq_reason = self._file_quality_ok(file_metrics.get(rel_path, {}))
                if fq_ok:
                    print(f"  [file] {rel_path} -> skipped ({fq_reason or 'within thresholds'})")
                    file_logs.append({"path": rel_path, "status": "skipped"})
                    continue

                log = {"path": rel_path, "attempts": []}
                accepted = False

                for retry in range(MAX_FILE_RETRIES):
                    current_file = self.workspace.collect_context_files(iteration, [rel_path])[0]
                    prev = log["attempts"][-1] if log["attempts"] else {}

                    if retry == 0:
                        hints = self.analyzer.analyze_repo_sonar(current_file, sonar_metrics, issues_by_file.get(rel_path, []))
                    elif not prev.get("compile_ok"):
                        hints = self.analyzer.analyze_compile_errors(current_file, prev.get("compile_errors", ""))
                    else:
                        hints = self.analyzer.analyze_test_failures(current_file, "\n".join(prev.get("failures", [])[:20]))

                    try:
                        gen = self.coder.code_repo_files(context_file=current_file, hints=hints)
                        self.workspace.apply_files(gen["files"], iteration)
                    except (ValueError, RuntimeError) as exc:
                        log["attempts"].append({"retry": retry + 1, "error": str(exc)})
                        print(f"  [file] {rel_path} -> coder error on retry {retry + 1}: {exc}")
                        continue

                    compile_result = self._parse_maven(
                        self.workspace.iteration_path(iteration),
                        self.runner.run_repo_compile_for_iteration(iteration, self.workspace),
                    )
                    if not compile_result["compile_ok"]:
                        log["attempts"].append({"retry": retry + 1, "compile_ok": False, "compile_errors": compile_result["compile_errors"]})
                        print(f"  [file] {rel_path} -> compile failed (retry {retry + 1})")
                        continue

                    test_result = self._parse_maven(
                        self.workspace.iteration_path(iteration),
                        self.runner.run_repo_tests_for_iteration(iteration, self.workspace),
                    )
                    failures = test_result.get("failures_with_values", [])
                    tests_failed = test_result.get("tests_failed", 0)
                    if tests_failed > 0 or not test_result.get("compile_ok"):
                        log["attempts"].append({"retry": retry + 1, "compile_ok": True, "tests_failed": tests_failed, "failures": failures})
                        print(f"  [file] {rel_path} -> {len(failures)} tests failed (retry {retry + 1})")
                        continue

                    accepted = True
                    log["attempts"].append({"retry": retry + 1, "compile_ok": True, "tests_failed": 0})
                    print(f"  [file] {rel_path} -> accepted (retry {retry + 1})")
                    if self.sonar:
                        sonar_metrics, sonar_issues, file_metrics = self._sonar_scan(repo_name, iteration)
                        log["sonar_after"] = file_metrics.get(rel_path, {})
                    break

                if not accepted:
                    self.workspace.restore_file(iteration - 1, iteration, rel_path)
                    print(f"  [file] {rel_path} -> rejected after {len(log['attempts'])} attempts")

                log["status"] = "accepted" if accepted else "rejected"
                file_logs.append(log)

            # ── end-of-iteration checks ────────────────────────────────
            final = self._parse_maven(
                self.workspace.iteration_path(iteration),
                self.runner.run_repo_tests_for_iteration(iteration, self.workspace),
            )
            iter_entry = {
                "iteration": iteration,
                "sonar_metrics": sonar_metrics,
                "file_metrics": file_metrics,
                "files": file_logs,
                "tests_passed": final.get("tests_passed", 0),
                "tests_run": final.get("tests_run", 0),
            }
            iterations.append(iter_entry)

            if not final.get("compile_ok"):
                print("  [maven] compile failed at end of iteration")
                return {"status": "FAIL", "reason": "compile_failed", "iterations": iterations}

            if not self._tests_all_pass(final):
                print(f"  [maven] {final.get('tests_failed', 0)} tests still failing")
                return {"status": "FAIL", "reason": "tests_failed", "iterations": iterations}

            if self.sonar:
                if self._quality_ok(file_metrics):
                    print("  [sonar] quality OK")
                    return {"status": "PASS", "iterations": iterations}
                print("  [sonar] quality not met, continuing...")

        return {"status": "FAIL", "reason": "max_iterations_reached", "iterations": iterations}

    # ── sonar ──────────────────────────────────────────────────────────

    def _sonar_scan(self, repo_name, iteration):
        sanitized = re.sub(r"[^a-zA-Z0-9_.:-]", "_", repo_name)
        component = f"{self.sonar.project_key}_{sanitized}_it{iteration}_{self._nonce}"
        iter_path = self.workspace.iteration_path(iteration)
        print(f"  [sonar] component={component}")
        try:
            self.sonar.sonar_scan(iter_path, project_key=component)
            metrics = self.sonar.get_metrics(component=component)
            issues = self.sonar.get_issues(component=component, in_new_code_period=False, retries=8, retry_delay=2, retry_on_empty=True)
            file_metrics = self.sonar.get_file_metrics(component=component)
            print(f"  [sonar] metrics: {metrics}")
            return metrics, issues, file_metrics
        except Exception as exc:
            print(f"  [sonar] scan failed: {exc}")
            return {}, [], {}

    # ── quality checks ─────────────────────────────────────────────────

    @staticmethod
    def _file_quality_ok(metrics):
        if not metrics:
            return True, ""
        bugs = int(metrics.get("bugs", 0))
        smells = int(metrics.get("code_smells", 0))
        cognitive = int(metrics.get("cognitive_complexity", 0))
        if bugs > SONAR_MAX_BUGS: return False, f"bugs={bugs}"
        if smells > SONAR_MAX_CODE_SMELLS: return False, f"code_smells={smells}"
        if cognitive > SONAR_MAX_COGNITIVE_COMPLEXITY: return False, f"cognitive_complexity={cognitive}"
        return True, ""

    @staticmethod
    def _quality_ok(file_metrics_by_path):
        if not file_metrics_by_path:
            return False
        return all(EvaluationLoop._file_quality_ok(m)[0] for m in file_metrics_by_path.values())

    # ── maven parsing ──────────────────────────────────────────────────

    @staticmethod
    def _tests_all_pass(result):
        return result.get("compile_ok", False) and result.get("tests_failed", 0) == 0

    @staticmethod
    def _parse_maven(iteration_path, proc):
        output = re.sub(r"\x1b\[[0-9;]*m", "", (proc.stdout or "") + "\n" + (proc.stderr or ""))
        upper = output.upper()
        if "COMPILATION ERROR" in upper or re.search(r"FAILED TO EXECUTE GOAL .*MAVEN-COMPILER-PLUGIN", upper):
            return {"compile_ok": False, "compile_errors": output.strip()[-2000:], "tests_run": 0, "tests_passed": 0, "tests_failed": 0, "failures_with_values": []}

        run, passed, failed, failures = EvaluationLoop._parse_surefire(iteration_path)
        if run == 0:
            run, passed, failed = EvaluationLoop._parse_maven_summary(output)
        return {"compile_ok": True, "compile_errors": None, "tests_run": run, "tests_passed": passed, "tests_failed": failed, "failures_with_values": failures}

    @staticmethod
    def _parse_surefire(iteration_path):
        reports_dir = Path(iteration_path) / "target" / "surefire-reports"
        if not reports_dir.is_dir():
            return 0, 0, 0, []
        run = failed = 0
        failures = []
        for xml in reports_dir.glob("TEST-*.xml"):
            try:
                root = ET.parse(xml).getroot()
            except ET.ParseError:
                continue
            tests = int(root.attrib.get("tests", 0) or 0)
            skipped = int(root.attrib.get("skipped", 0) or 0)
            errs = int(root.attrib.get("errors", 0) or 0)
            fails = int(root.attrib.get("failures", 0) or 0)
            run += max(tests - skipped, 0)
            failed += fails + errs
            for tc in root.findall("testcase"):
                node = tc.find("failure") or tc.find("error")
                if node is None:
                    continue
                cls, method = tc.attrib.get("classname", ""), tc.attrib.get("name", "")
                msg = re.sub(r"\s+", " ", node.attrib.get("message", "") or (node.text or "")).strip()
                label = f"{cls}.{method}" if cls else method
                failures.append(f"{label}: {msg}" if msg else label)
        return run, max(run - failed, 0), failed, failures

    @staticmethod
    def _parse_maven_summary(output):
        m = re.search(r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)", output)
        if not m:
            return 0, 0, 0
        run = int(m.group(1)) - int(m.group(4))
        failed = int(m.group(2)) + int(m.group(3))
        return run, max(run - failed, 0), failed

    @staticmethod
    def _group_issues_by_file(issues):
        grouped = {}
        for issue in issues:
            component = str(issue.get("component", "")).strip()
            if not component:
                continue
            _, _, rel = component.partition(":")
            key = (rel or component).replace("\\", "/")
            grouped.setdefault(key, []).append(issue)
        return grouped