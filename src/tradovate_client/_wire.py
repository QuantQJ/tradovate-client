"""The websocket wire: frames, heartbeat, authorize, requests, reconnection.

Tradovate runs the same frame protocol on every websocket host -- the user
socket on ``demo``/``live`` and the market data socket on ``md`` -- so the part
that speaks it lives here once and both sockets inherit it.

The protocol, as Tradovate defines it
-------------------------------------
Frames are a single character followed by a body:

====  ===========================================================
``o``  the socket is open; authorize now
``h``  server heartbeat
``a``  a JSON array of messages
``c``  the server is closing
====  ===========================================================

A request is four newline-separated fields::

    <endpoint>\\n<id>\\n<query>\\n<body>

A response comes back inside an ``a`` frame as
``{"i": <id>, "s": <status>, "d": <data>}``; anything without an ``i`` is a
streamed event.

The client must send ``[]`` at least every 2.5 seconds or the server drops the
connection.

Reconnection, and what it will not do
-------------------------------------
A socket that drops is reopened: a fresh token from the REST client, a new
connection, the authorize handshake, and then whatever the subclass needs to
be whole again -- a new ``user/syncrequest``, or its subscriptions sent again.

What is never repeated is a request that was in flight when the socket died.
Its caller gets ``TransportError(ambiguous=True)``, because the server may
have acted on it, and an order sent twice is two orders.

Handlers run on their own thread
--------------------------------
The thread that reads the socket does nothing but read: it answers
heartbeats, hands each response to whoever is waiting for it, and queues every
streamed event for a second thread that applies it and calls the handlers.
So a handler may send a request -- place an order from a fill, cancel a
subscription from a quote -- and the reply can still be read. Were handlers
called on the reading thread, that request would wait on a reply nothing was
left to read. A slow handler also cannot starve the heartbeat.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any, Callable

from ._http import _drop_none
from .enums import Env
from .errors import (
    PENALTY_TICKET,
    CaptchaRequired,
    ConfigError,
    TimePenalty,
    TradovateError,
    TransportError,
    penalty_of,
)

__all__ = ['Wire', 'HEARTBEAT_SECONDS', 'RECONNECT_DELAYS']

#: Tradovate drops a connection that has not sent anything for 2.5 seconds, so
#: the keep-alive goes out comfortably inside that.
HEARTBEAT_SECONDS = 2.0

#: Seconds to wait before each reconnection attempt; the last value repeats.
#: These are this library's own choice, not Tradovate's, and every socket
#: takes ``reconnect_delays=`` to replace them.
RECONNECT_DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0)

#: How many time penalties one socket request sits through before raising.
MAX_PENALTY_RETRIES = 4

#: A cap on any single penalty wait, so a bad ``p-time`` cannot hang a caller.
MAX_PENALTY_WAIT_SECONDS = 600.0

#: Frame types, as the protocol defines them.
FRAME_OPEN = 'o'
FRAME_HEARTBEAT = 'h'
FRAME_MESSAGES = 'a'
FRAME_CLOSE = 'c'


class Wire:
    """One Tradovate websocket: connect, authorize, request, stay up.

    Subclasses say which host (``_url``), where the token comes from
    (``_token``), what to do once authorized (``_after_authorize``) and what a
    streamed event means (``_on_event``).
    """

    def __init__(
        self,
        env,
        *,
        client=None,
        credentials=None,
        credential_path=None,
        token_dir=None,
        connect_timeout=12.0,
        request_timeout=8.0,
        heartbeat_interval=HEARTBEAT_SECONDS,
        ws=None,
        ws_factory=None,
        reconnect=True,
        reconnect_delays=RECONNECT_DELAYS,
        max_reconnects=None,
        sleep=time.sleep,
    ) -> None:
        self.env = Env(env)
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self.heartbeat_interval = heartbeat_interval
        self.reconnect = bool(reconnect)
        self.reconnect_delays = tuple(reconnect_delays) or RECONNECT_DELAYS
        self.max_reconnects = max_reconnects
        self.reconnects = 0
        self._sleep = sleep
        self._rest = client
        self._rest_kw = {
            'credentials': credentials,
            'credential_path': credential_path,
            'token_dir': token_dir,
        }
        self._ws = ws
        # A socket handed in from outside cannot be reopened unless the caller
        # also says how to make another one.
        self._ws_factory: Callable[[], Any] | None = ws_factory
        self._injected = ws is not None
        self._next = 0
        self._generation = 0
        self._lock = threading.Lock()
        self._pending: dict[Any, dict] = {}
        self._opened = threading.Event()
        self._authorized = threading.Event()
        self._authorize_answered = threading.Event()
        self._authorize_id: int | None = None
        self._authorize_status: Any = None
        self._stop = False
        self._session = 0
        self._reconnecting = False
        self._events: queue.Queue = queue.Queue()
        self._dispatcher: threading.Thread | None = None
        self._threads: list[threading.Thread] = []
        self._handlers: dict[str, list] = {}

    # ── what a subclass supplies ────────────────────────────────────────────

    @property
    def _url(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def _token(self) -> str:
        return self.rest.token()

    def _after_authorize(self, resumed) -> None:
        """Called once the socket is authorized. ``resumed`` on a reconnect."""

    def _on_event(self, message) -> None:
        """Called for every streamed message that is not a response."""

    # ── the REST client, for the token ──────────────────────────────────────

    @property
    def rest(self) -> Any:
        """The REST client whose token this socket reuses.

        Full authentication is a REST call, and the renewal logic lives there,
        so the socket borrows it rather than reimplementing it.
        """
        if self._rest is None:
            from .client import TradovateClient

            kw = {k: v for k, v in self._rest_kw.items() if v is not None}
            self._rest = TradovateClient(self.env, **kw)
        return self._rest

    # ── lifecycle ───────────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return bool(self._ws) and self._opened.is_set() and not self._stop

    @property
    def is_authorized(self) -> bool:
        return self._authorized.is_set()

    def connect(self) -> Any:
        """Open, authorize, and let the subclass take its first snapshot."""
        token = self._token()
        self._stop = False
        self._start_dispatcher()
        self._open(token, resumed=False)
        return self

    def _start_dispatcher(self) -> None:
        if self._dispatcher is not None and self._dispatcher.is_alive():
            return
        events: queue.Queue = queue.Queue()
        self._events = events
        self._dispatcher = threading.Thread(
            target=lambda: self._dispatch_loop(events),
            name='tradovate-dispatch',
            daemon=True,
        )
        self._dispatcher.start()

    def _dispatch_loop(self, events) -> None:
        """Apply streamed events and call handlers, off the reading thread."""
        while True:
            message = events.get()
            if message is None:
                return
            try:
                self._on_event(message)
            except Exception:
                # One malformed event must not take the stream down.
                pass

    def _new_socket(self) -> Any:
        if self._ws_factory is not None:
            return self._ws_factory()
        try:
            import websocket
        except ImportError as exc:
            raise ConfigError(
                'websocket-client is not installed; '
                'pip install "tradovate-client[socket]"'
            ) from exc
        try:
            return websocket.create_connection(self._url, timeout=self.connect_timeout)
        except Exception as exc:
            raise TransportError(
                'could not open a socket to %s: %s' % (self._url, exc)
            ) from exc

    def _open(self, token, *, resumed) -> None:
        if resumed or self._ws is None:
            self._ws = self._new_socket()
        self._opened.clear()
        self._authorized.clear()
        with self._lock:
            self._generation += 1
            generation = self._generation
        ws = self._ws
        self._spawn(lambda: self._receive_loop(ws, generation), 'tradovate-recv')
        self._spawn(lambda: self._heartbeat_loop(ws, generation), 'tradovate-heartbeat')
        if not self._opened.wait(self.connect_timeout):
            self._abandon(ws, final=not resumed)
            raise TransportError(
                'no open frame from Tradovate within %ss' % self.connect_timeout,
                endpoint='websocket',
            )
        try:
            self._authorize(token)
            self._after_authorize(resumed)
        except BaseException:
            self._abandon(ws, final=not resumed)
            raise

    def _abandon(self, ws, *, final) -> None:
        """Give up on one connection attempt."""
        if final:
            self.close()
            return
        # Retire this generation so its loops stop without triggering another
        # reconnect; the reconnect loop that called us decides what is next.
        with self._lock:
            self._generation += 1
        self._opened.clear()
        self._authorized.clear()
        try:
            ws.close()
        except Exception:
            pass

    def _spawn(self, target, name) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        self._threads = [t for t in self._threads if t.is_alive()]
        self._threads.append(thread)
        thread.start()

    def _authorize(self, token) -> None:
        with self._lock:
            self._next += 1
            request_id = self._next
            self._authorize_id = request_id
            self._authorize_status = None
            self._authorize_answered.clear()
        self._send_raw('authorize\n%d\n\n%s' % (request_id, token))
        if not self._authorize_answered.wait(self.connect_timeout):
            raise TransportError(
                'Tradovate did not accept the socket authorization within %ss'
                % self.connect_timeout,
                endpoint='authorize',
            )
        if not self._authorized.is_set():
            # It answered, and the answer was no. Say so at once rather than
            # waiting out the timeout for an acceptance that is not coming.
            raise TransportError(
                'Tradovate did not accept the socket authorization (status %s)'
                % self._authorize_status,
                endpoint='authorize',
                status=self._authorize_status,
            )

    def close(self) -> None:
        self._stop = True
        self._session += 1  # retires any reconnection still waiting its turn
        self._events.put(None)
        self._dispatcher = None
        self._opened.clear()
        self._authorized.clear()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            if not self._injected:
                # A closed socket is not reusable; the next connect() opens
                # a new one.
                self._ws = None
        self._fail_waiters()

    def __enter__(self) -> Any:
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # ── frames ──────────────────────────────────────────────────────────────

    def _send_raw(self, payload) -> None:
        if self._ws is None:
            raise TransportError('socket is not open', endpoint='websocket')
        try:
            self._ws.send(payload)
        except Exception as exc:
            raise TransportError(
                'socket send failed: %s' % exc, endpoint='websocket'
            ) from exc

    def _current(self, generation) -> bool:
        return generation == self._generation and not self._stop

    def _heartbeat_loop(self, ws, generation) -> None:
        while self._current(generation):
            try:
                ws.send('[]')
            except Exception:
                break
            time.sleep(self.heartbeat_interval)

    def _receive_loop(self, ws, generation) -> None:
        while self._current(generation):
            try:
                raw = ws.recv()
            except Exception:
                break
            if not raw:
                continue
            kind, body = raw[0], raw[1:]
            if kind == FRAME_OPEN:
                self._opened.set()
            elif kind == FRAME_HEARTBEAT:
                try:
                    ws.send('[]')
                except Exception:
                    break
            elif kind == FRAME_MESSAGES:
                try:
                    messages = json.loads(body)
                except ValueError:
                    messages = []
                for message in messages:
                    self._dispatch(message)
            elif kind == FRAME_CLOSE:
                break
        if generation != self._generation:
            return  # a newer connection owns the socket now
        self._opened.clear()
        self._authorized.clear()
        self._fail_waiters()
        if self._stop:
            return
        self._emit('disconnect', {'reconnecting': self._can_reconnect()})
        if self._can_reconnect():
            self._spawn(self._reconnect_loop, 'tradovate-reconnect')

    def _fail_waiters(self) -> None:
        """Wake anything still waiting, so a dead socket is not a hang."""
        with self._lock:
            waiting = list(self._pending.values())
            self._pending.clear()
        for entry in waiting:
            entry['closed'] = True
            entry['event'].set()

    def _dispatch(self, message) -> None:
        if not isinstance(message, dict):
            return
        if 'i' in message:
            request_id = message.get('i')
            if request_id == self._authorize_id:
                self._authorize_status = message.get('s')
                if message.get('s') == 200:
                    self._authorized.set()
                else:
                    self._authorized.clear()
                self._authorize_answered.set()
                return
            with self._lock:
                entry = self._pending.get(request_id)
            if entry is not None:
                entry['status'] = message.get('s')
                entry['data'] = message.get('d')
                entry['event'].set()
            return
        self._events.put(message)

    # ── reconnection ────────────────────────────────────────────────────────

    def _can_reconnect(self) -> bool:
        if not self.reconnect or self._stop:
            return False
        # A socket handed in by the caller can only be replaced if the caller
        # also supplied a way to make a new one.
        return not (self._injected and self._ws_factory is None)

    def _reconnect_loop(self) -> None:
        with self._lock:
            if self._reconnecting:
                return
            self._reconnecting = True
        session = self._session
        attempt = 0
        while True:
            if self._stop or session != self._session:
                self._end_reconnect()
                return
            if self.max_reconnects is not None and attempt >= self.max_reconnects:
                self._end_reconnect()
                self._emit(
                    'error',
                    TransportError(
                        'gave up reconnecting after %d attempts' % attempt,
                        endpoint='websocket',
                    ),
                )
                return
            delay = self.reconnect_delays[min(attempt, len(self.reconnect_delays) - 1)]
            attempt += 1
            self._sleep(delay)
            if self._stop or session != self._session:
                self._end_reconnect()
                return
            try:
                token = self._token()
            except (CaptchaRequired, ConfigError) as exc:
                # Nothing a retry can fix: a human has to sign in, or the
                # credentials are gone. Retrying would extend a penalty.
                self._end_reconnect()
                self._emit('error', exc)
                return
            except Exception as exc:
                self._emit('error', exc)
                continue
            try:
                self._open(token, resumed=True)
            except Exception as exc:
                self._emit('error', exc)
                continue
            # The new connection can drop before this loop has let go. Decide
            # under the lock who owns the next attempt: if it is still up,
            # hand off; if it already fell over, keep going here.
            with self._lock:
                alive = self._opened.is_set() or self._stop
                if alive:
                    self._reconnecting = False
            if alive:
                self.reconnects += 1
                self._emit('reconnect', {'attempts': attempt})
                return
            attempt = 0

    def _end_reconnect(self) -> None:
        with self._lock:
            self._reconnecting = False

    # ── requests ────────────────────────────────────────────────────────────

    def request(self, endpoint, query='', body=None, *, timeout=None) -> Any:
        """Send one request over the socket and wait for its answer.

        Raises on timeout or a non-2xx status. A request whose socket dies
        while it is in flight raises ``TransportError`` with
        ``ambiguous=True``: the server may have acted on it, so for an order
        the state has to be read rather than assumed.

        A time penalty -- a reply carrying ``p-ticket`` -- means the request
        was declined, not processed. It is waited out and sent again with the
        ticket attached, the way Tradovate's own socket example does, and a
        ``p-captcha`` raises without retrying.
        """
        waited = 0.0
        for attempt in range(1, MAX_PENALTY_RETRIES + 2):
            data = self._request_once(endpoint, query, body, timeout)
            penalty = penalty_of(data)
            if penalty is None:
                return data
            ticket, seconds, captcha = penalty
            if captcha:
                raise CaptchaRequired(
                    'Tradovate is asking for a captcha on %s, which a '
                    'third-party application cannot answer. Do not retry: '
                    'retrying extends the penalty.' % endpoint,
                    endpoint=endpoint,
                    body=data,
                )
            if attempt > MAX_PENALTY_RETRIES or not isinstance(
                body, (dict, type(None))
            ):
                raise TimePenalty(
                    'Tradovate is throttling %s; it last asked for %gs after '
                    '%gs of waiting' % (endpoint, seconds, waited),
                    seconds=seconds,
                    ticket=ticket,
                    attempts=attempt - 1,
                    endpoint=endpoint,
                    body=data,
                )
            wait = min(max(0.0, seconds), MAX_PENALTY_WAIT_SECONDS)
            waited += wait
            self._sleep(wait)
            body = dict(body or {})
            body[PENALTY_TICKET] = ticket
        raise TransportError(  # pragma: no cover - loop always returns
            'request did not resolve', endpoint=endpoint
        )

    def _request_once(self, endpoint, query, body, timeout) -> Any:
        if not self.is_connected:
            raise TransportError('socket is not open', endpoint=endpoint)
        timeout = self.request_timeout if timeout is None else timeout
        event = threading.Event()
        with self._lock:
            self._next += 1
            request_id = self._next
            entry: dict[str, Any] = {
                'event': event,
                'data': None,
                'status': None,
                'closed': False,
            }
            self._pending[request_id] = entry
        payload = '%s\n%d\n%s\n%s' % (
            endpoint,
            request_id,
            query or '',
            '' if body is None else json.dumps(_drop_none(body)),
        )
        try:
            self._send_raw(payload)
        except TransportError:
            with self._lock:
                self._pending.pop(request_id, None)
            raise
        if not event.wait(timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            raise TransportError(
                'no reply to %s within %ss -- the request may or may not have '
                'been acted on. Read the state before sending anything else.'
                % (endpoint, timeout),
                endpoint=endpoint,
                ambiguous=True,
            )
        with self._lock:
            self._pending.pop(request_id, None)
        if entry['closed']:
            raise TransportError(
                'the socket closed while %s was in flight -- it may or may not '
                'have been acted on. Read the state before sending anything '
                'else.' % endpoint,
                endpoint=endpoint,
                ambiguous=True,
            )
        status = entry['status']
        if status is not None and not 200 <= int(status) < 300:
            raise TradovateError(
                'Tradovate refused %s' % endpoint,
                endpoint=endpoint,
                status=status,
                body=entry['data'],
            )
        return entry['data']

    # ── events ──────────────────────────────────────────────────────────────

    def on(self, event_name, handler) -> Any:
        """Subscribe to an event.

        Every socket emits ``disconnect``, ``reconnect`` and ``error``; each
        subclass documents its own. A handler that raises does not take the
        receive loop down with it.
        """
        self._handlers.setdefault(str(event_name), []).append(handler)
        return self

    def off(self, event_name, handler) -> Any:
        """Remove a handler added with ``on``."""
        handlers = self._handlers.get(str(event_name), [])
        if handler in handlers:
            handlers.remove(handler)
        return self

    def _emit(self, event_name, payload) -> None:
        for handler in list(self._handlers.get(event_name, ())):
            try:
                handler(payload)
            except Exception:
                pass
