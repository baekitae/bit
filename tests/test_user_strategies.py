import numpy as np
import pandas as pd
import pytest

from src.backtest.engine import Market, Signals, run_backtest
from src.backtest.infinite import InfiniteParams, run_infinite
from src.data.upbit import resample
from src.paper.fills import Costs
from src.strategies.base import BUY
from src.strategies.titan import TitanRest
from tests.conftest import BAR, T0, Scripted, make_candles

NO_COST = Costs(0.0, 0.0)


def test_trailing_stop_raises_and_exits(cfg):
    # 진입 후 120까지 오르고 → 최고가 대비 -10% 아래로 하락
    rows = [(100, 101, 99, 100)] * 3 + [(100, 121, 99, 120), (120, 120, 112, 113), (113, 114, 105, 106)]
    df = make_candles(rows)
    s = Scripted({1: BUY}, stop_pct=0.20)
    s.params = {"trail_pct": 0.10}
    m = Market(df, 240)
    r = run_backtest(m, Signals.from_strategy(s, m), cfg)
    t = r.trades.iloc[0]
    assert t["exit_kind"] == "trail_stop"
    assert t["exit_idx"] == 5                       # 4번 봉 저가 112 > 108.9 → 5번 봉에서 이탈
    assert t["exit_price"] == pytest.approx(121 * 0.9 * (1 - 0.0005))


def test_trailing_not_applied_inside_same_bar(cfg):
    # 같은 봉에서 고점 후 급락해도, 그 봉 고점은 다음 봉부터 반영 (보수적)
    rows = [(100, 101, 99, 100)] * 3 + [(100, 130, 95, 96), (96, 97, 95, 96)]
    s = Scripted({2: BUY}, stop_pct=0.20)
    s.params = {"trail_pct": 0.10}
    df = make_candles(rows)
    m = Market(df, 240)
    r = run_backtest(m, Signals.from_strategy(s, m), cfg)
    t = r.trades.iloc[0]
    assert t["exit_idx"] == 4 and t["exit_price"] == pytest.approx(96 * (1 - 0.0005))  # 시가 96 < 117 → 시가 체결


def test_titan_entry_condition():
    n = 120
    close = np.concatenate([np.linspace(100, 200, 100), [195, 192, 190] + [190] * 17])
    rows = [(c, c * 1.005, c * 0.995, c) for c in close]
    df = make_candles(rows)
    s = TitanRest({"ma_mid": 20, "ma_long": 60, "rs_period": 60})
    out = s.compute(df)
    buys = np.flatnonzero(out["action"] == BUY)
    assert len(buys) >= 1
    i = buys[0]
    dev = out["deviation"].iloc[i]
    assert -0.02 <= dev <= 0.03
    assert out["sma_short"].iloc[i] > out["sma_mid"].iloc[i] > out["sma_long"].iloc[i]


def test_resample_daily():
    df = make_candles([(i, i + 2, i - 1, i + 1) for i in range(1, 15)])  # 14개 4시간봉 = 2일 + 2개
    d = resample(df, 240, 1440)
    assert len(d) == 2                               # 마지막 미완성 일봉 제외
    first = df.iloc[:6]
    assert d["open"].iloc[0] == first["open"].iloc[0] and d["close"].iloc[0] == first["close"].iloc[-1]
    assert d["high"].iloc[0] == first["high"].max() and d["low"].iloc[0] == first["low"].min()


def daily(prices, high_mult=1.0):
    return pd.DataFrame({"ts": [T0 + i * 86400 for i in range(len(prices))],
                         "open": prices, "high": [p * high_mult for p in prices],
                         "low": prices, "close": prices})


def test_infinite_take_profit_cycle():
    prices = [100.0] * 3 + [95.0, 90.0, 100.0, 104.0]
    d = daily(prices)
    d.loc[6, "high"] = 120.0
    r = run_infinite(d, InfiniteParams(splits=40), NO_COST, 1_000_000)
    c = r.cycles.iloc[0]
    assert c["result"] == "익절"
    # 평단가 +10% 에 전량 매도 → 사이클 수익 > 0, 끝나면 현금만
    assert c["pnl_pct"] > 0 and r.exposure[6] == 0


def test_infinite_quarter_stop_when_cash_runs_out():
    prices = list(np.linspace(100, 30, 120))       # 끝없이 하락
    r = run_infinite(daily(prices), InfiniteParams(splits=40), NO_COST, 1_000_000)
    c = r.cycles.iloc[0]
    assert c["result"] == "진행중" and c["quarter_stops"] >= 1
    assert r.exposure.max() <= 1.0 + 1e-9 and (r.equity > 0).all()


def test_infinite_loc_rules():
    # 종가가 평단+5% 보다 높으면 매수 없음
    prices = [100.0, 110.0, 110.0]
    r = run_infinite(daily(prices), InfiniteParams(splits=40), NO_COST, 1_000_000)
    assert r.cycles.iloc[0]["splits_used"] == pytest.approx(1.0)
    # 평단 이하이면 하루 1회분(0.5+0.5)
    r = run_infinite(daily([100.0, 99.0]), InfiniteParams(splits=40), NO_COST, 1_000_000)
    assert r.cycles.iloc[0]["splits_used"] == pytest.approx(2.0)
