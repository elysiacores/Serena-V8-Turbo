import logging

from sensai.util.git import GitStatus

from ..constants import REPO_ROOT
from .shell import subprocess_check_output

log = logging.getLogger(__name__)


def get_git_status() -> GitStatus | None:
    """Return repository status using one Git process instead of four."""
    try:
        output = subprocess_check_output(
            ["git", "status", "--porcelain=v2", "--branch", "--untracked-files=normal"],
            cwd=REPO_ROOT,
        )
        commit_hash: str | None = None
        has_unstaged_changes = False
        has_staged_changes = False
        has_untracked_files = False

        for line in output.splitlines():
            if line.startswith("# branch.oid "):
                commit_hash = line.removeprefix("# branch.oid ").strip()
                continue
            if line.startswith("? "):
                has_untracked_files = True
                continue
            if line.startswith(("1 ", "2 ", "u ")):
                parts = line.split(maxsplit=2)
                if len(parts) < 2:
                    continue
                xy = parts[1]
                if len(xy) >= 2:
                    has_staged_changes = has_staged_changes or xy[0] != "."
                    has_unstaged_changes = has_unstaged_changes or xy[1] != "."

        if not commit_hash or commit_hash == "(initial)":
            return None
        return GitStatus(
            commit=commit_hash,
            has_unstaged_changes=has_unstaged_changes,
            has_staged_uncommitted_changes=has_staged_changes,
            has_untracked_files=has_untracked_files,
        )
    except Exception:
        return None
