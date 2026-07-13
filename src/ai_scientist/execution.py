from __future__ import annotations

import ast
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .schemas import ExecutionResult, ExperimentorOutput


class UnsafeExperimentError(RuntimeError):
    pass


class LocalExperimentRunner:
    """Small local runner. Use an OS/container sandbox for untrusted production use."""

    blocked_import_roots = {
        "ctypes",
        "http",
        "multiprocessing",
        "os",
        "requests",
        "shutil",
        "socket",
        "subprocess",
        "urllib",
    }

    def __init__(self, root: Path, *, timeout_seconds: int, enabled: bool) -> None:
        self.root = root
        self.timeout_seconds = timeout_seconds
        self.enabled = enabled

    def run(
        self,
        value: ExperimentorOutput,
        *,
        round_number: int,
        input_files: dict[str, str] | None = None,
    ) -> ExecutionResult:
        workspace = self._safe_path(
            self.root,
            f"{value.hypothesis_id}/round-{round_number}",
        )
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        combined = ""
        for generated in value.files:
            target = self._safe_path(workspace, generated.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(generated.content, encoding="utf-8")
            combined += f"\n--- {generated.path} ---\n{generated.content}"
            if target.suffix == ".py":
                self._validate_python(generated.content, generated.path)
        injected_paths: set[Path] = set()
        for relative, content in (input_files or {}).items():
            target = self._safe_path(workspace, relative)
            if target.exists():
                raise UnsafeExperimentError(
                    f"Generated output conflicts with injected input: {relative}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            # Preserve the exact bytes represented by ``content`` on every host.
            # TextIO's platform-default newline conversion changes LF to CRLF on
            # Windows, which invalidates an integrity hash computed before the
            # file enters the experiment workspace.
            target.write_text(content, encoding="utf-8", newline="")
            injected_paths.add(target)
        code_hash = hashlib.sha256(combined.encode("utf-8")).hexdigest()
        if not self.enabled:
            return ExecutionResult(
                hypothesis_id=value.hypothesis_id,
                experiment_id=value.experiment_id,
                exit_code=126,
                stdout="",
                stderr="Code execution disabled. Enable AISCI_ALLOW_CODE_EXECUTION or --execute-code.",
                output_files={},
                result_ids=[],
                code_hash=code_hash,
                workspace=str(workspace),
            )
        entrypoint = self._safe_path(workspace, value.entrypoint)
        env = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "TEMP": str(workspace / "tmp"),
            "TMP": str(workspace / "tmp"),
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
        }
        (workspace / "tmp").mkdir(exist_ok=True)
        try:
            completed = subprocess.run(
                [sys.executable, "-I", str(entrypoint)],
                cwd=workspace,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds or None,
                check=False,
            )
            exit_code = completed.returncode
            stdout = completed.stdout[-50_000:]
            stderr = completed.stderr[-50_000:]
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            stdout = (exc.stdout or "")[-50_000:]
            stderr = (exc.stderr or "")[-50_000:]
            timed_out = True
        outputs: dict[str, str] = {}
        for path in workspace.rglob("*"):
            if (
                not path.is_file()
                or path == entrypoint
                or path in injected_paths
                or path.stat().st_size > 1_000_000
            ):
                continue
            relative = path.relative_to(workspace).as_posix()
            try:
                outputs[relative] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                outputs[relative] = "<binary output>"
        return ExecutionResult(
            hypothesis_id=value.hypothesis_id,
            experiment_id=value.experiment_id,
            exit_code=exit_code,
            timed_out=timed_out,
            stdout=stdout,
            stderr=stderr,
            output_files=outputs,
            result_ids=[
                f"{value.experiment_id}:{relative}" for relative in sorted(outputs)
            ],
            code_hash=code_hash,
            workspace=str(workspace),
        )

    @staticmethod
    def _safe_path(root: Path, relative: str) -> Path:
        candidate = (root / relative).resolve()
        resolved_root = root.resolve()
        if candidate != resolved_root and resolved_root not in candidate.parents:
            raise UnsafeExperimentError(f"Path escapes experiment workspace: {relative}")
        return candidate

    def _validate_python(self, content: str, path: str) -> None:
        try:
            tree = ast.parse(content, filename=path)
        except SyntaxError as exc:
            raise UnsafeExperimentError(f"Invalid Python in {path}: {exc}") from exc
        for node in ast.walk(tree):
            roots: list[str] = []
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots = [node.module.split(".")[0]]
            blocked = self.blocked_import_roots.intersection(roots)
            if blocked:
                raise UnsafeExperimentError(
                    f"Blocked import(s) in {path}: {', '.join(sorted(blocked))}"
                )
