"""Hot-path workloads the live gateway process actually runs.

Each driver exercises production functions (not a reimplementation) so a
change to fanout, redaction, or kanban dispatch shows up here. Work stays
in-process: no network, no real workers, no writes under ``~/.hermes``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

_log = logging.getLogger("bench.gateway")


def _cpu() -> float:
    return time.process_time()


def _time_loop(fn: Callable[[], None], iterations: int) -> dict[str, float]:
    # One untimed call warms imports / sqlite caches the way a live process
    # would after the first tick; reported numbers are the steady-state loop.
    fn()
    start = _cpu()
    wall0 = time.perf_counter()
    for _ in range(iterations):
        fn()
    cpu = _cpu() - start
    wall = time.perf_counter() - wall0
    return {
        "iterations": float(iterations),
        "cpu_s": cpu,
        "wall_s": wall,
        "cpu_us_per_op": (cpu / iterations) * 1_000_000 if iterations else 0.0,
        "ops_per_cpu_s": (iterations / cpu) if cpu else 0.0,
    }


# ---------------------------------------------------------------------------
# Fanout: one session streams deltas to N subscribers (Ink + desktop + mirrors)
# ---------------------------------------------------------------------------

def _delta_frame(index: int, session_id: str = "bench-session") -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "message.delta",
            "session_id": session_id,
            "payload": {"text": f"token-{index:04d} " + ("lorem " * 20)},
        },
    }


class _StdioLikeSink:
    """Mimics StdioTransport: each write re-serializes the dict (today's drain)."""

    def __init__(self) -> None:
        self.writes = 0
        self.bytes = 0

    def write(self, obj: dict) -> bool:
        from tui_gateway.transport import serialize_frame
        line = serialize_frame(obj, "bench", _log)
        self.writes += 1
        self.bytes += len(line)
        return True

    def write_wire(self, line: str) -> bool:
        if not line.endswith("\n"):
            line = line + "\n"
        self.writes += 1
        self.bytes += len(line)
        return True

    def close(self) -> None:
        return None


def fanout_stream(*, subscribers: int = 4, events: int = 400) -> dict[str, float]:
    """Admit ``events`` stream frames onto a FanoutTransport with ``subscribers`` peers."""
    from tui_gateway.transport import FanoutTransport, serialize_frame

    frames = [_delta_frame(i) for i in range(events)]

    def once() -> None:
        sinks = [_StdioLikeSink() for _ in range(subscribers)]
        fanout = FanoutTransport(*sinks)
        for frame in frames:
            fanout.write(frame)
        fanout.close()

    stats = _time_loop(once, 8)
    # Also report a single-encode baseline so the README can show the
    # theoretical floor (serialize once, no fanout decode/re-encode).
    encode_start = _cpu()
    encoded = 0
    for _ in range(8):
        for frame in frames:
            encoded += len(serialize_frame(frame, "bench", _log))
    encode_cpu = _cpu() - encode_start
    stats["encode_only_cpu_s"] = encode_cpu
    stats["subscribers"] = float(subscribers)
    stats["events"] = float(events)
    stats["encode_only_us_per_event"] = (encode_cpu / (8 * events)) * 1_000_000
    return stats


def json_dumps_compare(*, events: int = 2000) -> dict[str, float]:
    """stdlib json.dumps vs optional orjson on the same stream-event payload."""
    from tui_gateway.transport import serialize_frame

    frames = [_delta_frame(i) for i in range(events)]

    def stdlib() -> None:
        for frame in frames:
            json.dumps(frame, ensure_ascii=False)

    stats = {"stdlib": _time_loop(stdlib, 20)}
    try:
        import orjson  # type: ignore
    except ImportError:
        stats["orjson_available"] = 0.0
    else:
        def fast() -> None:
            for frame in frames:
                orjson.dumps(frame).decode("utf-8")

        stats["orjson"] = _time_loop(fast, 20)
        stats["orjson_available"] = 1.0
        stats["orjson_speedup"] = (
            stats["stdlib"]["cpu_s"] / stats["orjson"]["cpu_s"]
            if stats["orjson"]["cpu_s"] else 0.0
        )
    stats["serialize_frame"] = _time_loop(lambda: [serialize_frame(f, "bench", _log) for f in frames], 20)
    return _flatten("json", stats)


def _flatten(prefix: str, tree: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, value in tree.items():
        if isinstance(value, dict):
            for inner, number in value.items():
                if isinstance(number, (int, float)):
                    out[f"{prefix}.{key}.{inner}"] = float(number)
        elif isinstance(value, (int, float)):
            out[f"{prefix}.{key}"] = float(value)
    return out


# ---------------------------------------------------------------------------
# Redaction: tool dumps / status / secret-bearing blobs
# ---------------------------------------------------------------------------

_LOREM = (
    "The worker finished compiling the module and wrote 128 objects to the "
    "cache. Status: ok. elapsed=1.4s path=/tmp/build/out.o next=link.\n"
)

_CODE_DUMP = """\
def connect(url, api_key=None, timeout=30):
    # MAX_TOKENS=8192 is a budget, not a secret.
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    return headers

SELECT id, status, claim_expires FROM tasks WHERE status = 'running';
"""


def _corpus() -> dict[str, str]:
    prose = _LOREM * 80  # ~10 KiB of punctuation-heavy prose (':' '=' '.' '+')
    code = _CODE_DUMP * 40
    secret = prose + " exported OPENAI_API_KEY=sk-" + ("a" * 48) + "\n"
    status = "Running terminal command: pytest tests/gateway/test_fanout.py -q"
    return {
        "prose_10kib": prose,
        "code_dump": code,
        "secret_bearing": secret,
        "status_line": status,
    }


def redact_corpus(*, repeats: int = 80) -> dict[str, float]:
    from agent.redact import redact_for_egress, redact_sensitive_text

    samples = _corpus()
    out: dict[str, float] = {}
    for name, text in samples.items():
        def once(sample=text) -> None:
            redact_sensitive_text(sample, force=True)

        stats = _time_loop(once, repeats)
        for key, value in stats.items():
            out[f"redact.{name}.{key}"] = value
        out[f"redact.{name}.chars"] = float(len(text))

    def egress() -> None:
        for text in samples.values():
            redact_for_egress(text)

    for key, value in _time_loop(egress, repeats).items():
        out[f"redact.egress_all.{key}"] = value
    return out


# ---------------------------------------------------------------------------
# Kanban dispatcher: ~30 running tasks, reclaim-heavy idle tick
# ---------------------------------------------------------------------------

def _seed_running_board(conn: Any, n: int, *, pid: int) -> None:
    from hermes_cli import kanban_db as kb

    now = int(time.time())
    expires = now + 3600
    for i in range(n):
        task_id = kb.create_task(
            conn,
            title=f"bench-running-{i:02d}",
            assignee="bench",
        )
        lock = f"bench:lock{i:02d}"
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=?, assignee='bench' WHERE id=?",
            (lock, expires, pid, task_id),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (task_id, lock, expires, pid, now),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, task_id))
    conn.commit()


