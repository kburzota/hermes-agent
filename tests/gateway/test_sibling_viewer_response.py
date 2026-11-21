"""A Desktop socket is host-scoped: a chat in another profile attaches through a sibling connection
(``AuthorityConnection._sibling_for``), but the viewer's answer to that session's server request (the
vault code box, sudo, secret) is a bare JSON-RPC response with no ``profile`` and lands on the primary
connection. It must still settle, or ``ask_viewer`` waits out its timeout and the code is lost."""
from types import SimpleNamespace

import pytest


async def _authority(tmp_path, monkeypatch, profile_id):
    from gateway.config import GatewayConfig, Platform
    from gateway.session import SessionSource, SessionStore
    from gateway.session_authority import LiveSession, initialize_session_authority

    home = tmp_path / profile_id
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    store = SessionStore(home / 'sessions', GatewayConfig())

    async def answer(event):
        return 'ok'
    runner = SimpleNamespace(_session_db=store._db, session_store=store, _draining=False,
                             _handle_message=answer, _adapter_for_source=lambda source: None)
    authority = await initialize_session_authority(runner, profile_id=profile_id, instance_id='owner')
    store._db.create_session('s', source='telegram')
    authority.sessions['s'] = LiveSession(SessionSource(platform=Platform.TELEGRAM, chat_id='c'), 's')
    return authority


@pytest.mark.asyncio
@pytest.mark.parametrize('attached', [True, False])
async def test_primary_socket_settles_a_sibling_sessions_request(tmp_path, monkeypatch, attached):
    from gateway import session_server_requests
    from gateway.session_controls import AuthorityConnection

    primary = await _authority(tmp_path, monkeypatch, 'p')
    sibling = await _authority(tmp_path, monkeypatch, 'q')
    transport = object()
    connection = AuthorityConnection(primary, transport, {'user_id': 'owner', 'profile_scope': 'host'})
    # What ``_sibling_for('q')`` builds for a host-scoped grant.
    via = AuthorityConnection(sibling, transport, {'user_id': 'owner', 'profile_id': 'q', 'instance_id': 'owner'})
    connection._siblings['q'] = via
    if attached:
        await via.dispatch({'id': 1, 'method': 'session.resume', 'params': {'session_id': 's'}})
    pending = session_server_requests._Pending('s', events=sibling.sessions['s'].event_stream, method='vault.code')
    session_server_requests._open['srq-sib'] = pending
    try:
        reply = await connection.dispatch({'jsonrpc': '2.0', 'id': 'srq-sib', 'result': {'value': '123456'}})
        assert reply is None
        # Only a viewer attached to that session may answer it, sibling or not.
        assert pending.event.is_set() is attached
        assert pending.value == ('123456' if attached else '')
    finally:
        session_server_requests._open.pop('srq-sib', None)
        await connection.close()
