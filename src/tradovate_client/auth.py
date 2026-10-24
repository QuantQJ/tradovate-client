"""Credentials and access tokens.

The part that matters here is renewal. Tradovate throttles
``/auth/accesstokenrequest`` aggressively -- on the demo environment
especially -- so a client that re-authenticates from scratch on every run
gets locked out. ``/auth/renewaccesstoken`` is cheap and is not throttled the
same way, so this module renews near expiry and only performs a full
authentication when it has no usable token at all.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime

from .enums import Env
from .errors import AuthError, ConfigError

__all__ = ['Credentials', 'TokenStore']

CREDENTIAL_FIELDS = (
    'name',
    'password',
    'appId',
    'appVersion',
    'deviceId',
    'cid',
    'sec',
)

#: Renew once the token has fewer than this many seconds left.
RENEW_MARGIN_SECONDS = 300.0

#: Used when Tradovate's reply has no parseable expiry.
ASSUMED_LIFETIME_SECONDS = 3000.0

#: How many time penalties to sit through before giving up and raising.
#: Each wait is dictated by Tradovate's own ``p-time``.
MAX_PENALTY_RETRIES = 4

#: A cap on any single wait, so a bad ``p-time`` cannot hang a process
#: indefinitely. Tradovate's penalties are seconds to a few minutes.
MAX_PENALTY_WAIT_SECONDS = 600.0

#: User statuses that mean authentication will not succeed by retrying.
#: From Tradovate's own AccessTokenResponse model.
BLOCKING_USER_STATUSES = frozenset({'Closed', 'TemporaryLocked'})


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
    def from_mapping(cls, data, *, source='mapping') -> Credentials:
        missing = [f for f in CREDENTIAL_FIELDS if not data.get(f)]
        if missing:
            raise ConfigError('%s is missing: %s' % (source, ', '.join(missing)))
        return cls(**{f: data[f] for f in CREDENTIAL_FIELDS})

    @classmethod
    def from_file(cls, path) -> Credentials:
        path = pathlib.Path(path).expanduser()
        if not path.exists():
            raise ConfigError('no credential file at %s' % path)
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            raise ConfigError(
                '%s is readable by others (mode %o); run: chmod 600 %s'
                % (path, mode, path)
            )
        try:
            data = json.loads(path.read_text())
        except ValueError as exc:
            raise ConfigError('%s is not valid JSON: %s' % (path, exc)) from exc
        return cls.from_mapping(data, source=str(path))

    @classmethod
    def from_env(cls, prefix='TRADOVATE_') -> Credentials:
        data = {f: os.environ.get(prefix + _env_key(f)) for f in CREDENTIAL_FIELDS}
        return cls.from_mapping(data, source='environment (%s*)' % prefix)

    def __repr__(self) -> str:
        return 'Credentials(name=%r, appId=%r, cid=%r, password=***, sec=***)' % (
            self.name,
            self.appId,
            self.cid,
        )


def _env_key(field) -> str:
    out = []
    for i, ch in enumerate(field):
        if ch.isupper() and i:
            out.append('_')
        out.append(ch.upper())
    return ''.join(out)


def default_token_dir() -> pathlib.Path:
    base = os.environ.get('XDG_STATE_HOME')
    root = pathlib.Path(base) if base else pathlib.Path('~/.local/state').expanduser()
    return root / 'tradovate-client'


class TokenStore:
    """Caches one access token per environment on disk, mode 0600.

    The cache is keyed by environment so a demo token can never be presented
    to the live host.
    """

    def __init__(self, env, *, directory=None, clock=time.time, login=None) -> None:
        self.env = Env(env)
        # Keyed by login as well as environment. Two different logins sharing
        # a token directory would otherwise read each other's token, which on
        # the live host means trading the wrong account.
        self.login = str(login) if login else None
        self.directory = (
            pathlib.Path(directory).expanduser() if directory else default_token_dir()
        )
        self._clock = clock

    @property
    def path(self) -> pathlib.Path:
        if self.login:
            return self.directory / (
                'token-%s-%s.json' % (self.env.value, _login_key(self.login))
            )
        return self.directory / ('token-%s.json' % self.env.value)

    def read(self) -> tuple[str | None, float]:
        """Return ``(token, expires_at)`` or ``(None, 0.0)``."""
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None, 0.0
        if data.get('env') and data['env'] != self.env.value:
            return None, 0.0
        if self.login and data.get('login') not in (
            None,
            _login_key(self.login),
        ):
            return None, 0.0  # someone else's token; ignore it
        token = data.get('accessToken')
        try:
            expires = float(data.get('expires_at') or 0.0)
        except (TypeError, ValueError):
            expires = 0.0
        return (token, expires) if token else (None, 0.0)

    def write(self, reply) -> str:
        """Persist Tradovate's auth reply. Returns the access token."""
        token = reply.get('accessToken')
        if not token:
            raise AuthError('authentication reply carried no accessToken', body=reply)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            'accessToken': token,
            'expires_at': _expiry_of(reply, self._clock),
            'userId': reply.get('userId'),
            'env': self.env.value,
            'login': _login_key(self.login) if self.login else None,
        }
        # Written through a private temp file and moved into place, so the
        # token is never momentarily readable by anyone else: O_EXCL means the
        # file is this process's own, and the mode applies from creation
        # rather than being corrected afterwards.
        handle, tmp = tempfile.mkstemp(
            dir=str(self.directory), prefix='.token-', suffix='.tmp'
        )
        try:
            os.fchmod(handle, 0o600)
            with os.fdopen(handle, 'w') as fh:
                fh.write(json.dumps(payload))
            pathlib.Path(tmp).replace(self.path)
        except BaseException:
            try:
                pathlib.Path(tmp).unlink()
            except OSError:
                pass
            raise
        return token

    def clear(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass

    def fresh_enough(self, margin=RENEW_MARGIN_SECONDS) -> bool:
        token, expires = self.read()
        return bool(token) and (expires - self._clock()) > margin


def _login_key(login) -> str:
    """A short, stable, non-reversible tag for a login name.

    The username never appears in a filename; only a digest of it does.
    """
    return hashlib.blake2b(str(login).encode(), digest_size=6).hexdigest()


def _expiry_of(reply, clock) -> float:
    raw = reply.get('expirationTime')
    if raw:
        try:
            text = str(raw).replace('Z', '+00:00')
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            pass
    return clock() + ASSUMED_LIFETIME_SECONDS
