"""Optional read-only MCP tools for Workspace-scoped sidecars."""

import json

from serena.tools.tools_base import Tool, ToolMarkerOptional, ToolMarkerSymbolicRead
from serena_v8.sidecars import indexer_for_workspace, result_json, runner_for_workspace


def _json(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class AstGrepSearchTool(Tool, ToolMarkerOptional, ToolMarkerSymbolicRead):
    """Search the active Workspace structurally with optional ast-grep."""

    def apply(self, pattern: str, language: str, path: str = ".") -> str:
        """Run a read-only structural search inside the active Workspace."""
        return result_json(runner_for_workspace(self.project.project_root).ast_grep_search(pattern, language, path))


class CgcIndexTool(Tool, ToolMarkerOptional):
    """Build or refresh the isolated CGC graph for the active Workspace."""

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

class CgcCallersTool(Tool, ToolMarkerOptional, ToolMarkerSymbolicRead):

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
