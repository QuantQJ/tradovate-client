"""The transport layer: JSON over HTTPS, standard library only.

No third-party dependency. One place where every request is built, every
response is decoded, and every failure becomes a typed exception.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .errors import TransportError, ThrottledError, raise_for_status

DEFAULT_TIMEOUT = 20.0
DEFAULT_RETRIES = 2
USER_AGENT = 'tradovate-client/0.1.0 (+https://github.com/QuantQJ/tradovate-client)'


def _encode(body):
    if body is None:
        return None
    return json.dumps(_drop_none(body)).encode('utf-8')


def _drop_none(value):
    """Strip keys whose value is None, recursively.

    Tradovate's order models mark most fields optional. Sending an explicit
    null where the caller simply did not set a field is a good way to get a
    confusing rejection, so unset fields are omitted entirely.
    """
    if isinstance(value, dict):
        return {k: _drop_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_none(v) for v in value]
    return value


class Transport:
    """Minimal JSON client for one environment's REST base URL."""

    def __init__(self, base_url, *, timeout=DEFAULT_TIMEOUT,
                 retries=DEFAULT_RETRIES, sleep=time.sleep):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self._sleep = sleep

    def request(self, method, path, *, token=None, body=None, params=None):
        if params:
            path = '%s?%s' % (path, urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}))
        url = self.base_url + path
        headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'User-Agent': USER_AGENT,
        }
        if token:
            headers['Authorization'] = 'Bearer ' + token

        attempt = 0
        while True:
            attempt += 1
            try:
                req = urllib.request.Request(
                    url, data=_encode(body), headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    raise_for_status(resp.status, path, None,
                                     dict(resp.headers))
                    return self._decode(raw, path)
            except urllib.error.HTTPError as exc:
                raw = b''
                try:
                    raw = exc.read()
                except Exception:
                    pass
                parsed = self._safe_json(raw)
                try:
                    raise_for_status(exc.code, path, parsed, dict(exc.headers or {}))
                except ThrottledError as throttled:
                    if attempt <= self.retries:
                        self._sleep(throttled.retry_after or 2.0 * attempt)
                        continue
                    raise
                raise
            except urllib.error.URLError as exc:
                if attempt <= self.retries:
                    self._sleep(0.5 * attempt)
                    continue
                raise TransportError('request failed: %s' % exc.reason,
                                     endpoint=path) from exc
            except TimeoutError as exc:
                if attempt <= self.retries:
                    self._sleep(0.5 * attempt)
                    continue
                raise TransportError('request timed out after %ss' % self.timeout,
                                     endpoint=path) from exc

    @staticmethod
    def _safe_json(raw):
        try:
            return json.loads(raw.decode('utf-8'))
        except Exception:
            return raw.decode('utf-8', 'replace')[:500] or None

    @staticmethod
    def _decode(raw, path):
        if not raw:
            return None
        try:
            return json.loads(raw.decode('utf-8'))
        except ValueError as exc:
            raise TransportError('response was not JSON: %s' % exc,
                                 endpoint=path,
                                 body=raw.decode('utf-8', 'replace')[:200])
