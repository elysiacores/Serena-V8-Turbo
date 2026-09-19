"""Optional read-only MCP tools for Workspace-scoped sidecars."""

from serena.tools.tools_base import Tool, ToolMarkerOptional, ToolMarkerSymbolicRead
from serena_v8.sidecars import result_json, runner_for_workspace


class AstGrepSearchTool(Tool, ToolMarkerOptional, ToolMarkerSymbolicRead):
    """Search the active Workspace structurally with optional ast-grep."""

    def apply(self, pattern: str, language: str, path: str = ".") -> str:
        """Run a read-only structural search inside the active Workspace."""
        return result_json(runner_for_workspace(self.project.project_root).ast_grep_search(pattern, language, path))


class CgcIndexTool(Tool, ToolMarkerOptional):
    """Build or refresh the isolated CGC graph for the active Workspace."""

    def apply(self, force: bool = False, path: str = ".") -> str:
        """Index a Workspace-relative path into its external CGC database."""
        return result_json(runner_for_workspace(self.project.project_root).cgc_index(force, path))


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
