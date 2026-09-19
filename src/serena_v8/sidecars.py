"""Optional, workspace-scoped CGC and ast-grep sidecar adapters.

The live Serena/LSP path remains the source of truth. Sidecars are invoked as
bounded subprocesses and never own Workspace state. CGC databases are keyed by
the canonical Workspace path and stored outside the repository.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    cgc_query_timeout_ms: int = 5000
    cgc_incremental_index_timeout_ms: int = 10000
    cgc_full_index_timeout_ms: int = 120000
    max_output_bytes: int = 5 * 1024 * 1024

    @classmethod
    def from_environment(
        cls, workspace_root: str, environ: Mapping[str, str] | None = None
    ) -> "SidecarConfig":
        env = os.environ if environ is None else environ
        root = Path(workspace_root).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError(f"Workspace root is not a directory: {root}")
        def timeout_value(name: str, short_name: str, default: int, maximum: int = 600_000) -> int:
            raw = env.get(name, env.get(short_name, str(default)))
            try:
                return max(100, min(int(raw), maximum))
            except ValueError as exc:
                raise ValueError(f"{name} must be an integer") from exc

        timeout_ms = timeout_value("SERENA_V8_SIDECAR_TIMEOUT_MS", "SIDECAR_TIMEOUT_MS", 5000, 30_000)
        query_timeout_ms = timeout_value("SERENA_V8_CGC_QUERY_TIMEOUT_MS", "CGC_QUERY_TIMEOUT_MS", 5000)
        incremental_timeout_ms = timeout_value(
            "SERENA_V8_CGC_INCREMENTAL_INDEX_TIMEOUT_MS", "CGC_INCREMENTAL_INDEX_TIMEOUT_MS", 10_000
        )
        full_timeout_ms = timeout_value("SERENA_V8_CGC_FULL_INDEX_TIMEOUT_MS", "CGC_FULL_INDEX_TIMEOUT_MS", 120_000)
        try:
            max_output = int(env.get("SERENA_V8_SIDECAR_MAX_OUTPUT_BYTES", str(5 * 1024 * 1024)))
        except ValueError as exc:
            raise ValueError("SERENA_V8_SIDECAR_MAX_OUTPUT_BYTES must be an integer") from exc
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
            cgc_query_timeout_ms=query_timeout_ms,
            cgc_incremental_index_timeout_ms=incremental_timeout_ms,
            cgc_full_index_timeout_ms=full_timeout_ms,
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
    metrics: dict[str, int | float] = field(default_factory=dict)

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
            "metrics": self.metrics,
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

    def ast_grep_rewrite(
        self, pattern: str, rewrite: str, language: str, path: str = ".", apply: bool = False
    ) -> SidecarResult:
        if not pattern.strip() or not language.strip():
            raise ValueError("pattern and language must not be empty")
        target = self._safe_path(path)
        command = [
            self.config.ast_grep_bin, "run", "--pattern", pattern, "--rewrite", rewrite,
            "--lang", language,
        ]
        if apply:
            command.append("--update-all")
        else:
            command.append("--json=compact")
        command.append(str(target))
        return self._run(SidecarKind.AST_GREP, tuple(command))

    def cgc_index(self, force: bool = False, path: str = ".", db_path: str | None = None) -> SidecarResult:
        target = self._safe_path(path)
        command = self._cgc_prefix(db_path) + ("index", str(target), "--no-progress")
        if force:
            command += ("--force",)
        timeout_ms = self.config.cgc_full_index_timeout_ms if target == Path(self.config.workspace_root) else self.config.cgc_incremental_index_timeout_ms
        return self._run(SidecarKind.CGC, command, timeout_ms=timeout_ms)

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
        return self._run(SidecarKind.CGC, command, timeout_ms=self.config.cgc_query_timeout_ms)

    def _cgc_relationship(self, operation: str, function: str, path: str | None) -> SidecarResult:
        if not function.strip():
            raise ValueError("function must not be empty")
        command = self._cgc_prefix() + ("analyze", operation, function)
        if path:
            command += ("--file", str(self._safe_path(path)))
        return self._run(SidecarKind.CGC, command, timeout_ms=self.config.cgc_query_timeout_ms)

    def _cgc_prefix(self, db_path: str | None = None) -> tuple[str, ...]:
        return (self.config.cgc_bin, "--database", self.config.cgc_database, "--path", db_path or self.config.cgc_db_path)

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

    def _run(self, kind: SidecarKind, command: tuple[str, ...], timeout_ms: int | None = None) -> SidecarResult:
        import time

        deadline_ms = timeout_ms or self.config.timeout_ms
        self.last_command = command
        self.last_cwd = self.config.workspace_root
        started = time.monotonic()
        try:
            returncode, stdout, stderr = self._executor(
                command,
                cwd=self.config.workspace_root,
                timeout=deadline_ms / 1000,
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
        metrics = self._parse_cgc_metrics(stdout) if kind == SidecarKind.CGC else {}
        return SidecarResult(
            kind, status, self.config.workspace_root, stdout[:self.config.max_output_bytes],
            stderr[:self.config.max_output_bytes], error, (time.monotonic() - started) * 1000,
            deadline_ms, metrics,
        )

    @staticmethod
    def _parse_cgc_metrics(stdout: str) -> dict[str, int | float]:
        patterns = {
            "scanned_files": r"Total scanned files\s*[|:]\s*([0-9,]+)",
            "function_nodes": r"Function nodes\s*[|:]\s*([0-9,]+)",
            "class_nodes": r"Class nodes\s*[|:]\s*([0-9,]+)",
            "calls_edges": r"CALLS edges\s*[|:]\s*([0-9,]+)",
            "serialization_seconds": r"Serialization seconds\s*[|:]\s*([0-9.]+)",
        }
        metrics: dict[str, int | float] = {}
        for key, pattern in patterns.items():
            match = re.search(pattern, stdout, re.IGNORECASE)
            if match:
                value = match.group(1).replace(",", "")
                metrics[key] = float(value) if "." in value else int(value)
        return metrics

    @staticmethod
    def _execute(command: Sequence[str], *, cwd: str, timeout: float, max_output_bytes: int) -> tuple[int, str, str]:
        process = subprocess.Popen(
            list(command), cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
            raise TimeoutError(f"sidecar exceeded {timeout * 1000:.0f} ms deadline") from exc
        return process.returncode, stdout[:max_output_bytes], stderr[:max_output_bytes]


class WorkspaceCgcIndexer:
    """One bounded CGC index worker and job registry per Workspace."""

    def __init__(self, runner: SidecarRunner) -> None:
        self.runner = runner
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="serena-v8-cgc")
        self._jobs: dict[str, Future[SidecarResult]] = {}
        self._indexed_snapshots: dict[str, dict[str, tuple[int, int]]] = {}
        self._job_paths: dict[str, str] = {}
        self._job_meta: dict[str, dict[str, object]] = {}
        self._lock = threading.RLock()

    def submit(self, *, path: str = ".", force: bool = False) -> str:
        self.runner._safe_path(path)
        job_id = uuid.uuid4().hex
        with self._lock:
            self._job_paths[job_id] = str(self.runner._safe_path(path))
            self._job_meta[job_id] = {"submitted_at": datetime.now(timezone.utc).isoformat()}
            future = self._executor.submit(self._run_job, job_id, force, path)
            self._jobs[job_id] = future
            future.add_done_callback(lambda done: self._record_snapshot(job_id, done))
        return job_id

    def _run_job(self, job_id: str, force: bool, path: str) -> SidecarResult:
        with self._lock:
            self._job_meta[job_id]["started_at"] = datetime.now(timezone.utc).isoformat()
        target = self.runner._safe_path(path)
        staging: Path | None = None
        try:
            if target == Path(self.runner.config.workspace_root):
                staging = self._staging_db_path(job_id)
                shutil.rmtree(staging, ignore_errors=True)
                result = self.runner.cgc_index(force, path, db_path=str(staging))
                if result.status == SidecarStatus.OK:
                    self._promote_staging(staging)
                else:
                    shutil.rmtree(staging, ignore_errors=True)
            else:
                result = self.runner.cgc_index(force, path)
        except Exception as exc:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
            result = SidecarResult(
                SidecarKind.CGC, SidecarStatus.FAILED, self.runner.config.workspace_root,
                error=f"CGC index promotion failed: {exc}", timeout_ms=self.runner.config.cgc_full_index_timeout_ms,
            )
        with self._lock:
            self._job_meta[job_id]["completed_at"] = datetime.now(timezone.utc).isoformat()
            self._job_meta[job_id]["duration_ms"] = round(result.duration_ms, 3)
            self._job_meta[job_id]["graph_mode"] = "staging_promote" if staging is not None else "in_place"
        return result

    def _staging_db_path(self, job_id: str) -> Path:
        active = Path(self.runner.config.cgc_db_path)
        return active.parent / f".{active.name}.staging-{job_id}"

    def _promote_staging(self, staging: Path) -> None:
        active = Path(self.runner.config.cgc_db_path)
        active.parent.mkdir(parents=True, exist_ok=True)
        previous = active.parent / f".{active.name}.previous-{uuid.uuid4().hex}"
        if active.exists():
            os.replace(active, previous)
        try:
            os.replace(staging, active)
        except Exception:
            if previous.exists() and not active.exists():
                os.replace(previous, active)
            raise
        finally:
            shutil.rmtree(previous, ignore_errors=True)

    def _record_snapshot(self, job_id: str, future: Future[SidecarResult]) -> None:
        try:
            result = future.result()
        except Exception:
            return
        if result.status == SidecarStatus.OK:
            with self._lock:
                target = self._job_paths[job_id]
                self._indexed_snapshots[target] = self._snapshot(target)

    def _snapshot(self, target: str) -> dict[str, tuple[int, int]]:
        root = Path(self.runner.config.workspace_root)
        path = Path(target)
        files: list[Path] = []
        if path.is_file():
            files = [path]
        elif path.is_dir():
            for current, dirs, names in os.walk(path, followlinks=False):
                dirs[:] = [d for d in dirs if d not in {".git", "node_modules", ".next", ".next-build", "dist", "build"}]
                files.extend(Path(current) / name for name in names)
        snapshot: dict[str, tuple[int, int]] = {}
        for file_path in files:
            try:
                stat = file_path.stat()
            except OSError:
                continue
            snapshot[str(file_path.relative_to(root))] = (stat.st_mtime_ns, stat.st_size)
        return snapshot

    def stale_paths(self) -> list[str]:
        with self._lock:
            snapshots = list(self._indexed_snapshots.items())
        stale: set[str] = set()
        for target, previous in snapshots:
            current = self._snapshot(target)
            stale.update(set(previous) ^ set(current))
            stale.update(path for path in set(previous) & set(current) if previous[path] != current[path])
        return sorted(stale)

    def status(self, job_id: str) -> dict[str, object]:
        with self._lock:
            future = self._jobs.get(job_id)
            metadata = dict(self._job_meta.get(job_id, {}))
        if future is None:
            raise KeyError(f"unknown CGC index job: {job_id}")
        if not future.done():
            state = "running" if future.running() else "queued"
            return {"job_id": job_id, "state": state, "workspace_root": self.runner.config.workspace_root, **metadata}
        result = future.result()
        return {
            "job_id": job_id,
            "state": "completed" if result.status == SidecarStatus.OK else "failed",
            "workspace_root": self.runner.config.workspace_root,
            **metadata,
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
