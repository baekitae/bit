import copy

import pandas as pd
import pytest

from src.data.upbit import load_candles
from src.live import LiveRunner
from tests.conftest import BAR, BASE_CFG, T0, random_walk
from tests.test_upbit import FakeResp, fmt

DAY = 86400


class MarketSession:
    """업비트처럼 응답하되, 지금 시각(clock)까지 '마감 + 진행 중' 봉만 존재하는 가짜 시장."""

    def __init__(self, df, clock):
        self.df = df.set_index("ts")
        self.clock = clock
        self.fail = False

    def get(self, url, params, timeout):
        if self.fail:
            raise ConnectionError("network down")
        from datetime import datetime, timezone
        to = params.get("to")
        to_ts = int(datetime.strptime(to, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()) if to else None
        avail = [t for t in self.df.index if t <= self.clock[0] and (to_ts is None or t < to_ts)]
        page = sorted(avail, reverse=True)[: params["count"]]
        return FakeResp([{
            "candle_date_time_utc": fmt(t), "opening_price": self.df.loc[t, "open"],
            "high_price": self.df.loc[t, "high"], "low_price": self.df.loc[t, "low"],
            "trade_price": self.df.loc[t, "close"], "candle_acc_trade_volume": 1.0,
            "candle_acc_trade_price": 1.0} for t in page])


@pytest.fixture
def setup(conn):
    cfg = copy.deepcopy(BASE_CFG)
    cfg["data"] = {"history_years": 1, "request_interval_sec": 0, "max_candle_delay_bars": 1}
    cfg["live"] = {"delay_after_close_sec": 15, "max_late_sec": 900}
    cfg["strategy"]["window_bars"] = 200
    cfg["tracks"]["enabled"] = ["A1", "A5"]
    cfg["tracks"]["defs"] = {
        "A1": {"type": "rule", "strategy": "ma_cross", "timeframe": 240,
               "params": {"fast": 5, "slow": 20, "atr_period": 14, "atr_stop_mult": 2.0}},
        "A5": {"type": "rule", "strategy": "titan_rest", "timeframe": 1440,
               "params": {"ma_mid": 10, "ma_long": 30, "rs_period": 20, "band_high": 0.05}},
    }
    df = random_walk(2400, seed=12)          # 400일치 4시간봉
    clock = [T0 + 600 * BAR]                 # 첫 시작 시각 (봉 시작 기준)
    sess = MarketSession(df, clock)
    from src.data.upbit import UpbitClient
    client = UpbitClient(session=sess, interval_sec=0, sleep=lambda s: None)
    now = {"t": clock[0] + BAR + 20}         # 600번 봉 마감 20초 후

    def set_now(t):
        now["t"] = t
        clock[0] = (t // BAR) * BAR          # 진행 중인 봉까지 존재

    set_now(now["t"])
    runner = LiveRunner(conn, cfg, client, run_id="live", now=lambda: now["t"], log=lambda s: None)
    return runner, set_now, df, sess


def last_bar(conn, name):
    return conn.execute("SELECT last_bar_ts FROM tracks WHERE run_id='live' AND track=?", (name,)).fetchone()[0]


def test_first_start_and_normal_steps(conn, setup):
    runner, set_now, df, _ = setup
    s = runner.step()
    assert ("A1",) == tuple(n for n, _ in s["processed"] if n == "A1")      # 마감 직후 → 정상 처리
    assert last_bar(conn, "A1") == T0 + 600 * BAR
    for k in range(601, 700):
        set_now(T0 + (k + 1) * BAR + 20)
        runner.step()
    assert last_bar(conn, "A1") == T0 + 699 * BAR
    eq = conn.execute("SELECT COUNT(*) FROM equity WHERE run_id='live' AND track='A1'").fetchone()[0]
    assert eq == 100
    # 일봉 트랙은 하루에 한 번만 처리
    d = conn.execute("SELECT COUNT(*) FROM equity WHERE run_id='live' AND track='A5'").fetchone()[0]
    assert 15 <= d <= 18


def test_restart_gap_no_retroactive_trading(conn, setup):
    runner, set_now, df, _ = setup
    runner.step()
    set_now(T0 + 650 * BAR + 20)             # 49개 봉 동안 꺼져 있었음
    s = runner.step()
    assert any(c[0] == "A1" and c[1] == 48 for c in s["caught_up"])
    assert ("A1",) == tuple(n for n, _ in s["processed"] if n == "A1")
    # 놓친 구간에서 생긴 주문·거래 없음
    n = conn.execute("""SELECT COUNT(*) FROM paper_orders WHERE run_id='live' AND track='A1'
                        AND created_bar_ts > ? AND created_bar_ts < ?""",
                     (T0 + 600 * BAR, T0 + 649 * BAR)).fetchone()[0]
    assert n == 0
    log = conn.execute("SELECT detail FROM run_log WHERE event='missed_bars'").fetchall()
    assert any("A1" in r[0] for r in log)


def test_late_start_does_not_trade_old_bar(conn, setup):
    runner, set_now, df, _ = setup
    set_now(T0 + 601 * BAR + 2 * 3600)       # 마감 2시간 뒤 시작 → 그 봉으로 매매 안 함
    conn.execute("DELETE FROM tracks")
    s = runner.step()
    assert not [p for p in s["processed"] if p[0] == "A1"]


def test_delayed_stop_on_restart(conn, setup):
    runner, set_now, df, _ = setup
    runner.step()
    # A1 에 포지션을 강제로 만들고, 놓친 구간에 손절가를 건드리게 한다
    acc = runner.tracks[0].account
    oid = acc.place_order("buy", "entry", T0 + 600 * BAR, target_notional=100000, stop_loss=1e12)
    acc.fill_buy(oid, T0 + 600 * BAR, float(df["open"].iloc[600]), 100000, 1e12)
    conn.commit()
    set_now(T0 + 610 * BAR + 20)
    runner.step()
    t = conn.execute("SELECT exit_kind, exit_price FROM trades WHERE run_id='live' AND track='A1'").fetchone()
    assert t["exit_kind"] == "delayed_stop"


def test_network_failure_is_logged_and_recovers(conn, setup):
    runner, set_now, df, sess = setup
    runner.step()
    sess.fail = True
    set_now(T0 + 602 * BAR + 20)
    s = runner.step()
    assert s["error"] and "캔들 수집 실패" in s["error"]
    sess.fail = False
    set_now(T0 + 603 * BAR + 20)
    s = runner.step()
    assert s["error"] is None
    assert last_bar(conn, "A1") == T0 + 602 * BAR


def test_next_run_time(setup):
    runner, *_ = setup
    assert runner.next_run_at(T0 + 10) == T0 + BAR + 15
