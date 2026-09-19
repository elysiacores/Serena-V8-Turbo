"""Installation consistency checks for Serena V8."""
from __future__ import annotations

import importlib.metadata as metadata
from pathlib import Path
import shutil
import sys

from serena_v8._version import VERSION


def _dist_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def diagnose() -> tuple[bool, list[str]]:
    import serena

    issues: list[str] = []
    dist_version = _dist_version("serena-agent")
    legacy_v8 = _dist_version("serena-v8")
    runtime_version = getattr(serena, "__version__", None)
    environment_serena = Path(sys.executable).with_name("serena")
    path_serena = shutil.which("serena")

    if dist_version != VERSION:
        issues.append(f"distribution mismatch: serena-agent={dist_version!r}, expected {VERSION!r}")
    if runtime_version != VERSION:
        issues.append(f"runtime mismatch: serena={runtime_version!r}, expected {VERSION!r}")
    if legacy_v8 is not None:
        issues.append(f"legacy serena-v8 distribution is also installed ({legacy_v8}); remove it")
    if not environment_serena.is_file():
        issues.append(f"serena executable is missing from this environment: {environment_serena}")

    details = [
        f"expected_version={VERSION}",
        f"distribution_serena_agent={dist_version}",
        f"runtime_serena={runtime_version}",
        f"runtime_path={getattr(serena, '__file__', None)}",
        f"python={sys.executable}",
        f"environment_serena={environment_serena}",
        f"path_serena={path_serena}",
    ]
    if path_serena is not None and Path(path_serena).resolve() != environment_serena.resolve():
        details.append("WARNING: PATH resolves a different serena executable; MCP clients should use the environment_serena path above")
    if legacy_v8 is not None:
        details.append(f"legacy_distribution_serena_v8={legacy_v8}")
    details.extend(f"ERROR: {issue}" for issue in issues)
    return not issues, details


def main() -> int:
    ok, details = diagnose()
    print("\n".join(details))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
