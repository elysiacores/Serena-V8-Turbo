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
import time
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
    ERROR = "failed"  # compatibility alias used by earlier V8 callers/tests
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
    cgc_query_cache_ttl_ms: int = 5000
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
        query_cache_ttl_ms = timeout_value("SERENA_V8_CGC_QUERY_CACHE_TTL_MS", "CGC_QUERY_CACHE_TTL_MS", 5_000)
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
            cgc_query_cache_ttl_ms=query_cache_ttl_ms,
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

    _query_cache: dict[tuple[str, tuple[str, ...]], tuple[float, SidecarResult]] = {}
    _query_cache_lock = threading.RLock()
    _cache_max_entries = 128
    _cache_max_bytes = 16 * 1024 * 1024

    @classmethod
    def _prune_query_cache(cls) -> None:
        """Called under the cache lock; expiry is stored per entry, not per reader."""
        now = time.monotonic()
        for key, (expires, _) in list(cls._query_cache.items()):
            if expires <= now:
                cls._query_cache.pop(key)
        sizes = {key: len(result.stdout.encode()) + len(result.stderr.encode())
                 for key, (_, result) in cls._query_cache.items()}
        total = sum(sizes.values())
        while cls._query_cache and (len(cls._query_cache) > cls._cache_max_entries or total > cls._cache_max_bytes):
            key = next(iter(cls._query_cache))
            total -= sizes[key]
            cls._query_cache.pop(key)

    def __init__(self, config: SidecarConfig, executor: Executor | None = None) -> None:
        self.config = config
        self._executor = executor or self._execute
        self.last_command: tuple[str, ...] = ()
        self.last_cwd: str | None = None

    @classmethod
    def invalidate_query_cache(cls, workspace_root: str) -> None:
        root = str(Path(workspace_root).resolve())
        with cls._query_cache_lock:
            for key in [key for key in cls._query_cache if key[0] == root]:
                cls._query_cache.pop(key, None)

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
        timeout_ms = self.config.cgc_full_index_timeout_ms if target.is_dir() else self.config.cgc_incremental_index_timeout_ms
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
        cacheable = kind == SidecarKind.CGC and len(command) > 5 and command[5] in {"query", "analyze"}
        cache_key = (str(Path(self.config.workspace_root).resolve()), command)
        if cacheable:
            with self._query_cache_lock:
                self._prune_query_cache()
                cached = self._query_cache.get(cache_key)
                if cached is not None:
                    return cached[1]
                self._query_cache.pop(cache_key, None)
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
        result = SidecarResult(
            kind, status, self.config.workspace_root, stdout[:self.config.max_output_bytes],
            stderr[:self.config.max_output_bytes], error, (time.monotonic() - started) * 1000,
            deadline_ms, metrics,
        )
        if cacheable and result.status == SidecarStatus.OK:
            with self._query_cache_lock:
                self._query_cache[cache_key] = (time.monotonic() + self.config.cgc_query_cache_ttl_ms / 1000, result)
                self._prune_query_cache()
        return result

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
        """Run a sidecar while draining output continuously with bounded memory."""
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            start_new_session=True,
        )
        assert process.stdout is not None
        assert process.stderr is not None

        stdout_buffer = bytearray()
        stderr_buffer = bytearray()

        def drain(pipe, buffer: bytearray) -> None:
            try:
                while True:
                    chunk = pipe.read(64 * 1024)
                    if not chunk:
                        return
                    remaining = max_output_bytes - len(buffer)
                    if remaining > 0:
                        buffer.extend(chunk[:remaining])
            finally:
                pipe.close()

        stdout_thread = threading.Thread(target=drain, args=(process.stdout, stdout_buffer), daemon=True)
        stderr_thread = threading.Thread(target=drain, args=(process.stderr, stderr_buffer), daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            timeout_error = TimeoutError(f"sidecar exceeded {timeout * 1000:.0f} ms deadline")
            timeout_error.__cause__ = exc
        finally:
            stdout_thread.join()
            stderr_thread.join()

        if timed_out:
            raise timeout_error

        return (
            process.returncode,
            stdout_buffer.decode("utf-8", errors="replace"),
            stderr_buffer.decode("utf-8", errors="replace"),
        )


class WorkspaceCgcIndexer:
    """One bounded CGC index worker and job registry per Workspace."""

    _max_pending_jobs = 8  # Includes the running job.
    _max_retained_jobs = 64
    _job_retention_seconds = 3600

    def __init__(self, runner: SidecarRunner) -> None:
        self.runner = runner
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="serena-v8-cgc")
        self._jobs: dict[str, Future[SidecarResult]] = {}
        self._indexed_snapshots: dict[str, dict[str, tuple[int, int, str]]] = {}
        self._dirty_paths: set[str] = set()
        self._job_paths: dict[str, str] = {}
        self._job_meta: dict[str, dict[str, object]] = {}
        self._lock = threading.RLock()

    def _prune_jobs(self, reserve: int = 0) -> None:
        completed = [job for job, future in self._jobs.items() if future.done()]
        cutoff = time.time() - self._job_retention_seconds
        for job in completed:
            timestamp = self._job_meta[job].get("completed_at")
            expired = timestamp is not None and datetime.fromisoformat(str(timestamp)).timestamp() <= cutoff
            if expired or len(self._jobs) > self._max_retained_jobs - reserve:
                self._jobs.pop(job)
                self._job_paths.pop(job)
                self._job_meta.pop(job)

    def submit(self, *, path: str = ".", force: bool = False) -> str:
        job_id = uuid.uuid4().hex
        with self._lock:
            target = str(self.runner._safe_path(path))
            if sum(not future.done() for future in self._jobs.values()) >= self._max_pending_jobs:
                raise RuntimeError("CGC index queue is full; retry after a pending job completes")
            self._prune_jobs(reserve=1)
            self._job_paths[job_id] = target
            self._job_meta[job_id] = {"submitted_at": datetime.now(timezone.utc).isoformat()}
            if not force and self._is_unchanged(target):
                future: Future[SidecarResult] = Future()
                future.set_result(SidecarResult(
                    SidecarKind.CGC,
                    SidecarStatus.OK,
                    self.runner.config.workspace_root,
                    stdout="unchanged; index skipped",
                    metrics={"skipped": 1},
                ))
                self._job_meta[job_id]["state"] = "skipped"
                self._job_meta[job_id]["completed_at"] = datetime.now(timezone.utc).isoformat()
                self._job_meta[job_id]["duration_ms"] = 0.0
            else:
                future = self._executor.submit(self._run_job, job_id, force, path)
            self._jobs[job_id] = future

        return job_id

    def _is_unchanged(self, target: str) -> bool:
        previous = self._indexed_snapshots.get(target)
        relative = Path(target).relative_to(self.runner.config.workspace_root)
        dirty = any(Path(p) == relative or relative in Path(p).parents for p in self._dirty_paths)
        return not dirty and previous is not None and previous == self._snapshot(target)

    def _run_job(self, job_id: str, force: bool, path: str) -> SidecarResult:
        with self._lock:
            self._job_meta[job_id]["started_at"] = datetime.now(timezone.utc).isoformat()
        target = self.runner._safe_path(path)
        staging: Path | None = None
        try:
            before = self._snapshot(str(target))
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
            after = self._snapshot(str(target))
            if result.status == SidecarStatus.OK:
                changed = set(before) ^ set(after)
                changed.update(p for p in before.keys() & after.keys() if before[p] != after[p])
                with self._lock:
                    self._indexed_snapshots[str(target)] = before
                    self._dirty_paths.difference_update(before.keys() | after.keys())
                    self._dirty_paths.update(changed)
                    if changed:
                        self._job_meta[job_id]["state"] = "dirty"
                    self._job_meta[job_id]["changed_during_index"] = sorted(changed)
        except Exception as exc:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
            result = SidecarResult(
                SidecarKind.CGC, SidecarStatus.FAILED, self.runner.config.workspace_root,
                error=f"CGC index promotion failed: {exc}", timeout_ms=self.runner.config.cgc_full_index_timeout_ms,
            )
        if result.status == SidecarStatus.OK:
            SidecarRunner.invalidate_query_cache(self.runner.config.workspace_root)
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


    def _snapshot(self, target: str) -> dict[str, tuple[int, int, str]]:
        root = Path(self.runner.config.workspace_root)
        path = Path(target)
        files: list[Path] = []
        if path.is_file():
            files = [path]
        elif path.is_dir():
            for current, dirs, names in os.walk(path, followlinks=False):
                dirs[:] = [d for d in dirs if d not in {".git", "node_modules", ".next", ".next-build", "dist", "build"}]
                files.extend(Path(current) / name for name in names)
        snapshot: dict[str, tuple[int, int, str]] = {}
        for file_path in files:
            try:
                stat = file_path.stat()
                digest = hashlib.blake2b(digest_size=8)
                with file_path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError:
                continue
            snapshot[str(file_path.relative_to(root))] = (stat.st_mtime_ns, stat.st_size, digest.hexdigest())
        return snapshot

    def stale_paths(self) -> list[str]:
        with self._lock:
            snapshots = list(self._indexed_snapshots.items())
            stale = set(self._dirty_paths)
        for target, previous in snapshots:
            current = self._snapshot(target)
            stale.update(set(previous) ^ set(current))
            stale.update(path for path in set(previous) & set(current) if previous[path] != current[path])
        return sorted(stale)

    def status(self, job_id: str) -> dict[str, object]:
        with self._lock:
            self._prune_jobs()
            future = self._jobs.get(job_id)
            metadata = dict(self._job_meta.get(job_id, {}))
        if future is None:
            raise KeyError(f"unknown CGC index job: {job_id}")
        if not future.done():
            state = "running" if future.running() else "queued"
            return {"job_id": job_id, "state": state, "workspace_root": self.runner.config.workspace_root, **metadata}
        result = future.result()
        state = metadata.get("state") or ("completed" if result.status == SidecarStatus.OK else "failed")
        return {
            "job_id": job_id,
            "state": state,
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
