from datetime import datetime, timezone

from src.data.upbit import UpbitClient, find_gaps, load_candles, sync_candles
from tests.conftest import BAR, T0, UNIT


def fmt(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


class FakeResp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeSession:
    """업비트처럼: `to` 이전(배타적) 캔들을 최신순으로 최대 count 개."""

    def __init__(self, all_ts, fail_first=False):
        self.all_ts = sorted(all_ts)
        self.calls = []
        self.fail_first = fail_first

    def get(self, url, params, timeout):
        self.calls.append(dict(params))
        if self.fail_first:
            self.fail_first = False
            return FakeResp([], 429)
        to = params.get("to")
        to_ts = int(datetime.strptime(to, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()) if to else None
        cand = [t for t in self.all_ts if to_ts is None or t < to_ts]
        page = sorted(cand, reverse=True)[: params["count"]]
        return FakeResp([{
            "candle_date_time_utc": fmt(t), "opening_price": 100, "high_price": 110,
            "low_price": 90, "trade_price": 105, "candle_acc_trade_volume": 1.0,
            "candle_acc_trade_price": 105.0,
        } for t in page])


def client(sess):
    return UpbitClient(session=sess, interval_sec=0, sleep=lambda s: None)


def test_sync_paginates_and_drops_open_candle(conn):
    n = 650
    all_ts = [T0 + i * BAR for i in range(n)]
    now = all_ts[-1] + BAR // 2          # 마지막 봉은 진행 중
    sess = FakeSession(all_ts)
    sync_candles(conn, client(sess), "KRW-BTC", UNIT, since_ts=T0, now_ts=now)
    df = load_candles(conn, "KRW-BTC", UNIT)
    assert len(df) == n - 1
    assert df["ts"].iloc[-1] == all_ts[-2]
    assert find_gaps(df, UNIT) == []
    assert len(sess.calls) >= 4


def test_sync_update_fetches_only_new(conn):
    all_ts = [T0 + i * BAR for i in range(300)]
    sess = FakeSession(all_ts)
    sync_candles(conn, client(sess), "KRW-BTC", UNIT, since_ts=T0, now_ts=all_ts[-1] + BAR)
    more = all_ts + [all_ts[-1] + BAR * k for k in range(1, 4)]
    sess2 = FakeSession(more)
    sync_candles(conn, client(sess2), "KRW-BTC", UNIT, since_ts=T0, now_ts=more[-1] + BAR)
    assert len(sess2.calls) == 1
    assert len(load_candles(conn, "KRW-BTC", UNIT)) == 303


def test_sync_respects_since(conn):
    all_ts = [T0 + i * BAR for i in range(500)]
    since = all_ts[100]
    sync_candles(conn, client(FakeSession(all_ts)), "KRW-BTC", UNIT, since_ts=since, now_ts=all_ts[-1] + BAR)
    df = load_candles(conn, "KRW-BTC", UNIT)
    assert df["ts"].iloc[0] == since and len(df) == 400


def test_retry_on_429(conn):
    all_ts = [T0 + i * BAR for i in range(10)]
    sess = FakeSession(all_ts, fail_first=True)
    sync_candles(conn, client(sess), "KRW-BTC", UNIT, since_ts=T0, now_ts=all_ts[-1] + BAR)
    assert len(load_candles(conn, "KRW-BTC", UNIT)) == 10
