"""Minimal .env reader so keys can live in a local, gitignored file. Values are never printed."""

from __future__ import annotations

import os
from pathlib import Path
from typing import MutableMapping


def parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key.isidentifier():
            out[key] = value
    return out


def load_env_file(path: Path | str, environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Load KEY=VALUE pairs without overriding variables already set. Returns the KEY names loaded."""
    environ = os.environ if environ is None else environ
    p = Path(path)
    if not p.is_file():
        return []
    loaded = []
    for k, v in parse_env(p.read_text(encoding="utf-8")).items():
        if v != "" and k not in environ:
            environ[k] = v
            loaded.append(k)
    return loaded
