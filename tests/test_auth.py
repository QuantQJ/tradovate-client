"""Credential loading and the renewal decision, with a fake clock."""

import json
import os
import stat

import pytest

from tradovate_client import Credentials, Env, TokenStore
from tradovate_client.errors import AuthError, ConfigError

GOOD = {'name': 'user', 'password': 'pw', 'appId': 'app', 'appVersion': '1.0',
        'deviceId': 'device', 'cid': 1234, 'sec': 'secret'}


def _write_creds(tmp_path, data, mode=0o600):
    path = tmp_path / 'key.json'
    path.write_text(json.dumps(data))
    os.chmod(path, mode)
    return path


def test_credentials_from_file(tmp_path):
    creds = Credentials.from_file(_write_creds(tmp_path, GOOD))
    assert creds.name == 'user' and creds.cid == 1234
    assert creds.as_payload() == GOOD


def test_credentials_never_print_the_secret(tmp_path):
    text = repr(Credentials.from_file(_write_creds(tmp_path, GOOD)))
    assert 'secret' not in text and 'pw' not in text and '***' in text


def test_world_readable_credentials_are_refused(tmp_path):
    path = _write_creds(tmp_path, GOOD, mode=0o644)
    with pytest.raises(ConfigError, match='readable by others'):
        Credentials.from_file(path)


def test_missing_field_is_named(tmp_path):
    short = {k: v for k, v in GOOD.items() if k != 'deviceId'}
    with pytest.raises(ConfigError, match='deviceId'):
        Credentials.from_file(_write_creds(tmp_path, short))


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match='no credential file'):
        Credentials.from_file(tmp_path / 'absent.json')


def test_credentials_from_environment(monkeypatch):
    for field, value in GOOD.items():
        key = 'TRADOVATE_' + ''.join(
            ('_' + c.upper() if c.isupper() and i else c.upper())
            for i, c in enumerate(field))
        monkeypatch.setenv(key, str(value))
    creds = Credentials.from_env()
    assert creds.name == 'user' and creds.sec == 'secret'


def test_token_is_cached_per_environment(tmp_path):
    clock = lambda: 1000.0
    demo = TokenStore(Env.DEMO, directory=tmp_path, clock=clock)
    live = TokenStore(Env.LIVE, directory=tmp_path, clock=clock)
    demo.write({'accessToken': 'demo-token'})
    live.write({'accessToken': 'live-token'})
    assert demo.read()[0] == 'demo-token'
    assert live.read()[0] == 'live-token'
    assert demo.path != live.path


def test_token_file_is_private(tmp_path):
    store = TokenStore(Env.DEMO, directory=tmp_path, clock=lambda: 0.0)
    store.write({'accessToken': 'x'})
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_expiry_is_read_from_tradovates_reply(tmp_path):
    store = TokenStore(Env.DEMO, directory=tmp_path, clock=lambda: 0.0)
    store.write({'accessToken': 'x',
                 'expirationTime': '2030-01-01T00:00:00.000Z'})
    assert store.read()[1] > 1_800_000_000


def test_a_token_close_to_expiry_is_not_fresh(tmp_path):
    now = [1000.0]
    store = TokenStore(Env.DEMO, directory=tmp_path, clock=lambda: now[0])
    store.write({'accessToken': 'x'})          # assumed 3000s of life
    assert store.fresh_enough()
    now[0] = 1000.0 + 2800.0                    # 200s left, under the margin
    assert not store.fresh_enough()


def test_reply_without_a_token_raises(tmp_path):
    store = TokenStore(Env.DEMO, directory=tmp_path, clock=lambda: 0.0)
    with pytest.raises(AuthError, match='no accessToken'):
        store.write({'errorText': 'Captcha required'})


def test_missing_cache_reads_as_empty(tmp_path):
    store = TokenStore(Env.DEMO, directory=tmp_path / 'nope', clock=lambda: 0.0)
    assert store.read() == (None, 0.0)
    assert not store.fresh_enough()


# ── the MEDIUM finding: the token is never briefly world-readable ──────────

def test_token_directory_is_private(tmp_path):
    store = TokenStore(Env.DEMO, directory=tmp_path / 'state', clock=lambda: 0.0)
    store.write({'accessToken': 'x'})
    assert stat.S_IMODE((tmp_path / 'state').stat().st_mode) == 0o700


def test_no_leftover_temp_file_holds_the_token(tmp_path):
    store = TokenStore(Env.DEMO, directory=tmp_path, clock=lambda: 0.0)
    store.write({'accessToken': 'x'})
    assert [p.name for p in tmp_path.iterdir()] == [store.path.name]


def test_every_file_written_is_private(tmp_path):
    """Whatever the process umask is, nothing here is group or world readable."""
    old = os.umask(0o000)
    try:
        store = TokenStore(Env.LIVE, directory=tmp_path / 'umask', clock=lambda: 0.0)
        store.write({'accessToken': 'x'})
        for path in [store.path, store.directory]:
            assert not stat.S_IMODE(path.stat().st_mode) & 0o077, path
    finally:
        os.umask(old)


def test_rewriting_a_token_keeps_it_private(tmp_path):
    store = TokenStore(Env.DEMO, directory=tmp_path, clock=lambda: 0.0)
    store.write({'accessToken': 'first'})
    store.write({'accessToken': 'second'})
    assert store.read()[0] == 'second'
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
