"""Only approved modules may spawn processes or open network connections.

Everything else must go through the dispatcher, so an undeclared subprocess or
socket call is a bypass of its readiness, budget, and provenance gates.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "agentic_pipeline"
RESTRICTED_MODULES = frozenset(
    {"subprocess", "socket", "http", "urllib", "ftplib", "smtplib", "requests", "httpx", "aiohttp"}
)
RESTRICTED_OS_CALLS = frozenset(
    {"system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp"}
    | {f"exec{s}" for s in ("l", "le", "lp", "lpe", "v", "ve", "vp", "vpe")}
    | {f"spawn{s}" for s in ("l", "le", "lp", "lpe", "v", "ve", "vp", "vpe")}
)
# module file -> restricted top-level modules it may import
APPROVED = {"readiness.py": frozenset({"subprocess", "http"})}


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    allowed = APPROVED.get(path.name, frozenset())
    found: list[str] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            top = name.split(".")[0]
            if top in RESTRICTED_MODULES and top not in allowed:
                found.append(f"{path.name}:{node.lineno} imports {name}")
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
            and node.attr in RESTRICTED_OS_CALLS
        ):
            found.append(f"{path.name}:{node.lineno} uses os.{node.attr}")
    return found


def test_no_undeclared_process_or_network_access() -> None:
    violations = [v for path in sorted(PACKAGE.glob("*.py")) for v in _violations(path)]
    assert not violations, violations


def test_scanner_detects_bypass(tmp_path: Path) -> None:
    probe = tmp_path / "rogue.py"
    probe.write_text("import os\nfrom urllib.request import urlopen\nos.system('x')\n")
    assert _violations(probe) == ["rogue.py:2 imports urllib.request", "rogue.py:3 uses os.system"]
