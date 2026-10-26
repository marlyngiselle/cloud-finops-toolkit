"""Load the Azure Resource Graph queries shipped in ``queries/``.

Queries live as plain ``.kql`` files so they can be reviewed, pasted into the
Resource Graph Explorer, and diffed independently of the Python code. Each file
starts with a ``//`` comment header that documents it; those lines are stripped
before the query is sent.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent


def _queries_dir() -> Path:
    # Installed wheel: queries are bundled inside the package.
    # Editable install / source checkout: they sit at the repository root.
    for candidate in (_PACKAGE_DIR / "queries", _PACKAGE_DIR.parents[1] / "queries"):
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("could not locate the 'queries' directory")


@dataclass(frozen=True)
class Query:
    """A named Resource Graph query. ``name`` is the path under ``queries/`` without suffix."""

    name: str
    text: str


@cache
def load_query(name: str) -> Query:
    path = _queries_dir() / f"{name}.kql"
    if not path.is_file():
        raise KeyError(f"unknown query: {name}")
    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("//")
    ]
    body = "\n".join(lines).strip()
    if not body:
        raise ValueError(f"query file is empty: {path}")
    return Query(name=name, text=body)


def available_queries() -> list[str]:
    root = _queries_dir()
    return sorted(str(p.relative_to(root).with_suffix("")) for p in root.rglob("*.kql"))
