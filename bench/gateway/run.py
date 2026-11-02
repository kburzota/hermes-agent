"""CLI for the gateway CPU benches.

Usage (from the repo root, with the checkout on ``PYTHONPATH``)::

    python -m bench.gateway
    python -m bench.gateway --json
    python -m bench.gateway --only fanout_stream,redact_corpus
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from bench.gateway.workloads import (
    all_workloads,
    fanout_stream,
    json_dumps_compare,
    kanban_tick,
    redact_corpus,
)

_DRIVERS = {
    "fanout_stream": fanout_stream,
    "json_dumps_compare": json_dumps_compare,
    "redact_corpus": redact_corpus,
    "kanban_tick": kanban_tick,
}


def _print_human(results: dict[str, dict[str, float]]) -> None:
    for name, stats in results.items():
        print(f"\n== {name} ==")
        width = max(len(key) for key in stats) if stats else 8
        for key in sorted(stats):
            value = stats[key]
            if isinstance(value, float):
                print(f"  {key:<{width}}  {value:.6g}")
            else:
                print(f"  {key:<{width}}  {value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gateway hot-path CPU benches")
    parser.add_argument(
        "--only",
        default="",
        help="Comma-separated subset of: " + ",".join(_DRIVERS),
    )
    parser.add_argument("--json", action="store_true", help="Emit one JSON object")
    args = parser.parse_args(argv)

    if args.only:
        names = [part.strip() for part in args.only.split(",") if part.strip()]
        unknown = [name for name in names if name not in _DRIVERS]
        if unknown:
            parser.error("unknown workload(s): " + ", ".join(unknown))
        results = {name: _DRIVERS[name]() for name in names}
    else:
        results = all_workloads()

    payload: dict[str, Any] = {
        "python": sys.version.split()[0],
        "orjson": _orjson_version(),
        "workloads": results,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"python={payload['python']} orjson={payload['orjson']}")
        _print_human(results)
    return 0


def _orjson_version() -> str:
    try:
        import orjson  # type: ignore
    except ImportError:
        return "absent"
    return getattr(orjson, "__version__", "present")


if __name__ == "__main__":
    raise SystemExit(main())
