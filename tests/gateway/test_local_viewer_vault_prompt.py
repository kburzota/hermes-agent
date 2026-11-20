"""A local viewer's (Desktop) turn asks for vault input through the renderer's masked cards, as main's
sidecar did (``tui_gateway/agent_callbacks.py``): the session authority registered no vault callbacks, so
``browser_vault action=enter_code`` returned ``prompt_unavailable`` in a Desktop chat."""
import threading
import time
from types import SimpleNamespace

import pytest

from gateway.config import Platform


class _Peer:
    def __init__(self):
        self.frames = []

    def write(self, frame):
        self.frames.append(frame)
        return True

    def close(self):
        pass


def _turn(platform):
    from gateway.session_events import SessionEvents
    events, peer = SessionEvents(), _Peer()
    events.fanout.attach(peer)
    authority = SimpleNamespace(sessions={'s1': SimpleNamespace(event_stream=events)})
    turn = SimpleNamespace(_approval_owner=(authority, 's1', 1),
                           _ctx=SimpleNamespace(source=SimpleNamespace(platform=platform)))
    return turn, peer


def _answer(peer, method, value):
    from gateway.session_server_requests import resolve
    from tui_gateway.contracts.registry import SERVER_REQUESTS, validate_params
    deadline = time.monotonic() + 5
    while not any(f.get('method') == method for f in peer.frames):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    request = next(f for f in peer.frames if f.get('method') == method)
    assert validate_params(SERVER_REQUESTS[method], request['params'])[1] is None
    assert resolve({'jsonrpc': '2.0', 'id': request['id'], 'result': {'value': value}})


def test_code_prompt_reaches_the_viewer_and_can_prompt_here_is_true(monkeypatch):
    from agent.vault_backends import unlock
    from gateway.session_vault_prompt import vault_prompt_scope
    monkeypatch.setattr('tools.approval_context._no_user_can_answer', lambda: False)
    turn, peer = _turn(Platform.LOCAL)
    with vault_prompt_scope(turn, SimpleNamespace()):
        assert unlock.can_prompt_here()
        out = {}
        # Dispatch on a tool worker thread, the way parallel/deferred tool calls run.
        worker = threading.Thread(target=propagate(
            lambda: out.setdefault('code', unlock.get_code_prompt_callback()('example.com', ''))))
        worker.start()
        _answer(peer, 'vault.code', '123456')
        worker.join(5)
    assert out['code'] == '123456'
    assert not any('123456' in repr(f) for f in peer.frames)  # the code never rides the event stream
    assert unlock.get_code_prompt_callback() is None and unlock.get_unlock_prompt_callback() is None


def test_save_login_parses_the_viewer_json():
    from agent.vault_backends import unlock
    from gateway.session_vault_prompt import vault_prompt_scope
    turn, peer = _turn(Platform.LOCAL)
    with vault_prompt_scope(turn, SimpleNamespace()):
        out = {}
        worker = threading.Thread(target=propagate(lambda: out.setdefault(
            'v', unlock.get_save_login_prompt_callback()('https://a.b', 'a.b'))))
        worker.start()
        _answer(peer, 'vault.save_login', '{"identifier": "u", "password": "p"}')
        worker.join(5)
    assert out['v'] == {'identifier': 'u', 'password': 'p'}


def propagate(fn):
    from tools.thread_context import propagate_context_to_thread
    return propagate_context_to_thread(fn)


@pytest.mark.parametrize(('platform', 'installed'), [(Platform.LOCAL, True), (Platform.TELEGRAM, False)])
def test_scope_installs_only_for_local_viewer_turns(platform, installed):
    from agent.vault_backends import unlock
    from gateway.session_vault_prompt import vault_prompt_scope
    turn, _ = _turn(platform)
    with vault_prompt_scope(turn, SimpleNamespace()):
        assert (unlock.get_code_prompt_callback() is not None) is installed
        assert (unlock.get_unlock_prompt_callback() is not None) is installed
    assert unlock.get_code_prompt_callback() is None


def test_closed_session_answers_empty():
    from agent.vault_backends import unlock
    from gateway.session_vault_prompt import vault_prompt_scope
    turn, _ = _turn(Platform.LOCAL)
    with vault_prompt_scope(turn, SimpleNamespace()):
        turn._approval_owner[0].sessions.clear()
        assert unlock.get_code_prompt_callback()('example.com', '') == ''
