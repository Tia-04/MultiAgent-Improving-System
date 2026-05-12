"""
services/sonar_service.py
--------------------------
SonarQube client. Restored to the approach that worked in the original project:
  - sonar-scanner-cli runs as a Docker container
  - Uses host.docker.internal:9000 to reach SonarQube on Windows Docker Desktop
  - Basic auth: auth=(token, "") — works with SonarQube lts-community
  - After scan: polls /api/ce/component until SUCCESS instead of fixed sleep
"""

import os
import time

import requests


METRIC_KEYS = [
    "ncloc",
    "functions",
    "complexity",
    "cognitive_complexity",
    "bugs",
    "vulnerabilities",
    "code_smells",
    "duplicated_lines_density",
]


class SonarService:
    def __init__(self, token: str, project_key: str, docker_runner, host: str = "http://localhost:9000"):
        # `project_key` acts as a base key; loop may override per attempt.
        self.token = token
        self.project_key = project_key
        self.docker_runner = docker_runner
        self.host = host
        self.api = f"{host}/api"

    # ── Scanner ───────────────────────────────────────────────────────────────

    def sonar_scan(self, source_folder: str, project_key: str | None = None) -> None:
        """
        Run sonar-scanner-cli as a Docker container, reaching SonarQube at port 9000.
        """
        abs_folder = os.path.abspath(source_folder)
        effective_project_key = project_key or self.project_key
        token_preview = self.token[:8] + "..." if self.token else "(empty!)"
        print(
            f"  [sonar] scanning {abs_folder} "
            f"(project_key={effective_project_key}, token: {token_preview}) ..."
        )

        result = self.docker_runner.run_sonar_scanner(
            source_folder=source_folder,
            project_key=effective_project_key,
            token=self.token,
        )
        if result.returncode != 0:
            print(f"  [sonar] stderr: {result.stderr[-600:]}")
            raise RuntimeError("SonarQube scan failed")

        print("  [sonar] scan submitted — waiting for background task...")
        self._wait_for_analysis(component=effective_project_key)
        self._wait_for_measures(component=effective_project_key)

    def scan(self, source_folder: str, iteration: int, project_key: str | None = None) -> None:
        """Backward-compatible alias while callers migrate to sonar_scan()."""
        self.sonar_scan(source_folder=source_folder, project_key=project_key)

    # ── Wait for background task ──────────────────────────────────────────────

    def _wait_for_analysis(
        self,
        component: str | None = None,
        timeout: int = 100,
        poll_interval: int = 3,
    ) -> None:
        """
        Poll /api/ce/component until the background task reports SUCCESS.
        proceeds as soon as data is ready, to make sure that the scan is really complete.
        """
        url     = f"{self.api}/ce/component"
        component_key = component or self.project_key
        params  = {"component": component_key}
        elapsed = 0

        while elapsed < timeout:
            time.sleep(poll_interval)
            elapsed += poll_interval
            try:
                resp   = requests.get(url, params=params,
                                      auth=(self.token, ""), timeout=10)
                resp.raise_for_status()
                status = resp.json().get("current", {}).get("status", "")
                print(f"  [sonar] task status: {status} ({elapsed}s)")
                if status == "SUCCESS":
                    return
                if status in ("FAILED", "CANCELED"):
                    raise RuntimeError(f"SonarQube background task {status}")
            except requests.exceptions.RequestException as e:
                print(f"  [sonar] poll error: {e}")

        print(f"  [sonar] warning: timed out after {timeout}s, fetching anyway")

    def _wait_for_measures(
        self,
        component: str | None = None,
        timeout: int = 15,
        poll_interval: int = 1,
    ) -> None:
        """
        After CE reports SUCCESS, sometimes SonarQube may still need a brief moment before
        measures are queryable with the latest values. 
        """
        elapsed = 0
        while elapsed < timeout:
            time.sleep(poll_interval)
            elapsed += poll_interval
            try:
                metrics = self.get_metrics(component=component)
                if metrics:
                    print(f"  [sonar] measures ready ({elapsed}s)")
                    return
            except requests.exceptions.RequestException as e:
                print(f"  [sonar] measures poll error: {e}")

        print(f"  [sonar] warning: measures not confirmed after {timeout}s")

    # ── Metrics ───────────────────────────────────────────────────────────────

    def get_metrics(self, component: str | None = None, retries: int = 3, retry_delay: int = 1) -> dict:
        """Fetch Sonar measures for one component, with retry for eventual consistency."""
        component = component or self.project_key
        url    = f"{self.api}/measures/component"
        params = {"component": component, "metricKeys": ",".join(METRIC_KEYS)}
        last_error = None

        for attempt in range(1, retries + 1):
            try:
                resp = requests.get(url, params=params, auth=(self.token, ""), timeout=15)
                resp.raise_for_status()
                measures = resp.json().get("component", {}).get("measures", [])
                if measures:
                    return {m["metric"]: m.get("value", "N/A") for m in measures}
            except requests.exceptions.RequestException as e:
                last_error = e

            if attempt < retries:
                time.sleep(retry_delay)

        if last_error:
            raise last_error
        return {}

    def get_file_metrics(
        self,
        component: str | None = None,
        metric_keys: list[str] | None = None,
        retries: int = 3,
        retry_delay: int = 1,
    ) -> dict[str, dict]:
        """Fetch selected Sonar measures for each file in one component."""
        component = component or self.project_key
        metric_keys = metric_keys or ["bugs", "code_smells", "complexity", "cognitive_complexity"]
        url = f"{self.api}/measures/component_tree"
        params = {
            "component": component,
            "qualifiers": "FIL",
            "metricKeys": ",".join(metric_keys),
            "ps": 500,
        }
        last_error = None

        for attempt in range(1, retries + 1):
            try:
                resp = requests.get(url, params=params, auth=(self.token, ""), timeout=15)
                resp.raise_for_status()
                components = resp.json().get("components", [])
                if components:
                    result = {}
                    for item in components:
                        path = str(item.get("path", "")).replace("\\", "/")
                        if not path:
                            continue
                        result[path] = {
                            measure["metric"]: measure.get("value", "0")
                            for measure in item.get("measures", [])
                        }
                    return result
            except requests.exceptions.RequestException as e:
                last_error = e

            if attempt < retries:
                time.sleep(retry_delay)

        if last_error:
            raise last_error
        return {}

    # ── Issues ────────────────────────────────────────────────────────────────

    def get_issues(
        self,
        component: str | None = None,
        issue_types: list[str] | None = None,
        tags: list[str] | None = None,
        in_new_code_period: bool = False,
        page_size: int = 100,
        retries: int = 3,
        retry_delay: int = 1,
        retry_on_empty: bool = False,
    ) -> list:
        """
        Fetch Sonar issues with optional filters (type/tags/new-code).

        `retry_on_empty=True` is useful right after scan completion when issues
        index may lag a few seconds behind metrics.
        """
        component = component or self.project_key
        url    = f"{self.api}/issues/search"
        params = {
            "componentKeys": component,
            "statuses":      "OPEN",
            "ps":            page_size,
        }
        if issue_types:
            params["types"] = ",".join(issue_types)
        if tags:
            params["tags"] = ",".join(tags)
        if in_new_code_period:
            params["inNewCodePeriod"] = "true"
        print(f"  [DEBUG][sonar] get_issues params={params}")

        last_error = None
        for attempt in range(1, retries + 1):
            try:
                resp = requests.get(url, params=params, auth=(self.token, ""), timeout=15)
                resp.raise_for_status()
                issues = resp.json().get("issues", [])
                print(
                    f"  [DEBUG][sonar] get_issues result_count={len(issues)} "
                    f"(attempt={attempt}/{retries})"
                )
                if issues or not retry_on_empty or attempt == retries:
                    return issues
            except requests.exceptions.RequestException as e:
                last_error = e
                print(f"  [sonar] issues poll error: {e}")

            if attempt < retries:
                time.sleep(retry_delay)

        if last_error:
            raise last_error
        return []
    #TO REMOVE
    def get_issues_for_metric(
        self,
        metric: str,
        component: str | None = None,
        in_new_code_period: bool = True,
    ) -> list:
        """Convenience wrapper: return issues most relevant to one failed metric by filtering for issue type and keywords."""
        metric_name = (metric or "").strip().lower()
        print(
            f"  [DEBUG][sonar] get_issues_for_metric metric={metric_name} "
            f"in_new_code_period={in_new_code_period}"
        )
        if metric_name == "bugs":
            return self.get_issues(
                component=component,
                issue_types=["BUG"],
                in_new_code_period=in_new_code_period,
            )

        if metric_name == "code_smells":
            return self.get_issues(
                component=component,
                issue_types=["CODE_SMELL"],
                in_new_code_period=in_new_code_period,
            )
        if metric_name == "cognitive_complexity":
            smells = self.get_issues(
                component=component,
                issue_types=["CODE_SMELL"],
                in_new_code_period=in_new_code_period,
            )
            focused = []
            for i in smells:
                text = " ".join(
                    str(i.get(k, "")) for k in ("message", "rule", "cleanCodeAttribute")
                ).lower()
                # java:S3776 is Sonar's canonical Cognitive Complexity rule.
                if "cognitive" in text or "s3776" in text:
                    focused.append(i)
            return focused or smells

        return self.get_issues(component=component, in_new_code_period=in_new_code_period)
