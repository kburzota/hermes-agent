# Gateway CPU benches

In-process drivers for the Hermes gateway hot paths that pin one core on
BIG-IRON. Step 1 covered fanout / redaction / kanban. The Nov 1 production
`py-spy --gil` capture showed those were **not** the live costs, so the
harness now also drives the 43-profile liveness scan, the session-db read
pool, and empty hosted-room `bindings()` polls.

These are **not** a substitute for a live GIL profile. They exist so a fork
can measure a change on Linux CI (or a laptop) without standing up
Telegram/Desktop/hosted rooms.

## Run

From the repo root, with the checkout importable (the test environment or
`PYTHONPATH=.`):

```bash
python -m bench.gateway
python -m bench.gateway --json
python -m bench.gateway --only host_liveness_scan,readpool_pressure,hosted_bindings_empty
```

The runner reports **CPU time** (`time.process_time`) per operation after a
warm-up call. Wall time is included for sanity; compare CPU numbers across
runs on a quiet machine.

`orjson` is optional. The `json_dumps_compare` workload prints `orjson=absent`
when it is not importable and still measures stdlib `json.dumps` plus
`serialize_frame`.

The benches write a temporary `HERMES_HOME` and never touch `~/.hermes`.

## What each workload drives

| Workload | Production path | Shape |
|---|---|---|
| `fanout_stream` | `FanoutTransport.write` of `message.delta` to N stdio-like peers | 4 subscribers × 400 events, 8 repeats |
| `json_dumps_compare` | `serialize_frame` / stdlib / optional orjson | 2000 events × 20 repeats |
| `redact_corpus` | `redact_sensitive_text(force=True)` / `redact_for_egress` | 10 KiB prose, a code dump, a `sk-` blob, a status line |
| `kanban_tick` | `dispatch_once` + `has_spawnable_ready` on one board | 30 healthy `running` tasks, 12 ticks, no spawn |
| `host_liveness_scan` | `_check_gateway_running` + `_served_by_running_multiplexer` for N homes | 43 profiles, 8 scans; this process **is** the host gateway |
| `readpool_pressure` | SessionDB `_checkout_read_conn` across N `state.db` files | 43 files × 4 checkouts, 6 repeats |
| `hosted_bindings_empty` | `CanonicalHostedRoomService.bindings()` with no owned rooms | 43 services × 12 polls |

## Production GIL profile (Nov 1, BIG-IRON)

`py-spy record --gil --format raw`, default blocking mode, ~2,157 samples,
~19 kanban tasks, 43 profiles. `--gil` = only the GIL-holding thread.
Blocking mode paused the process; proportions are indicative.

Dominant **entry points**: hosted-room supervisor `_worker_loop` 16.5%,
Desktop `GET /api/profiles` 15.9%, managed-worker I/O 15.0%, heartbeat
restore scan 8.2%. Dominant **self-time leaves**: `_open_fd_count` 5.8%,
`_reclaim_idle_read_conn_anywhere` 5.7%, `_apply_wal_size_limit` 3.4%,
unix-socket `identify` 8.6% inclusive. JSON encode ~1% self; redact 1.3%;
kanban dispatch did not appear.

The 44 `hosted-room-driver-supervisor` threads are one `HostedRoomRuntime`
per serving authority (43 profiles + default), not 44 rooms. Each polls
`bindings()` every 5s.

## Measured numbers

Hardware: this cloud agent VM, CPython **3.14.9** (same family as production
BIG-IRON), **orjson absent**. CPU times are `time.process_time` after one
warm-up call.

### Step 1 (still on the branch; barely visible in the production GIL profile)

| Workload | Before (`live-patches`) | After (this branch) | Notes |
|---|---|---|---|
| `fanout_stream` (4 peers × 400 deltas) | 2723 µs / loop | 1787–2014 µs / loop | **1.35–1.5×**. Drain no longer re-`dumps` per peer. |
| encode once vs ×4 subscribers | — | **3.86×** | Isolated `serialize_frame` repeat. |
| `redact` 10 KiB assignment-heavy prose | 2344 µs | ~2370 µs | Assignment/`=` path still dominates. |
| `redact` 11 KiB plain chat (no `=`) | (not split out) | **851 µs** | Telegram / Zhipu / header regexes skipped. |
| `redact` 13 KiB code dump | 4649 µs | 4506–4597 µs | Small. |
| `kanban_tick` 30 running, 12 ticks | 2577 µs / tick | 2599 µs / tick | Watcher lost the second open + `to_thread` hop. |
| `json_dumps_compare` | orjson absent | orjson absent | JSON was ~1% self on BIG-IRON. **Do not expect 2× headroom from orjson.** |

### Step 2 (production-profile hot paths)

| Workload | Before (this branch, pre-fix) | After | Notes |
|---|---|---|---|
| `host_liveness_scan` 43 profiles | 10024 µs / scan, **1.13 identify/scan** | 8252 µs / scan, **0 identify** | Self-PID builds `build_identify_payload()`; no unix-socket self-dial. PID probe uses the 1s dashboard cache. |
| `readpool_pressure` 43 files × 4 | 7690 µs, **1036 reclaims**, max=24 | 5729 µs (**1.34×**), **756 reclaims**, max=64 | Bench still oversubscribes (172 checkouts > 64). A 43-profile “one idle each” fit is now reclaim-free (unit test). FD cache 0.25s → 2s, single-flight listdir. |
| `hosted_bindings_empty` 43 services | 63930 µs / scan, **46.6 list_rooms/scan** | **302 µs / scan (211×)**, **0 list_rooms** | Empty `owned` returns `()` without opening the room store. This is the 44-supervisor poll tax. |

Re-run `python -m bench.gateway` on `live-patches` vs this branch to reproduce.
Variance on a busy VM is a few percent; compare CPU, not wall.

## Expected effect on BIG-IRON

The two GIL entry points that are **this process talking to itself** should
drop a lot:

- Desktop roster poll (`GET /api/profiles`, ~16% of GIL): stop 43×
  `_probe_host_gateway` socket identify; reuse the PID cache `/api/status`
  already had.
- 44 hosted-room supervisors (~16.5% of GIL in `_worker_loop`): profiles
  that own no rooms no longer `list_rooms` + WAL-open every 5s. Rooms that
  exist still pay `read_ctx` (cheaper now: fewer fd listings, less reclaim).
- Read-pool leaves (~12% self): 43 profiles no longer sit permanently on a
  24-permit ceiling; `/dev/fd` is listed at most every 2s, once per window.

What this does **not** cut: managed-worker frame/stderr I/O (~15%),
heartbeat restore scanning every home (~8%), actual hosted-room task work
(~7%), 44 supervisor threads still exist (they just do less). Step 2
(process-per-profile-group) is still the way to get off one core.

## What this still does *not* cover

- Sharing one hosted-room supervisor across profiles (threads remain 1:1)
- Managed-worker `Popen` / `_read_frames`
- `hermes-change-watcher` session-DB digest every 0.5s
- Per-token stream redaction (today deltas are **not** secret-scrubbed)
- GIL wait (`take_gil`) — re-capture `py-spy` on BIG-IRON after deploy
