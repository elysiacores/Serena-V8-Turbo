"""Optional read-only MCP tools for Workspace-scoped sidecars."""

from serena.tools.tools_base import Tool, ToolMarkerOptional, ToolMarkerSymbolicRead
from serena_v8.sidecars import result_json, runner_for_workspace


class AstGrepSearchTool(Tool, ToolMarkerOptional, ToolMarkerSymbolicRead):
    """Search the active Workspace structurally with optional ast-grep."""

    def apply(self, pattern: str, language: str, path: str = ".") -> str:
        """
        Run a read-only structural search inside the active Workspace.

        :param pattern: ast-grep pattern, for example ``$FUNC($ARG)``
        :param language: source language understood by ast-grep
        :param path: Workspace-relative directory or file to search
        :return: JSON result with status, output, and bounded timing metadata
        """
        result = runner_for_workspace(self.project.project_root).ast_grep_search(pattern, language, path)
        return result_json(result)


class CgcQueryTool(Tool, ToolMarkerOptional, ToolMarkerSymbolicRead):
    """Query the optional read-only CGC graph sidecar for the active Workspace."""

    def apply(self, query: str) -> str:
        """
        Run a read-only graph query using the configured CGC command.

        :param query: sidecar-specific graph query string
        :return: JSON result with status, output, and bounded timing metadata
        """
        result = runner_for_workspace(self.project.project_root).cgc_query(query)
        return result_json(result)
