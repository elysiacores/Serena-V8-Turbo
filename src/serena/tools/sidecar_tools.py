"""Optional read-only MCP tools for Workspace-scoped sidecars."""

import json
from pathlib import Path

from serena.tools.tools_base import Tool, ToolMarkerOptional, ToolMarkerSymbolicRead
from serena_v8.sidecars import indexer_for_workspace, result_json, runner_for_workspace


def _json(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class AstGrepSearchTool(Tool, ToolMarkerOptional, ToolMarkerSymbolicRead):
    """Search the active Workspace structurally with optional ast-grep."""

    def apply(self, pattern: str, language: str, path: str = ".") -> str:
        """Run a read-only structural search inside the active Workspace."""
        return result_json(runner_for_workspace(self.project.project_root).ast_grep_search(pattern, language, path))


class AstGrepRewriteTool(Tool, ToolMarkerOptional):
    """Preview or explicitly apply an ast-grep rewrite with V8 synchronization."""

    def apply(
        self,
        pattern: str,
        rewrite: str,
        language: str,
        path: str,
        approved: bool = False,
    ) -> str:
        """Preview by default; approved file rewrites invalidate cache and notify LSP."""
        root = Path(self.project.project_root).resolve()
        target = (root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
        target.relative_to(root)
        if approved and not target.is_file():
            raise ValueError("approved ast-grep rewrites require a single file path")
        original = target.read_bytes() if approved else None
        result = runner_for_workspace(str(root)).ast_grep_rewrite(pattern, rewrite, language, str(target), approved)
        payload = result.to_dict()
        payload["approved"] = approved
        if approved and result.status.value == "ok":
            relative = str(target.relative_to(root))
            from serena_v8.lsp_sync import get_cache_invalidator, get_lsp_sync
            invalidator = get_cache_invalidator()
            sync = get_lsp_sync()
            assert original is not None
            invalidator.invalidate_file(relative, str(root))
            lsp_ok = sync.notify_changed(relative, str(root))
            payload["lsp_sync"] = lsp_ok
            payload["cache_invalidated"] = True
            if not lsp_ok:
                target.write_bytes(original)
                invalidator.invalidate_file(relative, str(root))
                payload["status"] = "rolled_back"
                payload["rolled_back"] = True
                payload["error"] = "LSP synchronization failed"
                return _json(payload)
            try:
                diagnostics_tool = self.agent.get_tool_by_name("get_diagnostics_for_file")
                payload["diagnostics"] = diagnostics_tool.apply(relative_path=relative)
                payload["diagnostics_checked"] = True
            except Exception as exc:
                target.write_bytes(original)
                invalidator.invalidate_file(relative, str(root))
                payload["status"] = "rolled_back"
                payload["rolled_back"] = True
                payload["diagnostics_checked"] = False
                payload["error"] = f"diagnostics failed: {exc}"
        else:
            payload["cache_invalidated"] = False
        return _json(payload)


class CgcIndexTool(Tool, ToolMarkerOptional):

    def apply(self, force: bool = False, path: str = ".") -> str:
        """Queue a Workspace-relative path for isolated background indexing."""
        indexer = indexer_for_workspace(self.project.project_root)
        job_id = indexer.submit(force=force, path=path)
        return _json({"job_id": job_id, "state": "queued", "workspace_root": indexer.runner.config.workspace_root})


class CgcIndexStatusTool(Tool, ToolMarkerOptional):
    """Read the status of a queued CGC index job."""

    def apply(self, job_id: str) -> str:
        """Return queued, running, completed, or failed state for this Workspace job."""
        return _json(indexer_for_workspace(self.project.project_root).status(job_id))

class CgcStalePathsTool(Tool, ToolMarkerOptional):
    """Report files changed since the last successful CGC index job."""

    def apply(self) -> str:
        """Return stale Workspace-relative paths requiring incremental indexing."""
        indexer = indexer_for_workspace(self.project.project_root)
        return _json({
            "workspace_root": indexer.runner.config.workspace_root,
            "stale_paths": indexer.stale_paths(),
        })


class CgcCallersTool(Tool, ToolMarkerOptional, ToolMarkerSymbolicRead):
    """Find callers of a function using the CGC graph sidecar."""

    def apply(self, function: str, path: str | None = None) -> str:
        """Return CGC callers; this is separate from Serena/LSP references."""
        return result_json(runner_for_workspace(self.project.project_root).cgc_callers(function, path))


class CgcCalleesTool(Tool, ToolMarkerOptional, ToolMarkerSymbolicRead):
    """Find callees of a function using the CGC graph sidecar."""

    def apply(self, function: str, path: str | None = None) -> str:
        """Return CGC callees; this is separate from Serena/LSP references."""
        return result_json(runner_for_workspace(self.project.project_root).cgc_callees(function, path))


class CgcQueryTool(Tool, ToolMarkerOptional, ToolMarkerSymbolicRead):
    """Query the optional read-only CGC graph sidecar for the active Workspace."""

    def apply(self, query: str) -> str:
        """Run a read-only Cypher query against the active Workspace graph."""
        return result_json(runner_for_workspace(self.project.project_root).cgc_query(query))
