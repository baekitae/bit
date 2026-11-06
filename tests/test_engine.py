"""빠른 백테스트 엔진과 트랙 엔진(실시간과 같은 코드)의 결과가 같은지 검증."""
import numpy as np
import pandas as pd
import pytest

from src.backtest.engine import Market, Signals, run_backtest
from src.backtest.metrics import result_metrics, summarize
from src.backtest.regime import REGIMES, classify, regime_breakdown
from src.backtest.replay import replay
from src.backtest.study import Study, neighbors, param_grid
from src.strategies import REGISTRY, make_strategy
from tests.conftest import UNIT, random_walk, store_candles

CASES = [
    ("ma_cross", {"fast": 5, "slow": 20}),
    ("donchian", {"entry_period": 20, "exit_period": 10, "atr_stop_mult": 2.0}),
    ("rsi_reversion", {"rsi_period": 7, "oversold": 30.0, "exit_level": 55.0}),
    ("bollinger_reversion", {"bb_period": 20, "bb_std": 2.0, "exit_std": 0.0}),
    ("titan_rest", {"ma_mid": 15, "ma_long": 40, "rs_period": 20, "band_high": 0.05, "trail_pct": 0.06}),
]


@pytest.mark.parametrize("name,params", CASES)
def test_engine_matches_track_replay(conn, cfg, name, params):
    df = random_walk(1200, seed=5)
    df.loc[700:, "ts"] += UNIT * 60          # 캔들 누락 1곳 포함
    store_candles(conn, df)
    cfg["strategy"].update({"name": name, "params": params, "window_bars": 300})
    replay(conn, cfg, "r", ["A"])

    market = Market(df, UNIT)
    res = run_backtest(market, Signals.from_strategy(make_strategy(name, params), market), cfg)

    eq_db = pd.read_sql_query("SELECT equity FROM equity WHERE run_id='r' ORDER BY bar_ts", conn)["equity"]
    np.testing.assert_allclose(res.equity, eq_db.to_numpy(), rtol=1e-9)
    tr_db = pd.read_sql_query("SELECT pnl, exit_kind FROM trades WHERE run_id='r' ORDER BY id", conn)
    assert len(tr_db) == len(res.trades) > 3
    np.testing.assert_allclose(res.trades["pnl"], tr_db["pnl"], rtol=1e-9)
    assert list(res.trades["exit_kind"]) == list(tr_db["exit_kind"])
    if name == "titan_rest":
        assert "trail_stop" in set(tr_db["exit_kind"])     # 트레일링 스탑이 실제로 작동

    m1 = result_metrics(market, res)
    m2 = summarize(conn, "r", "A", UNIT)
    for k in ("total_return", "mdd", "sharpe", "trades", "win_rate", "exposure", "bh_return"):
        assert m1[k] == pytest.approx(m2[k], rel=1e-9, nan_ok=True)


@pytest.mark.parametrize("name", list(REGISTRY))
def test_generate_equals_compute_last_row(name):
    """실시간 경로(generate)와 백테스트 경로(compute)가 같은 신호를 낸다."""
    cls = REGISTRY[name]
    s = cls()
    df = random_walk(800, seed=9)
    full = s.compute(df)
    for i in range(s.min_bars, len(df), 7):
        sig = s.generate(df.iloc[: i + 1])
        assert sig.action == full["action"].iloc[i]
        if sig.action == "buy":
            assert sig.stop_loss == pytest.approx(full["stop_loss"].iloc[i])


@pytest.mark.parametrize("name", list(REGISTRY))
def test_signals_are_events_and_causal(name):
    s = REGISTRY[name]()
    df = random_walk(900, seed=4)
    a = s.compute(df)["action"]
    b = s.compute(df.iloc[:600])["action"]
    assert (a.iloc[:600].to_numpy() == b.to_numpy()).all()        # 미래 데이터가 과거 신호를 바꾸지 않음
    assert (a == "buy").sum() > 0
    assert (a == "sell").sum() > 0 or name == "titan_rest"   # 타이탄은 손절·트레일링으로만 청산
    buys = np.flatnonzero(a.to_numpy() == "buy")
    assert not np.any(np.diff(buys) == 1) or name == "rsi_reversion"  # 연속 봉 매수 신호 없음(사건 기반)


@pytest.mark.parametrize("name", list(REGISTRY))
def test_grid_valid_and_neighbors(name):
    cls = REGISTRY[name]
    g = param_grid(cls)
    assert len(g) >= 50
    for p in g[:20]:
        for q in neighbors(cls, p):
            diff = [k for k in p if p[k] != q[k]]
            assert len(diff) == 1


def test_start_index_uses_warmup_only(cfg):
    df = random_walk(1000, seed=2)
    market = Market(df, UNIT)
    sig = Signals.from_strategy(make_strategy("ma_cross", {"fast": 5, "slow": 20}), market)
    r = run_backtest(market, sig, cfg, start=500)
    assert len(r.equity) == 500 and r.equity[0] == pytest.approx(cfg["tracks"]["initial_cash"])
    assert (r.trades["entry_idx"] >= 500).all()


def test_regime_breakdown_consistent(cfg):
    df = random_walk(1500, seed=6)
    market = Market(df, UNIT)
    reg = classify(df, {"ma_hours": 400, "slope_hours": 80, "slope_threshold": 0.01}, UNIT)
    r = run_backtest(market, Signals.from_strategy(make_strategy("ma_cross", {"fast": 5, "slow": 20}), market), cfg)
    rows = regime_breakdown(market, r, reg)
    assert [x["regime"] for x in rows] == list(REGIMES)
    # 판정불가 구간 수익이 0 이면, 국면별 수익을 곱하면 전체 수익이 된다
    total = np.prod([1 + x["strategy_return"] for x in rows])
    warm = reg == "판정불가"
    if not r.trades.empty and (r.trades["entry_idx"] > np.flatnonzero(warm).max()).all():
        assert total - 1 == pytest.approx(result_metrics(market, r)["total_return"], rel=1e-6)


def test_study_end_to_end(cfg):
    cfg["backtest"] = {
        "train_ratio": 0.7, "walk_forward": {"train_months": 6, "test_months": 3}, "min_trades_train": 3,
        "reject": {"val_sharpe_ratio_min": 0.5, "neighbor_profitable_min": 0.6, "neighbor_sharpe_gap_max": 0.5,
                   "min_trades_total": 30, "cost_stress_mult": 2.0},
    }
    cfg["regime"] = {"ma_hours": 400, "slope_hours": 80, "slope_threshold": 0.01}
    cfg["comparison"] = {"min_signals": 30}
    df = random_walk(2500, seed=8)
    study = Study(Market(df, UNIT), cfg)
    folds = study.folds()
    assert len(folds) >= 2
    for a, b, c, d in folds:
        assert a <= b < c <= d and c == b + 1
    rep = study.evaluate(REGISTRY["ma_cross"])
    assert rep.params in param_grid(REGISTRY["ma_cross"])
    assert rep.walk_forward["oos"]["trades"] >= 0
    assert isinstance(rep.passed, bool)
