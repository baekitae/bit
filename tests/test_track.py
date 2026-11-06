import pandas as pd
import pytest

from src.backtest.metrics import summarize
from src.backtest.replay import replay
from src.strategies.base import BUY, SELL
from src.tracks.tracks import TrackA, TrackC
from tests.conftest import BAR, T0, UNIT, Scripted, make_candles, random_walk, run_bars, store_candles

FEE = SLIP = 0.0005


def flat(n, price=100.0):
    return [(price, price * 1.01, price * 0.99, price)] * n


def orders(conn, run_id="t"):
    return pd.read_sql_query("SELECT * FROM paper_orders WHERE run_id=? ORDER BY id", conn, params=(run_id,))


def trades(conn, run_id="t"):
    return pd.read_sql_query("SELECT * FROM trades WHERE run_id=? ORDER BY id", conn, params=(run_id,))


def test_entry_fills_at_next_bar_open(conn, cfg):
    rows = flat(5)
    rows[3] = (103.0, 104.0, 102.0, 103.5)  # 신호 다음 봉
    df = make_candles(rows)
    t = TrackA(conn, "t", cfg, Scripted({2: BUY}), "v1")
    run_bars(t, df)
    o = orders(conn).iloc[0]
    assert o["status"] == "filled"
    assert o["created_bar_ts"] == T0 + 2 * BAR
    assert o["fill_bar_ts"] == T0 + 3 * BAR
    assert o["ref_price"] == pytest.approx(103.0)                # 다음 봉 시가
    assert o["fill_price"] == pytest.approx(103.0 * (1 + SLIP))
    # 목표 금액 = 신호 시점 자산의 30%
    assert o["target_notional"] == pytest.approx(300_000)
    pos = conn.execute("SELECT * FROM positions WHERE run_id='t'").fetchone()
    assert pos["stop_loss"] == pytest.approx(100.0 * 0.95)


def test_signal_exit_next_open(conn, cfg):
    rows = flat(8)
    rows[5] = (110.0, 111.0, 109.0, 110.0)
    df = make_candles(rows)
    t = TrackA(conn, "t", cfg, Scripted({1: BUY, 4: SELL}), "v1")
    run_bars(t, df)
    tr = trades(conn).iloc[0]
    assert tr["exit_kind"] == "exit_signal"
    assert tr["exit_ts"] == T0 + 5 * BAR
    assert tr["exit_price"] == pytest.approx(110.0 * (1 - SLIP))
    assert tr["pnl"] > 0


def test_stop_in_entry_bar_and_gap(conn, cfg):
    rows = flat(6)
    rows[2] = (100.0, 101.0, 90.0, 92.0)     # 진입 봉에서 바로 손절가(95) 이탈
    df = make_candles(rows)
    t = TrackA(conn, "t", cfg, Scripted({1: BUY}), "v1")
    run_bars(t, df)
    tr = trades(conn).iloc[0]
    assert tr["exit_kind"] == "stop" and tr["exit_ts"] == T0 + 2 * BAR
    assert tr["exit_price"] == pytest.approx(95.0 * (1 - SLIP))


def test_stop_gap_down_fills_at_open(conn, cfg):
    rows = flat(6)
    rows[3] = (85.0, 86.0, 80.0, 82.0)       # 시가부터 손절가 아래
    df = make_candles(rows)
    t = TrackA(conn, "t", cfg, Scripted({1: BUY}), "v1")
    run_bars(t, df)
    tr = trades(conn).iloc[0]
    assert tr["exit_price"] == pytest.approx(85.0 * (1 - SLIP))


def test_stop_beats_pending_exit_same_bar(conn, cfg):
    rows = flat(6)
    rows[3] = (100.0, 101.0, 90.0, 92.0)
    df = make_candles(rows)
    # 2번 봉에서 청산 신호 → 3번 봉 시가에 청산 체결 (시가가 먼저이므로 손절보다 먼저)
    t = TrackA(conn, "t", cfg, Scripted({1: BUY, 2: SELL}), "v1")
    run_bars(t, df)
    tr = trades(conn)
    assert len(tr) == 1 and tr.iloc[0]["exit_kind"] == "exit_signal"


def test_equity_every_bar_and_cash_conserved(conn, cfg):
    df = random_walk(400, seed=1)
    t = TrackA(conn, "t", cfg, Scripted({50: BUY, 80: SELL, 120: BUY, 200: SELL, 300: BUY}), "v1")
    run_bars(t, df)
    eq = pd.read_sql_query("SELECT * FROM equity WHERE run_id='t' ORDER BY bar_ts", conn)
    assert len(eq) == len(df)
    tr = trades(conn)
    st = conn.execute("SELECT cash FROM tracks WHERE run_id='t'").fetchone()
    pos = conn.execute("SELECT entry_cost FROM positions WHERE run_id='t'").fetchone()
    open_cost = pos["entry_cost"] if pos else 0.0
    assert st["cash"] == pytest.approx(1_000_000 + tr["pnl"].sum() - open_cost)


def test_position_never_exceeds_30pct(conn, cfg):
    df = random_walk(400, seed=2)
    t = TrackA(conn, "t", cfg, Scripted({i: BUY for i in range(0, 400, 7)} | {i: SELL for i in range(3, 400, 7)}), "v1")
    run_bars(t, df)
    o = orders(conn)
    buys = o[(o["side"] == "buy") & (o["status"] == "filled")]
    eq = pd.read_sql_query("SELECT bar_ts, equity FROM equity WHERE run_id='t'", conn).set_index("bar_ts")["equity"]
    for _, b in buys.iterrows():
        assert b["target_notional"] <= 0.30 * eq[b["created_bar_ts"]] + 1e-6


