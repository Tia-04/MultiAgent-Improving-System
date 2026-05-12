"""
services/docker_runner.py
-------------------------
Owns Docker command execution for the project:
  - runs Maven tests inside the already-running `java_tester_container` container
  - runs Sonar scanner inside the same container against mounted sources
"""

import os
import subprocess


class DockerRunner:
    JAVA_TESTER_CONTAINER = "java_tester_container"
    TEST_TIMEOUT = 1000
    COMPILE_TIMEOUT = 600
    SONAR_TIMEOUT = 1000
    CONTAINER_WORKSPACE_ROOT = "/app/workspace"
    CONTAINER_CLONED_REPO_ROOT = "/app/cloned_repo"

    def run_command(self, cmd: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
        """Execute one Docker command and return the completed process."""
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout or self.TEST_TIMEOUT,
        )

    def run_repo_tests(
        self,
        source_folder: str,
        mvn_args: list[str] | None = None,
    ) -> subprocess.CompletedProcess:
        """
        Run `mvn test` inside the tester container against one repo snapshot.

        `source_folder` is typically `workspace/iteration_n` on the host.
        """
        container_folder = self._host_path_to_container_path(source_folder)
        cmd = [
            "docker", "exec", self.JAVA_TESTER_CONTAINER,
            "/app/run_repo_tests.sh",
            container_folder,
            *(mvn_args or []),
        ]
        print(f"  [docker] exec: {' '.join(cmd)}")
        proc = self.run_command(cmd, timeout=self.TEST_TIMEOUT)
        self._log_process_output(proc)
        return proc

    def run_repo_tests_for_iteration(
        self,
        iteration: int,
        workspace,
        mvn_args: list[str] | None = None,
    ) -> subprocess.CompletedProcess:
        """Convenience wrapper for one workspace iteration snapshot."""
        return self.run_repo_tests(
            source_folder=workspace.iteration_path(iteration),
            mvn_args=mvn_args,
        )

    def run_repo_compile(
        self,
        source_folder: str,
        mvn_args: list[str] | None = None,
    ) -> subprocess.CompletedProcess:
        """Run `mvn -DskipTests compile` inside the tester container for one repo snapshot."""
        container_folder = self._host_path_to_container_path(source_folder)
        cmd = [
            "docker", "exec", self.JAVA_TESTER_CONTAINER,
            "sh", "-lc",
            f"cd {container_folder} && mvn -B -ntp -DskipTests compile {' '.join(mvn_args or [])}".strip(),
        ]
        print(f"  [docker] exec: {' '.join(cmd)}")
        proc = self.run_command(cmd, timeout=self.COMPILE_TIMEOUT)
        self._log_process_output(proc)
        return proc

    def run_repo_compile_for_iteration(
        self,
        iteration: int,
        workspace,
        mvn_args: list[str] | None = None,
    ) -> subprocess.CompletedProcess:
        """Convenience wrapper for compile-only execution on one workspace iteration snapshot."""
        return self.run_repo_compile(
            source_folder=workspace.iteration_path(iteration),
            mvn_args=mvn_args,
        )

    def run_sonar_scanner(self, source_folder: str, project_key: str, token: str) -> subprocess.CompletedProcess:
        """Run sonar-scanner inside the long-lived tester container."""
        container_folder = self._host_path_to_container_path(source_folder)
        cmd = [
            "docker", "exec", self.JAVA_TESTER_CONTAINER,
            "/app/run_sonar_scan.sh",
            container_folder,
            project_key,
            token,
            "http://sonarqube:9000",
        ]
        print(f"  [docker] run: {' '.join(cmd)}")
        proc = self.run_command(cmd, timeout=self.SONAR_TIMEOUT)
        self._log_process_output(proc)
        return proc

    @staticmethod
    def _log_process_output(proc: subprocess.CompletedProcess) -> None:
        if proc.stdout.strip():
            print(proc.stdout.strip())
        if proc.stderr.strip():
            print(f"  [docker stderr] {proc.stderr.strip()}")

    def _host_path_to_container_path(self, source_folder: str) -> str:
        abs_folder = os.path.abspath(source_folder)
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        workspace_root = os.path.join(repo_root, "workspace")
        cloned_repo_root = os.path.join(repo_root, "cloned_repo")

        if abs_folder == workspace_root or abs_folder.startswith(workspace_root + os.sep):
            relative = os.path.relpath(abs_folder, workspace_root)
            if relative == ".":
                return self.CONTAINER_WORKSPACE_ROOT
            return f"{self.CONTAINER_WORKSPACE_ROOT}/{relative.replace(os.sep, '/')}"

        if abs_folder == cloned_repo_root or abs_folder.startswith(cloned_repo_root + os.sep):
            relative = os.path.relpath(abs_folder, cloned_repo_root)
            if relative == ".":
                return self.CONTAINER_CLONED_REPO_ROOT
            return f"{self.CONTAINER_CLONED_REPO_ROOT}/{relative.replace(os.sep, '/')}"

        raise ValueError(
            f"Cannot map host path to tester container volume: {source_folder}"
        )
