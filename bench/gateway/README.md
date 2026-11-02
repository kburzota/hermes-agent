# Gateway CPU benches (step 1)

In-process drivers for the Hermes gateway hot paths that pin one core in
production: session/stream event fanout, secret redaction, and a kanban
dispatcher tick against ~30 running tasks.

These are **not** a substitute for a live `py-spy` GIL profile. They exist so
a fork can measure a change on Linux CI (or a laptop) without standing up
Telegram/Desktop/hosted rooms.

## Run

From the repo root, with the checkout importable (the test environment or
`PYTHONPATH=.`):

```bash
python -m bench.gateway
python -m bench.gateway --json
python -m bench.gateway --only fanout_stream,redact_corpus,kanban_tick
```

The runner reports **CPU time** (`time.process_time`) per operation after a
warm-up call. Wall time is included for sanity; compare CPU numbers across
runs on a quiet machine.

`orjson` is optional. The `json_dumps_compare` workload prints `orjson=absent`
when it is not importable and still measures stdlib `json.dumps` plus
`serialize_frame`.

The benches write a temporary `HERMES_HOME` for the kanban board and never
touch `~/.hermes`.

## What each workload drives

| Workload | Production path | Shape |
|---|---|---|
| `fanout_stream` | `FanoutTransport.write` of `message.delta` to N stdio-like peers | 4 subscribers × 400 events, 8 repeats |
| `json_dumps_compare` | `serialize_frame` / stdlib / optional orjson | 2000 events × 20 repeats |
| `redact_corpus` | `redact_sensitive_text(force=True)` / `redact_for_egress` | 10 KiB prose, a code dump, a `sk-` blob, a status line |
| `kanban_tick` | `dispatch_once` + `has_spawnable_ready` on one board | 30 healthy `running` tasks, 12 ticks, no spawn |

## Measured numbers

Hardware: this cloud agent VM, CPython **3.14.9** (same family as production
BIG-IRON), **orjson absent**. CPU times are `time.process_time` after one
warm-up call.

| Workload | Before (`live-patches`) | After (this branch) | Notes |
|---|---|---|---|
| `fanout_stream` (4 peers × 400 deltas) | 2723 µs / loop | 1787–2014 µs / loop | **1.35–1.5×**. Drain no longer re-`dumps` per peer. |
| encode once vs ×4 subscribers | — | **3.86×** | Isolated `serialize_frame` repeat; this is the production bug. |
| `redact` 10 KiB assignment-heavy prose | 2344 µs | ~2370 µs | Assignment/`=` path still dominates (expected). |
| `redact` 11 KiB plain chat (no `=`) | (not split out) | **851 µs** | Telegram / Zhipu / header regexes skipped. |
| `redact` 13 KiB code dump | 4649 µs | 4506–4597 µs | Small; `_PREFIX_RE` still runs when a prefix literal is present. |
| `kanban_tick` 30 running, 12 ticks | 2577 µs / tick | 2599 µs / tick | Bench already used one connection; watcher path dropped the **second open + `to_thread` hop**. |
| `json_dumps_compare` | orjson absent | orjson absent | Optional; install `orjson` locally to see the dumps speedup. |

Re-run `python -m bench.gateway` on `live-patches` vs this branch to reproduce.
Variance on a busy VM is a few percent; compare CPU, not wall.

## What this does *not* cover

- Hosted-room supervisor thread count / poll interval
- Managed-worker `Popen` / fork
- `hermes-change-watcher` session-DB digest every 0.5s
- Per-token stream redaction (today deltas are **not** secret-scrubbed)
- GIL wait (`take_gil`) — needs Kyle's live `py-spy` on BIG-IRON
