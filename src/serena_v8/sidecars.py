"""Optional, workspace-scoped CGC and ast-grep sidecar adapters.

The live Serena/LSP path remains the source of truth. Sidecars are invoked as
bounded subprocesses and never own Workspace state. CGC databases are keyed by
the canonical Workspace path and stored outside the repository.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
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
    cgc_bin: str = "cgc"
    cgc_database: str = "kuzudb"
    cgc_db_path: str = ""
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
            max_output = int(env.get("SERENA_V8_SIDECAR_MAX_OUTPUT_BYTES", str(5 * 1024 * 1024)))
        except ValueError as exc:
            raise ValueError("sidecar numeric settings must be integers") from exc
        timeout_ms = max(100, min(timeout_ms, 30_000))
        max_output = max(1024, min(max_output, 50 * 1024 * 1024))
        db_root = Path(env.get("SERENA_V8_CGC_DB_ROOT", str(Path.home() / ".serena-v8" / "cgc"))).expanduser()
        workspace_id = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:24]
        command = env.get("SERENA_V8_CGC_COMMAND", "").strip()
        return cls(
            workspace_root=str(root),
            ast_grep_bin=env.get("SERENA_V8_AST_GREP_BIN", "ast-grep"),
            cgc_bin=env.get("SERENA_V8_CGC_BIN", "cgc"),
            cgc_database=env.get("SERENA_V8_CGC_DATABASE", "kuzudb"),
            cgc_db_path=str((db_root / workspace_id).resolve()),
            cgc_command=tuple(shlex.split(command)) if command else (),
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
        command = (self.config.ast_grep_bin, "run", "--pattern", pattern, "--lang", language, str(target))
        return self._run(SidecarKind.AST_GREP, command)

    def cgc_index(self, force: bool = False, path: str = ".") -> SidecarResult:
        target = self._safe_path(path)
        command = self._cgc_prefix() + ("index", str(target), "--no-progress")
        if force:
            command += ("--force",)
        return self._run(SidecarKind.CGC, command)

    def cgc_callers(self, function: str, path: str | None = None) -> SidecarResult:
        return self._cgc_relationship("callers", function, path)

    def cgc_callees(self, function: str, path: str | None = None) -> SidecarResult:
        return self._cgc_relationship("calls", function, path)

    def cgc_query(self, query: str) -> SidecarResult:
        if not query.strip():
            raise ValueError("query must not be empty")
        if self.config.cgc_command:
            command = tuple(part.replace("{workspace_root}", self.config.workspace_root) for part in self.config.cgc_command) + (query,)
        else:
            command = self._cgc_prefix() + ("query", query)
        return self._run(SidecarKind.CGC, command)

    def _cgc_relationship(self, operation: str, function: str, path: str | None) -> SidecarResult:
        if not function.strip():
            raise ValueError("function must not be empty")
        command = self._cgc_prefix() + ("analyze", operation, function)
        if path:
            command += ("--file", str(self._safe_path(path)))
        return self._run(SidecarKind.CGC, command)

    def _cgc_prefix(self) -> tuple[str, ...]:
        return (self.config.cgc_bin, "--database", self.config.cgc_database, "--path", self.config.cgc_db_path)

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
            status, error, stdout, stderr = SidecarStatus.UNAVAILABLE, f"{kind.value} executable not found", "", ""
        except TimeoutError as exc:
            status, error, stdout, stderr = SidecarStatus.TIMEOUT, str(exc) or "sidecar request timed out", "", ""
        except OSError as exc:
            status, error, stdout, stderr = SidecarStatus.FAILED, f"could not start sidecar: {exc}", "", ""
        else:
            status = SidecarStatus.OK if returncode == 0 else SidecarStatus.FAILED
            error = "" if returncode == 0 else f"sidecar exited with code {returncode}"
        return SidecarResult(kind, status, self.config.workspace_root, stdout[:self.config.max_output_bytes], stderr[:self.config.max_output_bytes], error, (time.monotonic() - started) * 1000, self.config.timeout_ms)

    @staticmethod
    def _execute(command: Sequence[str], *, cwd: str, timeout: float, max_output_bytes: int) -> tuple[int, str, str]:
        try:
            completed = subprocess.run(list(command), cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"sidecar exceeded {timeout * 1000:.0f} ms deadline") from exc
        return completed.returncode, completed.stdout[:max_output_bytes], completed.stderr[:max_output_bytes]


class WorkspaceCgcIndexer:
    """One bounded CGC index worker and job registry per Workspace."""

    def __init__(self, runner: SidecarRunner) -> None:
        self.runner = runner
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="serena-v8-cgc")
        self._jobs: dict[str, Future[SidecarResult]] = {}
        self._lock = threading.RLock()

    def submit(self, *, path: str = ".", force: bool = False) -> str:
        self.runner._safe_path(path)
        job_id = uuid.uuid4().hex
        with self._lock:
            self._jobs[job_id] = self._executor.submit(self.runner.cgc_index, force, path)
        return job_id

    def status(self, job_id: str) -> dict[str, object]:
        with self._lock:
            future = self._jobs.get(job_id)
        if future is None:
            raise KeyError(f"unknown CGC index job: {job_id}")
        if not future.done():
            state = "running" if future.running() else "queued"
            return {"job_id": job_id, "state": state, "workspace_root": self.runner.config.workspace_root}
        result = future.result()
        return {
            "job_id": job_id,
            "state": "completed" if result.status == SidecarStatus.OK else "failed",
            "workspace_root": self.runner.config.workspace_root,
            "result": result.to_dict(),
        }

    def wait(self, job_id: str, timeout: float | None = None) -> dict[str, object]:
        with self._lock:
            future = self._jobs.get(job_id)
        if future is None:
            raise KeyError(f"unknown CGC index job: {job_id}")
        future.result(timeout=timeout)
        return self.status(job_id)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


_indexers: dict[str, WorkspaceCgcIndexer] = {}
_indexers_lock = threading.Lock()


def indexer_for_workspace(workspace_root: str) -> WorkspaceCgcIndexer:
    root = str(Path(workspace_root).expanduser().resolve(strict=True))
    with _indexers_lock:
        indexer = _indexers.get(root)
        if indexer is None:
            indexer = WorkspaceCgcIndexer(runner_for_workspace(root))
            _indexers[root] = indexer
        return indexer


def runner_for_workspace(workspace_root: str) -> SidecarRunner:
    return SidecarRunner(SidecarConfig.from_environment(workspace_root))


def result_json(result: SidecarResult) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)
