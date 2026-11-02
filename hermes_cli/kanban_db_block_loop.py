"""Unblock-loop parking: stop the dispatcher from reclaiming a looping card.

``block_task`` counts same-cause re-blocks and used to route the card to
``triage`` at ``BLOCK_RECURRENCE_LIMIT``. Auto-decompose treats every triage
card as a new idea: ``fanout=false`` specifies it back to ``todo``/``ready``,
and the dispatcher reclaims it on the next tick. That is the production
loop (a deploy card re-blocked ~once a minute while detection kept firing).

Default policy: park as sticky ``blocked`` so only an explicit unblock
returns it to the pool, comment why, and — when the reason explicitly
mentions exactly one other installed profile (``@name``, ``profile``/
``assignee``/``assigned to``) — create one support card assigned to that
profile (idempotent; linked as a *parent* of the parked card so it is not
gated behind the card it exists to unblock).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

_UNBLOCK_AUTHOR = "block-loop"
_UNBLOCK_KEY_PREFIX = "block-loop-unblock:"
_PARK_COMMENT_PREFIX = "Parked after repeated same-cause blocks"

# Profile names in a block reason must be mentioned explicitly. A bare word
# match would mint a support card for a profile named ``test`` from
# "waiting for a test slot".
_PROFILE_TOKEN = r"[A-Za-z0-9][A-Za-z0-9_-]*"
_AT_MENTION = re.compile(
    rf"(?<![A-Za-z0-9._-])@({_PROFILE_TOKEN})(?![A-Za-z0-9._-])",
    re.IGNORECASE,
)
_LABELED_MENTION = re.compile(
    rf"(?<![A-Za-z0-9_-])(?:profile|assignee)\s*:?\s*({_PROFILE_TOKEN})(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
_ASSIGNED_TO_MENTION = re.compile(
    rf"(?<![A-Za-z0-9_-])assigned\s+to\s+({_PROFILE_TOKEN})(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)


def park_block_loop_enabled() -> bool:
    """``kanban.park_block_loop`` (default True): land loop detection in ``blocked``."""
    return _kanban_flag("park_block_loop", True)


def block_loop_unblock_card_enabled() -> bool:
    """``kanban.block_loop_unblock_card`` (default True): mint one named-agent support card."""
    return _kanban_flag("block_loop_unblock_card", True)


def route_block(
    kind: Optional[str], reason: Optional[str], source_status: str, *,
    prev_kind: Optional[str], prev_recurrences: int,
    loop_landing: str = "blocked",
) -> tuple[str, str, str, tuple, dict]:
    """``(new_status, event_kind, set_sql, params, payload)`` for ``block_task``.

    ``dependency`` waits in ``todo``. Other kinds count same-cause recurrences.
    At ``BLOCK_RECURRENCE_LIMIT`` the event is ``block_loop_detected`` and the
    card lands in ``loop_landing`` (``blocked`` by default, ``triage`` when
    ``kanban.park_block_loop`` is false).
    """
    from hermes_cli.kanban_db import BLOCK_RECURRENCE_LIMIT

    payload = {"reason": reason, "kind": kind, "source_status": source_status}
    if kind == "dependency":
        return "todo", "dependency_wait", "block_kind    = ?", (kind,), payload
    recurrences = prev_recurrences + 1 if prev_kind == kind else 1
    set_sql = "block_kind    = ?,\n                       block_recurrences = ?"
    payload = {"reason": reason, "kind": kind, "recurrences": recurrences, "source_status": source_status}
    if recurrences >= BLOCK_RECURRENCE_LIMIT:
        payload["limit"] = BLOCK_RECURRENCE_LIMIT
        landing = loop_landing if loop_landing in ("blocked", "triage") else "blocked"
        payload["parked"] = landing
        return landing, "block_loop_detected", set_sql, (kind, recurrences), payload
    return "blocked", "blocked", set_sql, (kind, recurrences), payload


def named_unblock_assignee(
    reason: Optional[str], *, known_profiles: Iterable[str], exclude: Optional[str] = None,
) -> Optional[str]:
    """The single installed profile *explicitly* named in *reason*, or None.

    A hit is ``@name``, ``profile name`` / ``profile: name``, ``assignee name``
    / ``assignee: name``, or ``assigned to name``. Bare words never match, so a
    profile named ``test`` is not taken from "waiting for a test slot". Skips
    the looping card's own assignee and the virtual ``default`` profile.
    Several installed profiles → None rather than guess.
    """
    text = (reason or "").strip()
    if not text:
        return None
    skip = {(exclude or "").casefold(), "default"}
    known = {n.casefold(): n for n in known_profiles if n and n.casefold() not in skip}
    hits: list[str] = []
    seen: set[str] = set()
    for raw in _explicit_profile_mentions(text):
        key = raw.casefold()
        if key in seen:
            continue
        seen.add(key)
        if key in known:
            hits.append(known[key])
    if len(hits) == 1:
        return hits[0]
    return None


def _explicit_profile_mentions(text: str) -> list[str]:
    """Token spans from ``@name`` / ``profile|assignee name`` / ``assigned to name``."""
    found: list[str] = []
    for pattern in (_AT_MENTION, _LABELED_MENTION, _ASSIGNED_TO_MENTION):
        found.extend(m.group(1) for m in pattern.finditer(text))
    return found


def block_loop_task_ids(conn: Any, task_ids: list[str]) -> set[str]:
    """Subset of *task_ids* that already have a ``block_loop_detected`` event."""
    if not task_ids:
        return set()
    placeholders = ",".join("?" * len(task_ids))
    rows = conn.execute(
        "SELECT DISTINCT task_id FROM task_events "
        f"WHERE kind = 'block_loop_detected' AND task_id IN ({placeholders})",
        task_ids,
    ).fetchall()
    return {row["task_id"] for row in rows}


def filter_triage_ids(conn: Any, task_ids: list[str]) -> list[str]:
    """Drop loop-escalated cards so auto-decompose cannot put them back in the pool."""
    skip = block_loop_task_ids(conn, task_ids)
    return [tid for tid in task_ids if tid not in skip]


def on_block_loop_detected(
    conn: Any, task_id: str, *, reason: Optional[str], kind: Optional[str], payload: Optional[dict] = None,
) -> Optional[str]:
    """Comment + optional named-agent support card. Never raises; None if no card."""
    try:
        return _on_block_loop_detected(conn, task_id, reason=reason, kind=kind, payload=payload)
    except Exception:
        logger.exception("block-loop follow-up failed for %s", task_id)
        return None


def _kanban_flag(key: str, default: bool) -> bool:
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        kanban = cfg.get("kanban") if isinstance(cfg, dict) else None
        if not isinstance(kanban, dict) or key not in kanban:
            return default
        return bool(kanban.get(key))
    except Exception:
        logger.exception("block-loop: failed to read kanban.%s", key)
        return default


def _known_profile_names() -> set[str]:
    try:
        from hermes_cli import profiles as profiles_mod
        return {p.name for p in profiles_mod.list_profiles()}
    except Exception:
        logger.exception("block-loop: failed to list profiles")
        return set()


def _on_block_loop_detected(
    conn: Any, task_id: str, *, reason: Optional[str], kind: Optional[str], payload: Optional[dict],
) -> Optional[str]:
    from hermes_cli import kanban_db as kb

    card_id = None
    if block_loop_unblock_card_enabled():
        card_id = _ensure_unblock_card(conn, task_id, reason=reason)
    recurrences = (payload or {}).get("recurrences")
    limit = (payload or {}).get("limit")
    lines = [
        f"{_PARK_COMMENT_PREFIX} ({recurrences}/{limit}). "
        "The dispatcher will not reclaim this card until it is explicitly unblocked.",
    ]
    if reason:
        lines.append(f"Block reason: {reason}")
    if kind:
        lines.append(f"Block kind: {kind}")
    if card_id:
        card = kb.get_task(conn, card_id)
        assignee = (card.assignee if card else None) or "unassigned"
        lines.append(
            f"Created unblock card {card_id} assigned to {assignee}. "
            f"Complete that card, then unblock {task_id}."
        )
    already = any(
        (c.body or "").startswith(_PARK_COMMENT_PREFIX) for c in kb.list_comments(conn, task_id)
    )
    if not already:
        kb.add_comment(conn, task_id, _UNBLOCK_AUTHOR, "\n".join(lines))
    return card_id


def _ensure_unblock_card(conn: Any, task_id: str, *, reason: Optional[str]) -> Optional[str]:
    from hermes_cli import kanban_db as kb
    from hermes_cli.profiles import profile_exists

    parent = kb.get_task(conn, task_id)
    if parent is None:
        return None
    assignee = named_unblock_assignee(
        reason, known_profiles=_known_profile_names(), exclude=parent.assignee,
    )
    if not assignee:
        return None
    try:
        if not profile_exists(assignee):
            return None
    except Exception:
        logger.exception("block-loop: profile_exists(%s) failed", assignee)
        return None
    key = f"{_UNBLOCK_KEY_PREFIX}{task_id}"
    existing = conn.execute(
        "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
        "ORDER BY created_at DESC LIMIT 1",
        (key,),
    ).fetchone()
    if existing:
        card_id = existing["id"]
        _link_unblock_parent(conn, card_id, task_id)
        return card_id
    snippet = (reason or "resolve the blocker").strip().replace("\n", " ")
    title = f"Unblock {task_id}: {snippet}"[:80]
    body = (
        f"This card unblocks {task_id} ({parent.title}).\n\n"
        "The parent was parked after repeated same-cause blocks so the dispatcher "
        "would stop reclaiming it. Do the work below, complete this card, then "
        f"unblock {task_id} (`hermes kanban unblock {task_id}`).\n\n"
        f"Required action:\n{reason or '(see parent card)'}\n"
    )
    ws_kind = parent.workspace_kind or "scratch"
    ws_path = parent.workspace_path if ws_kind != "worktree" else None
    card_id = kb.create_task(
        conn,
        title=title,
        body=body,
        assignee=assignee,
        created_by=_UNBLOCK_AUTHOR,
        workspace_kind=ws_kind,
        workspace_path=ws_path,
        tenant=parent.tenant,
        creator_task_id=task_id,
        idempotency_key=key,
    )
    _link_unblock_parent(conn, card_id, task_id)
    return card_id


def _link_unblock_parent(conn: Any, parent_id: str, child_id: str) -> None:
    """Support card is the *parent* of the parked card (docs: never the reverse)."""
    from hermes_cli import kanban_db as kb

    if parent_id in kb.parent_ids(conn, child_id):
        return
    kb.link_tasks(conn, parent_id, child_id)
