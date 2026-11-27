"""Fakes shared by the socket tests. No network, no credentials."""

from __future__ import annotations

import json
import queue
import threading
import time

WAIT = 3.0


class FakeRest:
    """Stands in for the REST client: tokens, and contract lookups."""

    def __init__(self, token='tok-123', md_token=None, contracts=None):
        self._token = token
        self._md_token = md_token
        self.token_calls = 0
        self.md_token_calls = 0
        self.contracts = dict(contracts or {})
        self.lookups = []
        self.token_error = None

    def token(self, **kw):
        self.token_calls += 1
        if self.token_error is not None:
            raise self.token_error
        return self._token

    def md_token(self):
        self.md_token_calls += 1
        if self.token_error is not None:
            raise self.token_error
        return self._md_token or self._token

    def find_contract(self, name):
        from tradovate_client import Contract
        from tradovate_client.errors import NotFoundError

        self.lookups.append(name)
        if name not in self.contracts:
            raise NotFoundError('no contract named %r' % name)
        return Contract(id=self.contracts[name], name=name)


class FakeServer:
    """A websocket that speaks Tradovate's frame protocol.

    ``replies`` maps an endpoint to the ``d`` payload it answers with, to a
    ``(status, data)`` pair, or to a callable taking the request body and
    returning either. Anything not listed answers 200 with None.
    """

    def __init__(self, replies=None, *, open_frame=True, authorize_status=200):
        self.sent: list[str] = []
        self.heartbeats = 0
        self.closed = False
        self.replies = dict(replies or {})
        self.authorize_status = authorize_status
        self._inbox: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._seen = 0
        self._stop = False
        if open_frame:
            self._inbox.put('o')
        self._responder = threading.Thread(target=self._serve, daemon=True)
        self._responder.start()

    def send(self, payload):
        if self.closed:
            raise ConnectionError('socket closed')
        with self._lock:
            if payload == '[]':
                self.heartbeats += 1
            else:
                self.sent.append(payload)

    def recv(self):
        item = self._inbox.get()
        if item is None:
            raise ConnectionError('socket closed')
        return item

    def close(self):
        self.closed = True
        self._stop = True
        self._inbox.put(None)

    def drop(self):
        """The connection dies underneath the client."""
        self.close()

    def push(self, frame):
        self._inbox.put(frame)

    def push_event(self, data, event='props'):
        self._inbox.put('a' + json.dumps([{'e': event, 'd': data}]))

    def _serve(self):
        while not self._stop:
            with self._lock:
                pending = self.sent[self._seen :]
                self._seen = len(self.sent)
            for payload in pending:
                self._answer(payload)
            time.sleep(0.005)

    def _answer(self, payload):
        lines = payload.split('\n', 3)
        endpoint, request_id = lines[0], lines[1]
        if endpoint == 'authorize':
            self._inbox.put(
                'a' + json.dumps([{'i': int(request_id), 's': self.authorize_status}])
            )
            return
        reply = self.replies.get(endpoint)
        if callable(reply):
            body = json.loads(lines[3]) if len(lines) > 3 and lines[3] else None
            reply = reply(body)
        status, data = reply if isinstance(reply, tuple) else (200, reply)
        self._inbox.put(
            'a' + json.dumps([{'i': int(request_id), 's': status, 'd': data}])
        )

    @property
    def requests(self):
        return [p.split('\n')[0] for p in self.sent]

    def bodies_of(self, endpoint):
        out = []
        for payload in self.sent:
            lines = payload.split('\n', 3)
            if lines[0] == endpoint and len(lines) > 3 and lines[3]:
                out.append(json.loads(lines[3]))
        return out

    def body_of(self, endpoint):
        found = self.bodies_of(endpoint)
        return found[0] if found else None

    def query_of(self, endpoint):
        for payload in self.sent:
            lines = payload.split('\n', 3)
            if lines[0] == endpoint:
                return lines[2]
        return None


class Factory:
    """Hands out one prepared server per connection, in order."""

    def __init__(self, *servers):
        self.servers = list(servers)
        self.made = 0

    def __call__(self):
        if self.made >= len(self.servers):
            raise ConnectionError('no more servers')
        server = self.servers[self.made]
        self.made += 1
        return server


def until(condition, timeout=WAIT):
    """Wait for something another thread is about to do."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return bool(condition())