def kanban_tick(*, tasks: int = 30, ticks: int = 12) -> dict[str, float]:
    """One-board dispatch_once loop with ``tasks`` healthy running rows (no spawn)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    home = Path(tempfile.mkdtemp(prefix="hermes-bench-kanban-"))
    hermes_home = home / ".hermes"
    hermes_home.mkdir()
    previous_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(hermes_home)
    connects = {"n": 0}
    real_connect = kbc.connect

    def counting_connect(*args: Any, **kwargs: Any):
        connects["n"] += 1
        return real_connect(*args, **kwargs)

    kbc.connect = counting_connect  # type: ignore[method-assign]
    try:
        kb.init_db()
        conn = kbc.connect()
        try:
            _seed_running_board(conn, tasks, pid=os.getpid())
        finally:
            conn.close()

        def once() -> None:
            tick_conn = kbc.connect()
            try:
                kbd.dispatch_once(
                    tick_conn,
                    spawn_fn=lambda *_a, **_k: None,
                    dry_run=True,
                    stale_timeout_seconds=0,
                )
                kbd.has_spawnable_ready(tick_conn)
            finally:
                tick_conn.close()

        stats = _time_loop(once, ticks)
        stats["tasks"] = float(tasks)
        stats["connects"] = float(connects["n"])
        return stats
    finally:
        kbc.connect = real_connect  # type: ignore[method-assign]
        if previous_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = previous_home


def all_workloads() -> dict[str, dict[str, float]]:
    return {
        "fanout_stream": fanout_stream(),
        "json_dumps_compare": json_dumps_compare(),
        "redact_corpus": redact_corpus(),
        "kanban_tick": kanban_tick(),
    }
