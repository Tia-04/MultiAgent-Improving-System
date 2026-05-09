"""
services/workspace_manager.py
-----------------------------
Manages iteration snapshots for the repo-based experiment.

Each iteration folder is a self-contained, testable copy of the target repo:

  workspace/
    iteration_0/
      pom.xml
      src/main/java/...
      src/test/java/...
      ...
    iteration_1/
      ...same tree, with edits applied from a diff

This layout keeps each attempt reproducible and makes it straightforward to run
Maven tests and Sonar analysis against one specific iteration directory.
"""

import json
import os
import shutil
from pathlib import Path


class WorkspaceManager:
    def __init__(self, base_dir: str = "workspace"):
        self.base_dir = os.path.abspath(base_dir)
        self.source_repo = None
        os.makedirs(self.base_dir, exist_ok=True)

    def set_source_repo(self, repo_path: str) -> None:
        """Register the repo that will seed iteration_0."""
        resolved = os.path.abspath(repo_path)
        if not os.path.isdir(resolved):
            raise ValueError(f"Source repo does not exist: {repo_path}")
        self.source_repo = resolved

    def iteration_relpath(self, iteration: int) -> str:
        """Container-friendly relative path for one iteration folder."""
        return f"iteration_{iteration}"

    def iteration_path(self, iteration: int) -> str:
        """Absolute host path for one iteration folder."""
        return os.path.join(self.base_dir, self.iteration_relpath(iteration))

    def bootstrap_iteration(self, iteration: int = 0) -> str:
        """
        Create iteration_0 from the source repo.

        If the folder already exists, it is left in place and returned.
        """
        if self.source_repo is None:
            raise ValueError("Workspace source_repo is not set.")

        destination = self.iteration_path(iteration)
        if os.path.isdir(destination):
            return destination

        shutil.copytree(
            self.source_repo,
            destination,
            ignore=shutil.ignore_patterns(".git", "target", ".idea", ".vscode", "*.iml"),
        )
        print(f"  [workspace] bootstrapped {destination}")
        return destination

    def create_iteration_from_previous(self, iteration: int) -> str:
        """
        Create an iteration by copying the previous iteration snapshot.

        Example: iteration_1 is copied from iteration_0 before applying a diff.
        """
        if iteration <= 0:
            return self.bootstrap_iteration(0)

        previous = self.iteration_path(iteration - 1)
        current = self.iteration_path(iteration)

        if not os.path.isdir(previous):
            raise ValueError(f"Previous iteration does not exist: {previous}")
        if os.path.isdir(current):
            return current

        shutil.copytree(
            previous,
            current,
            ignore=shutil.ignore_patterns("target", "*.diff", "*.patch"),
        )
        print(f"  [workspace] copied {previous} -> {current}")
        return current

    def write_json_artifact(self, payload: dict | list, iteration: int, filename: str = "changes.json") -> str:
        """Persist a JSON artifact inside the iteration folder."""
        folder = self.iteration_path(iteration)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, filename)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
        print(f"  [workspace] wrote artifact {path}")
        return path

    def apply_files(self, edited_files: list[dict], iteration: int) -> str:
        """
        Create the iteration snapshot and overwrite files with model-produced contents.

        Returns the absolute path of the updated iteration folder.
        """
        folder = self.create_iteration_from_previous(iteration)
        root = Path(folder)
        for item in edited_files:
            rel_path = str(item.get("path", "")).replace("\\", "/")
            content = item.get("content")
            if not rel_path or not isinstance(content, str):
                raise RuntimeError(f"Invalid edited file payload: {item}")
            target_path = root / rel_path
            if not target_path.is_file():
                raise RuntimeError(f"Cannot overwrite missing file: {rel_path}")
            target_path.write_text(content, encoding="utf-8", newline="\n")

        print(f"  [workspace] applied file updates in {folder}")
        return folder

    def restore_file(self, iteration: int, rel_path: str, content: str) -> str:
        """Restore one repo-relative file inside an existing iteration snapshot."""
        folder = self.iteration_path(iteration)
        root = Path(folder)
        target_path = root / rel_path.replace("\\", "/")
        if not target_path.is_file():
            raise RuntimeError(f"Cannot restore missing file: {rel_path}")
        target_path.write_text(content, encoding="utf-8", newline="\n")
        print(f"  [workspace] restored {rel_path} in {folder}")
        return str(target_path)

    def collect_context_files(
        self,
        iteration: int,
        relative_paths: list[str],
    ) -> list[dict]:
        """Read a selected set of repo-relative files from one iteration."""
        root = Path(self.iteration_path(iteration))
        items = []
        for rel_path in relative_paths:
            file_path = root / rel_path
            if not file_path.is_file():
                raise ValueError(f"Context file not found in iteration_{iteration}: {rel_path}")
            items.append(
                {
                    "path": rel_path.replace("\\", "/"),
                    "content": file_path.read_text(encoding="utf-8"),
                }
            )
        return items

    def list_java_files(self, iteration: int, subdir: str = "src/main/java") -> list[str]:
        """List repo-relative Java files under one source subtree."""
        root = Path(self.iteration_path(iteration))
        source_root = root / subdir
        if not source_root.is_dir():
            return []
        return sorted(
            str(path.relative_to(root)).replace("\\", "/")
            for path in source_root.rglob("*.java")
        )
