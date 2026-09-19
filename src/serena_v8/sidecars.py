"""Optional, workspace-scoped CGC and ast-grep sidecar adapters.

The live Serena/LSP path remains the source of truth. Sidecars are invoked as
bounded, read-only subprocesses and never own Workspace state.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable, Mapping, Sequence


class SidecarKind(StrEnum):
    AST_GREP = "ast-grep"
    CGC = "cgc"


class SidecarStatus(StrEnum):
    OK = "ok"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class SidecarConfig:
    workspace_root: str
    ast_grep_bin: str = "ast-grep"
    cgc_command: tuple[str, ...] = ()
    timeout_ms: int = 5000
    max_output_bytes: int = 5 * 1024 * 1024

    @classmethod
    def from_environment(
        cls, workspace_root: str, environ: Mapping[str, str] | None = None
    ) -> "SidecarConfig":
        env = os.environ if environ is None else environ
        root = Path(workspace_root).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError(f"Workspace root is not a directory: {root}")
        try:
            timeout_ms = int(env.get("SERENA_V8_SIDECAR_TIMEOUT_MS", "5000"))
        except ValueError as exc:
            raise ValueError("SERENA_V8_SIDECAR_TIMEOUT_MS must be an integer") from exc
        timeout_ms = max(100, min(timeout_ms, 30_000))
        try:
            max_output = int(env.get("SERENA_V8_SIDECAR_MAX_OUTPUT_BYTES", str(5 * 1024 * 1024)))
        except ValueError as exc:
            raise ValueError("SERENA_V8_SIDECAR_MAX_OUTPUT_BYTES must be an integer") from exc
        max_output = max(1024, min(max_output, 50 * 1024 * 1024))
        command = env.get("SERENA_V8_CGC_COMMAND", "").strip()
        cgc_command = tuple(shlex.split(command)) if command else ()
        return cls(
            workspace_root=str(root),
            ast_grep_bin=env.get("SERENA_V8_AST_GREP_BIN", "ast-grep"),
            cgc_command=cgc_command,
            timeout_ms=timeout_ms,
            max_output_bytes=max_output,
        )


@dataclass(frozen=True)
class SidecarResult:
    kind: SidecarKind
    status: SidecarStatus
    workspace_root: str
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    duration_ms: float = 0.0
    timeout_ms: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "status": self.status.value,
            "workspace_root": self.workspace_root,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 3),
            "timeout_ms": self.timeout_ms,
        }


Executor = Callable[..., tuple[int, str, str]]


class SidecarRunner:
    """Run optional sidecars with fixed Workspace scope and bounded resources."""

    def __init__(self, config: SidecarConfig, executor: Executor | None = None) -> None:
        self.config = config
        self._executor = executor or self._execute
        self.last_command: tuple[str, ...] = ()
        self.last_cwd: str | None = None

    def ast_grep_search(self, pattern: str, language: str, path: str = ".") -> SidecarResult:
        if not pattern.strip():
            raise ValueError("pattern must not be empty")
        if not language.strip():
            raise ValueError("language must not be empty")
        target = self._safe_path(path)
        command = (
            self.config.ast_grep_bin,
            "run",
            "--pattern",
            pattern,
            "--lang",
            language,
            str(target),
        )
        return self._run(SidecarKind.AST_GREP, command)

    def cgc_query(self, query: str) -> SidecarResult:
        if not query.strip():
            raise ValueError("query must not be empty")
        if not self.config.cgc_command:
            return SidecarResult(
                kind=SidecarKind.CGC,
                status=SidecarStatus.UNAVAILABLE,
                workspace_root=self.config.workspace_root,
                error="SERENA_V8_CGC_COMMAND is not configured",
                timeout_ms=self.config.timeout_ms,
            )
        command = tuple(
            part.replace("{workspace_root}", self.config.workspace_root)
            for part in self.config.cgc_command
        ) + (query,)
        return self._run(SidecarKind.CGC, command)

    def _safe_path(self, path: str) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = Path(self.config.workspace_root) / candidate
        resolved = candidate.resolve(strict=False)
        root = Path(self.config.workspace_root)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError("sidecar path must remain inside the active Workspace") from exc
        return resolved

    def _run(self, kind: SidecarKind, command: tuple[str, ...]) -> SidecarResult:
        import time

        self.last_command = command
        self.last_cwd = self.config.workspace_root
        started = time.monotonic()
        try:
            returncode, stdout, stderr = self._executor(
                command,
                cwd=self.config.workspace_root,
                timeout=self.config.timeout_ms / 1000,
                max_output_bytes=self.config.max_output_bytes,
            )
        except FileNotFoundError:
            status = SidecarStatus.UNAVAILABLE
            error = f"{kind.value} executable not found"
            stdout = stderr = ""
        except TimeoutError as exc:
            status = SidecarStatus.TIMEOUT
            error = str(exc) or "sidecar request timed out"
            stdout = stderr = ""
        except OSError as exc:
            status = SidecarStatus.FAILED
            error = f"could not start sidecar: {exc}"
            stdout = stderr = ""
        else:
            status = SidecarStatus.OK if returncode == 0 else SidecarStatus.FAILED
            error = "" if returncode == 0 else f"sidecar exited with code {returncode}"
        return SidecarResult(
            kind=kind,
            status=status,
            workspace_root=self.config.workspace_root,
            stdout=stdout[: self.config.max_output_bytes],
            stderr=stderr[: self.config.max_output_bytes],
            error=error,
            duration_ms=(time.monotonic() - started) * 1000,
            timeout_ms=self.config.timeout_ms,
        )

    @staticmethod
    def _execute(command: Sequence[str], *, cwd: str, timeout: float, max_output_bytes: int) -> tuple[int, str, str]:
        try:
            completed = subprocess.run(
                list(command),
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"sidecar exceeded {timeout * 1000:.0f} ms deadline") from exc
        return completed.returncode, completed.stdout[:max_output_bytes], completed.stderr[:max_output_bytes]


def runner_for_workspace(workspace_root: str) -> SidecarRunner:
    """Create a new sidecar runner; no state is shared between Workspaces."""
    return SidecarRunner(SidecarConfig.from_environment(workspace_root))


def result_json(result: SidecarResult) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)
