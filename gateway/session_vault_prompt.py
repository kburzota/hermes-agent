"""The masked password-manager prompts (unlock, save login, one-time code) for a local viewer's turn.

Main's TUI sidecar registered the three ``agent.vault_backends.unlock`` callbacks per session
(``tui_gateway/agent_callbacks.py``) and the renderer answers ``vault.unlock_prompt`` /
``vault.save_login`` / ``vault.code`` (``tui_gateway/contracts/server_requests.py``). On the session
authority none were registered, so ``browser_vault`` took the headless path (``prompt_unavailable``)
in a Desktop chat. Values travel only in the viewer's response frame, never on the event stream.
"""
from __future__ import annotations

import contextlib
import json

UNLOCK_TIMEOUT_S = 120  # main: ``_ask("vault.unlock_prompt", ..., timeout=120)``
VIEWER_INPUT_TIMEOUT_S = 180  # main: save_login and code wait 180 s


@contextlib.contextmanager
def vault_prompt_scope(turn, agent):
    """Install the vault callbacks on this (agent) thread for a local viewer's turn; restore after.
    Parallel tool workers inherit them through ``tools.thread_context``."""
    from gateway.run_turn_progress import local_viewer_turn
    if not local_viewer_turn(turn):
        yield
        return
    from agent.vault_backends import unlock
    from gateway.session_server_requests import ask_viewer
    authority, session_id, _generation = turn._approval_owner

    def ask(method, params, timeout):
        live = authority.sessions.get(session_id)
        if live is None:
            return ""
        return ask_viewer(live.event_stream, session_id, method, params, timeout=timeout,
                          cancelled=lambda: bool(getattr(agent, "_interrupt_requested", False)))

    def save_login(origin, site):
        raw = ask("vault.save_login", {"origin": origin, "site": site}, VIEWER_INPUT_TIMEOUT_S)
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            return None
        return data if isinstance(data, dict) and data.get("password") else None

    slots = ((unlock.get_unlock_prompt_callback, unlock.set_unlock_prompt_callback,
              lambda backend, display_name: ask("vault.unlock_prompt",
                                                {"backend": backend, "display_name": display_name}, UNLOCK_TIMEOUT_S)),
             (unlock.get_save_login_prompt_callback, unlock.set_save_login_prompt_callback, save_login),
             (unlock.get_code_prompt_callback, unlock.set_code_prompt_callback,
              lambda site, hint: ask("vault.code", {"site": site, "hint": hint}, VIEWER_INPUT_TIMEOUT_S)))
    previous = [get() for get, _set, _cb in slots]
    for _get, set_, cb in slots:
        set_(cb)
    unlock.set_current_session_id(session_id)  # an unlock on this turn is recorded as this session's
    try:
        yield
    finally:
        for (_get, set_, _cb), prev in zip(slots, previous):
            set_(prev)
        unlock.set_current_session_id(None)
