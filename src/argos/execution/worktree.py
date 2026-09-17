"""Local Git worktrees and immutable code snapshots, independent of agent claims."""

import os
import subprocess
from pathlib import Path


class WorktreeManager:
    def __init__(self, repository: Path):
        self.repository = repository.resolve()

    def git(self, cwd: Path, *args: str, index: Path | None = None) -> bytes:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(
            GIT_TERMINAL_PROMPT="0",
            GIT_AUTHOR_NAME="ARGOS",
            GIT_AUTHOR_EMAIL="argos@localhost",
            GIT_COMMITTER_NAME="ARGOS",
            GIT_COMMITTER_EMAIL="argos@localhost",
        )
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(cwd), *args],
            env=env,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors="replace").strip())
        return result.stdout

    def source_commit(self) -> str:
        return self.git(self.repository, "rev-parse", "HEAD^{commit}").decode().strip()

    def create(self, workspace: Path, commit: str) -> None:
        self.git(self.repository, "worktree", "add", "--detach", str(workspace), commit)

    def snapshot(self, workspace: Path, source: str, evidence: Path) -> tuple[str, list[str]]:
        # Separate index includes untracked/ignored implementation files without
        # trusting the backend's staging area or HEAD (the backend must not commit).
        index = evidence / "snapshot.index"
        self.git(workspace, "read-tree", source, index=index)
        self.git(
            workspace, "add", "--all", "--force", "--", ".", ":(exclude).argos-coding", index=index
        )
        diff = self.git(
            workspace, "diff", "--cached", "--binary", "--no-ext-diff", source, "--", index=index
        )
        (evidence / "code.diff").write_bytes(diff)
        names = self.git(
            workspace, "diff", "--cached", "--name-only", "-z", source, "--", index=index
        )
        tree = self.git(workspace, "write-tree", index=index).decode().strip()
        return tree, [p.decode() for p in names.split(b"\0") if p]

    def record_revision(self, workspace: Path, source: str, tree: str) -> str:
        original = self.git(workspace, "rev-parse", f"{source}^{{tree}}").decode().strip()
        self.git(workspace, "read-tree", tree)
        if tree == original:
            return source
        commit = (
            self.git(
                workspace,
                "commit-tree",
                tree,
                "-p",
                source,
                "-m",
                "ARGOS experiment implementation",
            )
            .decode()
            .strip()
        )
        self.git(workspace, "update-ref", "HEAD", commit)
        self.git(workspace, "update-ref", f"refs/argos/runs/{workspace.parent.name}", commit)
        return commit

    def cleanup(self, workspace: Path, evidence: Path) -> None:
        # Cleanup is opt-in and only after a terminal manifest, patch and logs exist.
        import json

        from argos.protocols import ExperimentResult

        if evidence.resolve().is_relative_to(workspace.resolve()):
            raise ValueError("Evidence cannot be inside the worktree being removed")
        if (evidence / "preservation-error.log").exists():
            raise ValueError("Cannot clean up after incomplete evidence preservation")
        result = ExperimentResult.model_validate(json.loads((evidence / "result.json").read_text()))
        if Path(result.worktree).resolve() != workspace.resolve():
            raise ValueError("Result does not belong to this worktree")
        for path in [result.diff_path, result.stdout_path, result.stderr_path, *result.artifacts]:
            item = Path(path).resolve()
            if not item.is_relative_to(evidence.resolve()) or not item.is_file():
                raise ValueError("Evidence must be retained outside the worktree before cleanup")
        self.git(self.repository, "worktree", "remove", "--force", str(workspace))
