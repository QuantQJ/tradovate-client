"""Credentials and access tokens.

The part that matters here is renewal. Tradovate throttles
``/auth/accesstokenrequest`` aggressively -- on the demo environment
especially -- so a client that re-authenticates from scratch on every run
gets locked out. ``/auth/renewaccesstoken`` is cheap and is not throttled the
same way, so this module renews near expiry and only performs a full
authentication when it has no usable token at all.
"""

import json
import os
import pathlib
import tempfile
import time
from dataclasses import dataclass, asdict
from datetime import datetime

from .enums import Env
from .errors import AuthError, ConfigError

__all__ = ['Credentials', 'TokenStore']

CREDENTIAL_FIELDS = ('name', 'password', 'appId', 'appVersion', 'deviceId', 'cid', 'sec')

#: Renew once the token has fewer than this many seconds left.
RENEW_MARGIN_SECONDS = 300.0

#: Used when Tradovate's reply has no parseable expiry.
ASSUMED_LIFETIME_SECONDS = 3000.0


@dataclass(frozen=True)
class Credentials:
    """The seven fields Tradovate's authentication endpoint expects."""

    name: str
    password: str
    appId: str
    appVersion: str
    deviceId: str
    cid: int
    sec: str

    def as_payload(self) -> dict:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data, *, source='mapping'):
        missing = [f for f in CREDENTIAL_FIELDS if not data.get(f)]
        if missing:
            raise ConfigError('%s is missing: %s' % (source, ', '.join(missing)))
        return cls(**{f: data[f] for f in CREDENTIAL_FIELDS})

    @classmethod
    def from_file(cls, path):
        path = pathlib.Path(os.path.expanduser(str(path)))
        if not path.exists():
            raise ConfigError('no credential file at %s' % path)
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            raise ConfigError(
                '%s is readable by others (mode %o); run: chmod 600 %s'
                % (path, mode, path))
        try:
            data = json.loads(path.read_text())
        except ValueError as exc:
            raise ConfigError('%s is not valid JSON: %s' % (path, exc)) from exc
        return cls.from_mapping(data, source=str(path))

    @classmethod
    def from_env(cls, prefix='TRADOVATE_'):
        data = {f: os.environ.get(prefix + _env_key(f)) for f in CREDENTIAL_FIELDS}
        return cls.from_mapping(data, source='environment (%s*)' % prefix)

    def __repr__(self):
        return 'Credentials(name=%r, appId=%r, cid=%r, password=***, sec=***)' % (
            self.name, self.appId, self.cid)


def _env_key(field):
    out = []
    for i, ch in enumerate(field):
        if ch.isupper() and i:
            out.append('_')
        out.append(ch.upper())
    return ''.join(out)


def default_token_dir():
    base = os.environ.get('XDG_STATE_HOME') or os.path.expanduser('~/.local/state')
    return pathlib.Path(base) / 'tradovate-client'


class TokenStore:
    """Caches one access token per environment on disk, mode 0600.

    The cache is keyed by environment so a demo token can never be presented
    to the live host.
    """

    def __init__(self, env, *, directory=None, clock=time.time):
        self.env = Env(env)
        self.directory = pathlib.Path(
            os.path.expanduser(str(directory))) if directory else default_token_dir()
        self._clock = clock

    @property
    def path(self):
        return self.directory / ('token-%s.json' % self.env.value)

    def read(self):
        """Return ``(token, expires_at)`` or ``(None, 0.0)``."""
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None, 0.0
        token = data.get('accessToken')
        try:
            expires = float(data.get('expires_at') or 0.0)
        except (TypeError, ValueError):
            expires = 0.0
        return (token, expires) if token else (None, 0.0)

    def write(self, reply):
        """Persist Tradovate's auth reply. Returns the access token."""
        token = reply.get('accessToken')
        if not token:
            raise AuthError('authentication reply carried no accessToken',
                            body=reply)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            'accessToken': token,
            'expires_at': _expiry_of(reply, self._clock),
            'userId': reply.get('userId'),
            'env': self.env.value,
        }
        # Written through a private temp file and moved into place, so the
        # token is never momentarily readable by anyone else: O_EXCL means the
        # file is this process's own, and the mode applies from creation
        # rather than being corrected afterwards.
        handle, tmp = tempfile.mkstemp(dir=str(self.directory),
                                       prefix='.token-', suffix='.tmp')
        try:
            os.fchmod(handle, 0o600)
            with os.fdopen(handle, 'w') as fh:
                fh.write(json.dumps(payload))
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return token

    def clear(self):
        try:
            self.path.unlink()
        except OSError:
            pass

    def fresh_enough(self, margin=RENEW_MARGIN_SECONDS):
        token, expires = self.read()
        return bool(token) and (expires - self._clock()) > margin


def _expiry_of(reply, clock):
    raw = reply.get('expirationTime')
    if raw:
        try:
            text = str(raw).replace('Z', '+00:00')
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            pass
    return clock() + ASSUMED_LIFETIME_SECONDS
