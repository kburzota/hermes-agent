"""Optional-orjson JSON dumps for TUI/gateway wire frames.

``orjson`` is not a required dependency. When it is importable it encodes the
same JSON-serializable dicts ``json.dumps`` does (UTF-8, no NaN). Failures
(lone surrogates, non-JSON types) fall back to the stdlib so ``serialize_frame``
can still emit the -32603 error envelope.
"""

from __future__ import annotations

import json
from typing import Any

try:
    import orjson as _orjson
except ImportError:  # pragma: no cover - optional speedup
    _orjson = None


def dumps(obj: Any) -> str:
    """``json.dumps(obj, ensure_ascii=False)``-compatible text, orjson when present."""
    if _orjson is not None:
        try:
            return _orjson.dumps(obj).decode("utf-8")
        except (TypeError, ValueError):
            pass
    return json.dumps(obj, ensure_ascii=False)


def dumps_compact_bytes(obj: Any) -> bytes:
    """Replay-ring snapshot: compact UTF-8 JSON, surrogate-safe on fallback."""
    if _orjson is not None:
        try:
            return _orjson.dumps(obj)
        except (TypeError, ValueError):
            pass
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8", errors="surrogatepass"
    )