def test_missing_stop_is_rejected(conn, cfg):
    s = Scripted({1: BUY})
    s.generate_orig = s.generate
    s.generate = lambda c: (lambda sig: (setattr(sig, "stop_loss", None), sig)[1])(s.generate_orig(c))
    t = TrackA(conn, "t", cfg, s, "v1")
    run_bars(t, make_candles(flat(4)))
    rc = conn.execute("SELECT * FROM risk_checks WHERE run_id='t'").fetchone()
    assert rc["approved"] == 0 and "손절가" in rc["reasons"]
    assert orders(conn).empty


def test_consecutive_losses_trigger_cooldown(conn, cfg):
    rows = []
    script = {}
    # 매 3봉마다 진입 → 다음 봉에서 손절 (5번 연속 손실)
    for k in range(6):
        base = len(rows)
        rows += flat(1)                                # 신호 봉
        rows += [(100.0, 101.0, 90.0, 95.0)]           # 진입 봉에서 손절
        rows += flat(1)
        script[base] = BUY
    df = make_candles(rows)
    t = TrackA(conn, "t", cfg, Scripted(script), "v1")
    run_bars(t, df)
    tr = trades(conn)
    assert len(tr) == 5          # 6번째 진입은 차단
    rc = pd.read_sql_query("SELECT * FROM risk_checks WHERE run_id='t' ORDER BY id", conn)
    assert rc.iloc[-1]["approved"] == 0 and "연속 손실" in rc.iloc[-1]["reasons"]


def test_daily_loss_limit(conn, cfg):
    cfg["risk"]["daily_loss_pct"] = 0.01
    rows = flat(1) + [(100.0, 101.0, 60.0, 61.0)] + flat(1, 61.0) + flat(3, 61.0)
    df = make_candles(rows)
    # 0번 봉 진입 신호 → 1번 봉에서 40% 급락, 손절은 95에서 → 손실 약 1.5%
    t = TrackA(conn, "t", cfg, Scripted({0: BUY, 2: BUY}), "v1")
    run_bars(t, df)
    rc = pd.read_sql_query("SELECT * FROM risk_checks WHERE run_id='t' ORDER BY id", conn)
    assert rc.iloc[0]["approved"] == 1
    assert rc.iloc[1]["approved"] == 0 and "일일 손실" in rc.iloc[1]["reasons"]


def test_day_rollover_resets_daily_loss(conn, cfg):
    cfg["risk"]["daily_loss_pct"] = 0.01
    per_day = 24 * 60 // UNIT  # 6
    rows = flat(1) + [(100.0, 101.0, 60.0, 61.0)] + flat(per_day, 61.0) + flat(3, 61.0)
    df = make_candles(rows)
    nxt = per_day + 1  # 다음 날 봉
    t = TrackA(conn, "t", cfg, Scripted({0: BUY, nxt: BUY}), "v1")
    run_bars(t, df)
    rc = pd.read_sql_query("SELECT * FROM risk_checks WHERE run_id='t' ORDER BY id", conn)
    assert rc.iloc[-1]["approved"] == 1


def test_data_gap_blocks_entry_and_cancels_fill(conn, cfg):
    df = make_candles(flat(6))
    df.loc[3:, "ts"] += BAR      # 3번 봉 앞에 누락
    t = TrackA(conn, "t", cfg, Scripted({2: BUY, 4: BUY}), "v1")  # 누락 뒤 봉은 ts 기준 4번
    run_bars(t, df)
    o = orders(conn)
    assert o.iloc[0]["status"] == "cancelled"
    rc = pd.read_sql_query("SELECT * FROM risk_checks WHERE run_id='t' ORDER BY id", conn)
    assert "데이터 이상" in rc.iloc[1]["reasons"]


def test_resume_after_restart_is_identical(conn, cfg):
    df = random_walk(600, seed=7)
    store_candles(conn, df)
    replay(conn, cfg, "full", ["A"])

    store_candles_partial = df.iloc[:350]
    conn.execute("DELETE FROM candles")
    store_candles(conn, store_candles_partial)
    replay(conn, cfg, "resumed", ["A"])
    conn.execute("DELETE FROM candles")
    store_candles(conn, df)
    replay(conn, cfg, "resumed", ["A"])      # 새 객체로 이어서 실행 (DB에서 복원)

    a = pd.read_sql_query("SELECT bar_ts, equity FROM equity WHERE run_id='full' ORDER BY bar_ts", conn)
    b = pd.read_sql_query("SELECT bar_ts, equity FROM equity WHERE run_id='resumed' ORDER BY bar_ts", conn)
    pd.testing.assert_frame_equal(a, b)
    assert len(trades(conn, "full")) > 0
    assert len(trades(conn, "full")) == len(trades(conn, "resumed"))


def test_full_replay_with_ma_cross(conn, cfg):
    df = random_walk(1500, seed=11)
    store_candles(conn, df)
    n = replay(conn, cfg, "r", ["A"])
    assert n == len(df)
    s = summarize(conn, "r", "A", UNIT)
    assert s["trades"] > 5
    assert -1 < s["mdd"] <= 0
    sig = conn.execute("SELECT COUNT(*) FROM signals WHERE run_id='r'").fetchone()[0]
    assert sig > 0


def test_c_is_placeholder(conn, cfg):
    with pytest.raises(NotImplementedError):
        TrackC(conn, "t", cfg, Scripted({}), "v1")
