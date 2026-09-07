#!/usr/bin/env python3
"""IRISX Bright Data L2 hook — source assembled from staged base64 parts (exact bytes)."""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path

_EXPECTED_SHA256 = "5b37e9b03a688185844707d6778d184a53f05904f65b9c0a5be90e78002e344e"
_STAGING = Path(__file__).resolve().parent.parent / ".staging-irisx-bd"


def _load_source() -> str:
    parts = []
    for i in range(7):
        p = _STAGING / f"part{i:02d}.b64"
        if not p.is_file():
            raise FileNotFoundError(f"missing staged part: {p}")
        parts.append(p.read_text(encoding="ascii").strip())
    raw = base64.b64decode("".join(parts))
    digest = hashlib.sha256(raw).hexdigest()
    if digest != _EXPECTED_SHA256:
        raise RuntimeError(f"brightdata source sha256 mismatch: {digest}")
    return raw.decode("utf-8")


_SRC = _load_source()
exec(compile(_SRC, str(Path(__file__).resolve()), "exec"), globals())
