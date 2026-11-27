"""The market data socket, against a fake server. No network, no credentials.

The message shapes are the ones in Tradovate's own tutorial --
``tradovate/example-api-js``, ``tutorial/WebSockets`` EX-08 through EX-11.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fakes import WAIT, Factory, FakeRest, FakeServer, until
from tradovate_client import (
    ChartType,
    ElementSizeUnit,
    Env,
    MarketDataSocket,
    TradovateError,
)
from tradovate_client.errors import ConfigError, NotFoundError
from tradovate_client.marketdata import MARKET_DATA_URL, decode_ticks

QUOTE = {
    'timestamp': '2017-04-13T04:59:06.588Z',
    'contractId': 11,
    'entries': {
        'Bid': {'price': 18405, 'size': 7},
        'TotalTradeVolume': {'size': 4118},
        'Offer': {'price': 18410, 'size': 12},
        'LowPrice': {'price': 18355},
        'Trade': {'price': 18405, 'size': 2},
        'OpenInterest': {'size': 40702},
        'OpeningPrice': {'price': 18380},
        'HighPrice': {'price': 18435},
        'SettlementPrice': {'price': 18370},
    },
}
DOM = {
    'contractId': 11,
    'timestamp': '2017-04-13T11:33:57.488Z',
    'bids': [{'price': 2335.25, 'size': 33}, {'price': 2333, 'size': 758}],
    'offers': [{'price': 2335.5, 'size': 255}, {'price': 2337.75, 'size': 466}],
}
HISTOGRAM = {
    'contractId': 11,
    'timestamp': '2017-04-13T11:33:57.412Z',
    'tradeDate': {'year': 2017, 'month': 4, 'day': 13},
    'base': 2338.75,
    'items': {'-14': 5906, '2': 1234},
    'refresh': False,
}


def bar(stamp, close=2333.75, **extra):
    base = {
        'timestamp': stamp,
        'open': 2334.25,
        'high': 2334.5,
        'low': 2333,
        'close': close,
        'upVolume': 4712,
        'downVolume': 201,
        'upTicks': 1333,
        'downTicks': 82,
        'bidVolume': 2857,
        'offerVolume': 2056,
    }
    base.update(extra)
    return base


def md(replies=None, *, rest=None, servers=None, **kw):
    servers = servers or [FakeServer(replies or {})]
    sleeps = []
    sock = MarketDataSocket(
        Env.DEMO,
        client=rest or FakeRest(contracts={'ABCZ6': 11, 'XYZH7': 22}),
        ws_factory=Factory(*servers),
        connect_timeout=WAIT,
        request_timeout=kw.pop('request_timeout', WAIT),
        heartbeat_interval=0.01,
        sleep=sleeps.append,
        **kw,
    )
    sock.server = servers[0]
    sock.servers = servers
    return sock


# ── the connection ──────────────────────────────────────────────────────────


def test_market_data_has_its_own_host():
    assert MARKET_DATA_URL == 'wss://md.tradovateapi.com/v1/websocket'
    assert Env.DEMO.market_data_url == MARKET_DATA_URL
    assert Env.LIVE.market_data_url == MARKET_DATA_URL


def test_it_authorizes_with_the_market_data_token():
    rest = FakeRest(token='access', md_token='md-token')
    s = md(rest=rest)
    try:
        s.connect()
        frame = next(p for p in s.server.sent if p.startswith('authorize'))
        assert frame.split('\n')[3] == 'md-token'
    finally:
        s.close()


def test_a_rest_client_without_a_market_data_token_hands_over_its_access_token():
    class Bare:
        def token(self, **kw):
            return 'access'

    s = md(rest=Bare())
    try:
        s.connect()
        frame = next(p for p in s.server.sent if p.startswith('authorize'))
        assert frame.split('\n')[3] == 'access'
    finally:
        s.close()


def test_connecting_asks_for_nothing_but_authorization():
    s = md()
    try:
        s.connect()
        assert s.server.requests == ['authorize']
    finally:
        s.close()


# ── quotes ──────────────────────────────────────────────────────────────────


def test_a_quote_subscription_delivers_quotes():
    s = md()
    seen = []
    try:
        s.connect()
        s.subscribe_quote('ABCZ6', seen.append)
        assert s.server.body_of('md/subscribequote') == {'symbol': 'ABCZ6'}
        s.server.push_event({'quotes': [QUOTE]}, event='md')
        assert until(lambda: len(seen) == 1)
        quote = seen[0]
        assert (quote.bid, quote.bid_size) == (18405.0, 7.0)
        assert (quote.ask, quote.ask_size) == (18410.0, 12.0)
        assert (quote.last, quote.last_size) == (18405.0, 2.0)
        assert quote.volume == 4118.0
        assert (quote.high, quote.low, quote.open) == (18435.0, 18355.0, 18380.0)
        assert quote.settlement == 18370.0
        assert quote.open_interest == 40702.0
        assert quote.timestamp == '2017-04-13T04:59:06.588Z'
        assert s.quote('ABCZ6') is quote
    finally:
        s.close()


def test_an_absent_entry_is_none_not_zero():
    """A missing bid must not read as a bid of zero."""
    s = md()
    seen = []
    try:
        s.connect()
        s.subscribe_quote('ABCZ6', seen.append)
        s.server.push_event(
            {'quotes': [{'contractId': 11, 'entries': {'Trade': {'price': 5.0}}}]},
            event='md',
        )
        assert until(lambda: len(seen) == 1)
        assert seen[0].last == 5.0
        assert seen[0].last_size is None
        assert seen[0].bid is None and seen[0].ask is None
        assert seen[0].volume is None
    finally:
        s.close()


def test_quotes_are_routed_by_contract():
    s = md()
    abc, xyz = [], []
    try:
        s.connect()
        s.subscribe_quote('ABCZ6', abc.append)
        s.subscribe_quote('XYZH7', xyz.append)
        s.server.push_event({'quotes': [QUOTE, dict(QUOTE, contractId=22)]}, event='md')
        assert until(lambda: len(abc) == 1 and len(xyz) == 1)
        assert abc[0].contract_id == 11 and xyz[0].contract_id == 22
    finally:
        s.close()


def test_a_contract_id_needs_no_lookup():
    rest = FakeRest()
    s = md(rest=rest)
    seen = []
    try:
        s.connect()
        s.subscribe_quote(11, seen.append)
        assert rest.lookups == []
        assert s.server.body_of('md/subscribequote') == {'symbol': 11}
        s.server.push_event({'quotes': [QUOTE]}, event='md')
        assert until(lambda: len(seen) == 1)
    finally:
        s.close()


def test_a_symbol_is_looked_up_once():
    rest = FakeRest(contracts={'ABCZ6': 11})
    s = md(rest=rest)
    try:
        s.connect()
        s.subscribe_quote('ABCZ6')
        s.subscribe_dom('ABCZ6')
        s.quote('ABCZ6')
        assert rest.lookups == ['ABCZ6']
    finally:
        s.close()


def test_an_unknown_symbol_is_refused_before_anything_is_sent():
    s = md()
    try:
        s.connect()
        with pytest.raises(NotFoundError):
            s.subscribe_quote('NOPE')
        assert 'md/subscribequote' not in s.server.requests
    finally:
        s.close()


def test_a_refused_subscription_raises_and_leaves_nothing_behind():
    s = md({'md/subscribequote': (403, 'not entitled')})
    try:
        s.connect()
        with pytest.raises(TradovateError):
            s.subscribe_quote('ABCZ6')
        assert s._subs == []
    finally:
        s.close()


def test_cancelling_unsubscribes():
    s = md()
    seen = []
    try:
        s.connect()
        sub = s.subscribe_quote('ABCZ6', seen.append)
        sub.cancel()
        assert s.server.body_of('md/unsubscribequote') == {'symbol': 'ABCZ6'}
        assert sub.active is False
        s.server.push_event({'quotes': [QUOTE]}, event='md')
        s.subscribe_dom('ABCZ6')  # a round trip, so the quote has been read
        assert seen == []
    finally:
        s.close()


def test_two_handlers_share_one_subscription():
    """The second subscribe sends nothing; the stream stops with the last."""
    s = md()
    one, two = [], []
    try:
        s.connect()
        first = s.subscribe_quote('ABCZ6', one.append)
        second = s.subscribe_quote('ABCZ6', two.append)
        assert s.server.requests.count('md/subscribequote') == 1
        s.server.push_event({'quotes': [QUOTE]}, event='md')
        assert until(lambda: len(one) == 1 and len(two) == 1)
        first.cancel()
        assert 'md/unsubscribequote' not in s.server.requests
        second.cancel()
        assert s.server.requests.count('md/unsubscribequote') == 1
    finally:
        s.close()


def test_a_handler_that_raises_does_not_stop_the_stream():
    s = md()
    seen = []

    def bad(_quote):
        raise RuntimeError('handler bug')

    try:
        s.connect()
        s.subscribe_quote('ABCZ6', bad)
        s.subscribe_quote('ABCZ6', seen.append)
        s.server.push_event({'quotes': [QUOTE]}, event='md')
        s.server.push_event({'quotes': [QUOTE]}, event='md')
        assert until(lambda: len(seen) == 2)
    finally:
        s.close()


def test_every_quote_is_also_an_event():
    s = md()
    seen = []
    s.on('quote', seen.append)
    try:
        s.connect()
        s.subscribe_quote('ABCZ6')
        s.server.push_event({'quotes': [QUOTE]}, event='md')
        assert until(lambda: len(seen) == 1)
    finally:
        s.close()


# ── depth and histograms ────────────────────────────────────────────────────


def test_depth_of_market():
    s = md()
    seen = []
    try:
        s.connect()
        sub = s.subscribe_dom('ABCZ6', seen.append)
        assert s.server.body_of('md/subscribedom') == {'symbol': 'ABCZ6'}
        s.server.push_event({'doms': [DOM]}, event='md')
        assert until(lambda: len(seen) == 1)
        assert seen[0].bids == [(2335.25, 33), (2333, 758)]
        assert seen[0].offers == [(2335.5, 255), (2337.75, 466)]
        assert s.dom('ABCZ6') is seen[0]
        sub.cancel()
        assert s.server.body_of('md/unsubscribedom') == {'symbol': 'ABCZ6'}
    finally:
        s.close()


def test_the_histogram():
    s = md()
    seen = []
    try:
        s.connect()
        sub = s.subscribe_histogram('ABCZ6', seen.append)
        assert s.server.body_of('md/subscribehistogram') == {'symbol': 'ABCZ6'}
        s.server.push_event({'histograms': [HISTOGRAM]}, event='md')
        assert until(lambda: len(seen) == 1)
        assert seen[0].base == 2338.75
        assert seen[0].items == {'-14': 5906, '2': 1234}
        assert seen[0].trade_date == {'year': 2017, 'month': 4, 'day': 13}
        assert seen[0].refresh is False
        sub.cancel()
        assert s.server.body_of('md/unsubscribehistogram') == {'symbol': 'ABCZ6'}
    finally:
        s.close()


# ── charts ──────────────────────────────────────────────────────────────────


def test_the_chart_enums_are_tradovates():
    assert {t.value for t in ChartType} == {
        'Tick',
        'DailyBar',
        'MinuteBar',
        'Custom',
        'DOM',
    }
    assert {u.value for u in ElementSizeUnit} == {
        'Volume',
        'Range',
        'UnderlyingUnits',
        'Renko',
        'MomentumRange',
        'PointAndFigure',
        'OFARange',
    }


def test_a_chart_request_carries_tradovates_fields():
    s = md({'md/getchart': {'historicalId': 32, 'realtimeId': 31}})
    try:
        s.connect()
        chart = s.chart(
            'ABCZ6',
            underlying_type='MinuteBar',
            element_size=15,
            as_much_as_elements=66,
            closest_timestamp='2017-04-13T11:33Z',
        )
        assert s.server.body_of('md/getchart') == {
            'symbol': 'ABCZ6',
            'chartDescription': {
                'underlyingType': 'MinuteBar',
                'elementSize': 15,
                'elementSizeUnit': 'UnderlyingUnits',
                'withHistogram': False,
            },
            'timeRange': {
                'closestTimestamp': '2017-04-13T11:33Z',
                'asMuchAsElements': 66,
            },
        }
        assert chart.ids == (31, 32)
    finally:
        s.close()


def test_a_chart_needs_a_time_range_and_none_is_assumed():
    s = md({'md/getchart': {'historicalId': 32, 'realtimeId': 31}})
    try:
        s.connect()
        with pytest.raises(ConfigError, match='how much to load'):
            s.chart('ABCZ6')
        assert 'md/getchart' not in s.server.requests
    finally:
        s.close()


def test_an_unknown_chart_type_is_refused():
    s = md()
    try:
        s.connect()
        with pytest.raises(ValueError):
            s.chart('ABCZ6', underlying_type='Hourly', as_much_as_elements=1)
    finally:
        s.close()


def test_a_datetime_goes_out_as_utc():
    s = md({'md/getchart': {'historicalId': 32, 'realtimeId': 31}})
    try:
        s.connect()
        s.chart(
            'ABCZ6',
            closest_timestamp=datetime(2017, 4, 13, 11, 33, tzinfo=timezone.utc),
        )
        sent = s.server.body_of('md/getchart')['timeRange']
        assert sent == {'closestTimestamp': '2017-04-13T11:33:00.000Z'}
    finally:
        s.close()


def test_bars_arrive_history_first_then_real_time():
    s = md({'md/getchart': {'historicalId': 32, 'realtimeId': 31}})
    bars, ended = [], []
    try:
        s.connect()
        chart = s.chart(
            'ABCZ6', as_much_as_elements=2, on_bar=bars.append, on_eoh=ended.append
        )
        s.server.push_event(
            {
                'charts': [
                    {
                        'id': 32,
                        'td': 20170413,
                        'bars': [
                            bar('2017-04-13T11:00:00.000Z'),
                            bar('2017-04-13T10:45:00.000Z', close=2330.0),
                        ],
                    },
                    {'id': 32, 'eoh': True},
                ]
            },
            event='chart',
        )
        assert chart.wait_for_history(WAIT)
        assert ended == [chart]
        assert [b.timestamp for b in chart.bars()] == [
            '2017-04-13T10:45:00.000Z',
            '2017-04-13T11:00:00.000Z',
        ]
        first = chart.bars()[1]
        assert (first.open, first.high, first.low, first.close) == (
            2334.25,
            2334.5,
            2333,
            2333.75,
        )
        assert (first.up_volume, first.down_volume, first.volume) == (4712, 201, 4913)
        assert (first.up_ticks, first.down_ticks) == (1333, 82)
        assert (first.bid_volume, first.offer_volume) == (2857, 2056)
        assert first.time == datetime(2017, 4, 13, 11, 0, tzinfo=timezone.utc)
        # real time, on the other subscription id
        s.server.push_event(
            {'charts': [{'id': 31, 'bars': [bar('2017-04-13T11:15:00.000Z')]}]},
            event='chart',
        )
        assert until(lambda: len(bars) == 3)
    finally:
        s.close()


def test_the_forming_bar_is_replaced_not_repeated():
    s = md({'md/getchart': {'historicalId': 32, 'realtimeId': 31}})
    try:
        s.connect()
        chart = s.chart('ABCZ6', as_much_as_elements=1)
        for close in (10.0, 11.0, 12.0):
            s.server.push_event(
                {
                    'charts': [
                        {'id': 31, 'bars': [bar('2017-04-13T11:00:00.000Z', close)]}
                    ]
                },
                event='chart',
            )
        assert until(lambda: chart.bars() and chart.bars()[0].close == 12.0)
        assert len(chart.bars()) == 1
    finally:
        s.close()


def test_packets_for_another_chart_are_ignored():
    s = md({'md/getchart': {'historicalId': 32, 'realtimeId': 31}})
    try:
        s.connect()
        chart = s.chart('ABCZ6', as_much_as_elements=1)
        s.server.push_event(
            {'charts': [{'id': 999, 'bars': [bar('2017-04-13T11:00:00.000Z')]}]},
            event='chart',
        )
        s.server.push_event({'charts': [{'id': 32, 'eoh': True}]}, event='chart')
        assert chart.wait_for_history(WAIT)
        assert chart.bars() == []
    finally:
        s.close()


def test_cancelling_a_chart_uses_the_real_time_id():
    s = md({'md/getchart': {'historicalId': 32, 'realtimeId': 31}})
    try:
        s.connect()
        chart = s.chart('ABCZ6', as_much_as_elements=1)
        chart.cancel()
        assert s.server.body_of('md/cancelchart') == {'subscriptionId': 31}
    finally:
        s.close()


def test_a_refused_chart_raises():
    s = md({'md/getchart': {'errorText': 'no such symbol'}})
    try:
        s.connect()
        with pytest.raises(TradovateError, match='no such symbol'):
            s.chart('ABCZ6', as_much_as_elements=1)
        assert s._charts == []
    finally:
        s.close()


# ── ticks ───────────────────────────────────────────────────────────────────

PACKET = {
    'id': 16335,
    's': 'db',
    'td': 20190718,
    'bp': 11917,
    'bt': 1563421179735,
    'ts': 0.25,
    'tks': [
        {'t': 0, 'p': 0, 's': 3, 'b': -1, 'a': 0, 'bs': 122, 'as': 28, 'id': 11768401},
        {'t': 250, 'p': 2, 's': 1, 'id': 11768402},
    ],
}


def test_ticks_are_decoded_the_way_tradovate_documents():
    """timestamp = bt + t; price = (bp + p) * ts; bid and ask the same way."""
    first, second = decode_ticks(PACKET)
    assert first.id == 11768401
    assert first.timestamp_ms == 1563421179735
    assert first.price == 11917 * 0.25
    assert first.size == 3
    assert first.bid == (11917 - 1) * 0.25
    assert first.ask == 11917 * 0.25
    assert (first.bid_size, first.ask_size) == (122, 28)
    assert second.timestamp_ms == 1563421179735 + 250
    assert second.price == (11917 + 2) * 0.25
    assert second.bid is None and second.ask is None
    assert first.time == datetime.fromtimestamp(1563421179.735, tz=timezone.utc)


def test_a_tick_chart_delivers_ticks():
    s = md({'md/getchart': {'historicalId': 16335, 'realtimeId': 16336}})
    ticks = []
    try:
        s.connect()
        chart = s.chart(
            'ABCZ6',
            underlying_type=ChartType.TICK,
            as_much_as_elements=2,
            on_tick=ticks.append,
        )
        assert (
            s.server.body_of('md/getchart')['chartDescription']['underlyingType']
            == 'Tick'
        )
        s.server.push_event(
            {'charts': [PACKET, {'id': 16335, 'eoh': True}]}, event='chart'
        )
        assert chart.wait_for_history(WAIT)
        assert [t.id for t in chart.ticks()] == [11768401, 11768402]
        assert len(ticks) == 2
    finally:
        s.close()


# ── the blocking calls ──────────────────────────────────────────────────────


def answering(server, packets_for):
    """Make ``md/getchart`` push its packets as soon as it is asked."""
    counter = {'n': 0}

    def reply(body):
        counter['n'] += 1
        ids = {'historicalId': 1000 + counter['n'], 'realtimeId': 2000 + counter['n']}
        packets = packets_for(body, counter['n'])
        frames = [dict(p, id=ids['historicalId']) for p in packets]
        frames.append({'id': ids['historicalId'], 'eoh': True})
        server.push_event({'charts': frames}, event='chart')
        return ids

    server.replies['md/getchart'] = reply
    return counter


def test_bars_is_one_blocking_call():
    s = md()
    answering(
        s.server,
        lambda body, n: [
            {
                'bars': [
                    bar('2017-04-13T11:00:00.000Z'),
                    bar('2017-04-13T10:00:00.000Z'),
                ]
            }
        ],
    )
    try:
        s.connect()
        bars = s.bars('ABCZ6', count=2, element_size=60)
        assert [b.timestamp for b in bars] == [
            '2017-04-13T10:00:00.000Z',
            '2017-04-13T11:00:00.000Z',
        ]
        sent = s.server.body_of('md/getchart')
        assert sent['timeRange'] == {'asMuchAsElements': 2}
        assert sent['chartDescription']['elementSize'] == 60
        # the subscription is not left running
        assert s.server.body_of('md/cancelchart') == {'subscriptionId': 2001}
        assert s._charts == []
    finally:
        s.close()


def test_ticks_is_one_blocking_call():
    s = md()
    answering(s.server, lambda body, n: [PACKET])
    try:
        s.connect()
        ticks = s.ticks('ABCZ6', count=2)
        assert [t.price for t in ticks] == [11917 * 0.25, 11919 * 0.25]
        assert s.server.body_of('md/getchart')['chartDescription'] == {
            'underlyingType': 'Tick',
            'elementSize': 1,
            'elementSizeUnit': 'UnderlyingUnits',
            'withHistogram': False,
        }
    finally:
        s.close()


def test_no_end_of_history_times_out_and_still_cancels():
    s = md({'md/getchart': {'historicalId': 32, 'realtimeId': 31}})
    try:
        s.connect()
        with pytest.raises(TimeoutError):
            s.bars('ABCZ6', count=2, timeout=0.05)
        assert s.server.body_of('md/cancelchart') == {'subscriptionId': 31}
    finally:
        s.close()


def test_history_pages_until_it_reaches_the_start():
    """Tradovate caps one request; the next one ends at the oldest bar held."""
    pages = {
        1: ['2020-01-05T00:00:00.000Z', '2020-01-04T00:00:00.000Z'],
        2: ['2020-01-04T00:00:00.000Z', '2020-01-03T00:00:00.000Z'],
        3: [
            '2020-01-03T00:00:00.000Z',
            '2020-01-02T00:00:00.000Z',
            '2020-01-01T00:00:00.000Z',
        ],
    }
    s = md()
    counter = answering(
        s.server, lambda body, n: [{'bars': [bar(t) for t in pages[n]]}]
    )
    try:
        s.connect()
        bars = s.history(
            'ABCZ6',
            since='2020-01-01T00:00:00.000Z',
            until='2020-01-05T00:00:00.000Z',
            underlying_type=ChartType.DAILY_BAR,
        )
        assert [b.timestamp[:10] for b in bars] == [
            '2020-01-01',
            '2020-01-02',
            '2020-01-03',
            '2020-01-04',
            '2020-01-05',
        ]
        assert counter['n'] == 3
        ranges = [b['timeRange'] for b in s.server.bodies_of('md/getchart')]
        assert ranges == [
            {
                'closestTimestamp': '2020-01-05T00:00:00.000Z',
                'asFarAsTimestamp': '2020-01-01T00:00:00.000Z',
            },
            {
                'closestTimestamp': '2020-01-04T00:00:00.000Z',
                'asFarAsTimestamp': '2020-01-01T00:00:00.000Z',
            },
            {
                'closestTimestamp': '2020-01-03T00:00:00.000Z',
                'asFarAsTimestamp': '2020-01-01T00:00:00.000Z',
            },
        ]
        assert s.server.requests.count('md/cancelchart') == 3
    finally:
        s.close()


def test_history_stops_when_a_request_brings_nothing_older():
    """The data may simply not go back as far as was asked."""
    s = md()
    counter = answering(
        s.server,
        lambda body, n: [{'bars': [bar('2020-01-04T00:00:00.000Z')]}],
    )
    try:
        s.connect()
        bars = s.history(
            'ABCZ6', since='2019-01-01T00:00:00.000Z', until='2020-01-05T00:00:00.000Z'
        )
        assert [b.timestamp for b in bars] == ['2020-01-04T00:00:00.000Z']
        assert counter['n'] == 2
    finally:
        s.close()


def test_history_with_no_data_returns_nothing():
    s = md()
    counter = answering(s.server, lambda body, n: [])
    try:
        s.connect()
        assert s.history('ABCZ6', since='2019-01-01T00:00:00.000Z') == []
        assert counter['n'] == 1
    finally:
        s.close()


# ── reconnection ────────────────────────────────────────────────────────────


def test_subscriptions_are_sent_again_after_a_reconnect():
    first = FakeServer({'md/getchart': {'historicalId': 32, 'realtimeId': 31}})
    second = FakeServer({'md/getchart': {'historicalId': 42, 'realtimeId': 41}})
    s = md(servers=[first, second])
    quotes, bars = [], []
    try:
        s.connect()
        s.subscribe_quote('ABCZ6', quotes.append)
        s.subscribe_quote('ABCZ6', lambda q: None)
        s.subscribe_dom('XYZH7')
        chart = s.chart('ABCZ6', as_much_as_elements=5, on_bar=bars.append)
        first.drop()
        assert until(lambda: s.reconnects == 1)
        assert second.requests == [
            'authorize',
            'md/subscribequote',
            'md/subscribedom',
            'md/getchart',
        ]
        assert second.body_of('md/subscribequote') == {'symbol': 'ABCZ6'}
        assert second.body_of('md/getchart') == first.body_of('md/getchart')
        # the chart follows its new ids, and the handlers are still attached
        assert chart.ids == (41, 42)
        second.push_event({'quotes': [QUOTE]}, event='md')
        second.push_event(
            {'charts': [{'id': 42, 'bars': [bar('2017-04-13T11:00:00.000Z')]}]},
            event='chart',
        )
        assert until(lambda: len(quotes) == 1 and len(bars) == 1)
    finally:
        s.close()


def test_a_cancelled_subscription_is_not_brought_back():
    first, second = FakeServer(), FakeServer()
    s = md(servers=[first, second])
    try:
        s.connect()
        s.subscribe_quote('ABCZ6').cancel()
        first.drop()
        assert until(lambda: s.reconnects == 1)
        assert second.requests == ['authorize']
    finally:
        s.close()


def test_packets_that_beat_the_reply_are_not_lost():
    """The receive thread can see a chart's first packets before the caller
    has been handed the ids they carry."""
    s = md()

    def reply(_body):
        s.server.push_event(
            {
                'charts': [
                    {'id': 32, 'bars': [bar('2017-04-13T11:00:00.000Z')]},
                    {'id': 32, 'eoh': True},
                ]
            },
            event='chart',
        )
        return {'historicalId': 32, 'realtimeId': 31}

    s.server.replies['md/getchart'] = reply
    try:
        s.connect()
        chart = s.chart('ABCZ6', as_much_as_elements=1)
        assert chart.wait_for_history(WAIT)
        assert len(chart.bars()) == 1
    finally:
        s.close()


def test_a_handler_can_cancel_its_own_subscription():
    s = md(request_timeout=1.0)
    seen = []
    holder = {}

    def once(quote):
        seen.append(quote)
        holder['sub'].cancel()

    try:
        s.connect()
        holder['sub'] = s.subscribe_quote('ABCZ6', once)
        s.server.push_event({'quotes': [QUOTE]}, event='md')
        assert until(lambda: 'md/unsubscribequote' in s.server.requests)
        s.server.push_event({'quotes': [QUOTE]}, event='md')
        assert until(lambda: s.quote('ABCZ6') is not seen[0])
        assert len(seen) == 1
    finally:
        s.close()
