"""Where credentials come from, and the places this library refuses to look.

There is no default path. The point of these tests is that a key file sitting
in a home directory is not picked up unless the caller names it.
"""

import json
import os

import pytest

from tradovate_client import Credentials, Env, TradovateClient
from tradovate_client.errors import ConfigError

GOOD = {
    'name': 'user',
    'password': 'pw',
    'appId': 'app',
    'appVersion': '1.0',
    'deviceId': 'device',
    'cid': 1234,
    'sec': 'secret',
}


def write_key(path, data=None):
    path.write_text(json.dumps(data or GOOD))
    os.chmod(path, 0o600)
    return path


def test_no_credentials_anywhere_raises_and_lists_the_options():
    client = TradovateClient(Env.DEMO)
    with pytest.raises(ConfigError) as caught:
        client.credentials
    text = str(caught.value)
    assert 'credentials=' in text
    assert 'credential_path=' in text
    assert 'TRADOVATE_CREDENTIALS' in text
    assert 'TRADOVATE_*' in text


def test_there_is_no_default_credential_path_attribute():
    assert not hasattr(TradovateClient, 'DEFAULT_CREDENTIAL_PATH')


def test_a_key_file_in_the_home_directory_is_not_read(tmp_path, monkeypatch):
    """The old default. Even sitting right there, it must be ignored."""
    home = tmp_path / 'home'
    home.mkdir()
    write_key(home / '.tradovate_key')
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setattr(os.path, 'expanduser', lambda p: p.replace('~', str(home), 1))
    with pytest.raises(ConfigError, match='no credentials were given'):
        TradovateClient(Env.DEMO).credentials


def test_passed_credentials_are_used_as_is():
    creds = Credentials(**GOOD)
    client = TradovateClient(Env.LIVE, credentials=creds)
    assert client.credentials is creds


def test_a_named_file_is_read(tmp_path):
    path = write_key(tmp_path / 'key.json')
    client = TradovateClient(Env.DEMO, credential_path=str(path))
    assert client.credentials.name == 'user'


def test_the_environment_can_name_the_file(tmp_path, monkeypatch):
    path = write_key(tmp_path / 'elsewhere.json')
    monkeypatch.setenv('TRADOVATE_CREDENTIALS', str(path))
    assert TradovateClient(Env.DEMO).credentials.cid == 1234


def test_the_environment_can_carry_the_fields(monkeypatch):
    monkeypatch.setenv('TRADOVATE_NAME', 'user')
    monkeypatch.setenv('TRADOVATE_PASSWORD', 'pw')
    monkeypatch.setenv('TRADOVATE_APP_ID', 'app')
    monkeypatch.setenv('TRADOVATE_APP_VERSION', '1.0')
    monkeypatch.setenv('TRADOVATE_DEVICE_ID', 'device')
    monkeypatch.setenv('TRADOVATE_CID', '1234')
    monkeypatch.setenv('TRADOVATE_SEC', 'secret')
    assert TradovateClient(Env.DEMO).credentials.name == 'user'


def test_an_explicit_path_wins_over_the_environment(tmp_path, monkeypatch):
    named = write_key(tmp_path / 'named.json', dict(GOOD, name='from-argument'))
    other = write_key(tmp_path / 'env.json', dict(GOOD, name='from-environment'))
    monkeypatch.setenv('TRADOVATE_CREDENTIALS', str(other))
    client = TradovateClient(Env.DEMO, credential_path=str(named))
    assert client.credentials.name == 'from-argument'


def test_a_named_file_that_is_missing_says_so(tmp_path):
    client = TradovateClient(Env.DEMO, credential_path=str(tmp_path / 'absent.json'))
    with pytest.raises(ConfigError, match='no credential file'):
        client.credentials


def test_a_named_file_others_can_read_is_still_refused(tmp_path):
    path = tmp_path / 'loose.json'
    path.write_text(json.dumps(GOOD))
    os.chmod(path, 0o644)
    client = TradovateClient(Env.DEMO, credential_path=str(path))
    with pytest.raises(ConfigError, match='readable by others'):
        client.credentials
