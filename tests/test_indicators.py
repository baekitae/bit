import numpy as np
import pandas as pd
import pytest

from src.indicators.core import atr, bollinger, donchian, ema, rsi, sma, volume_ratio
from tests.conftest import random_walk


def test_sma():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    out = sma(s, 3)
    assert out.isna().sum() == 2
    assert out.iloc[-1] == pytest.approx(4.0)


def test_ema_matches_pandas():
    s = pd.Series(np.arange(1, 50, dtype=float))
    assert ema(s, 10).iloc[-1] == pytest.approx(s.ewm(span=10, adjust=False).mean().iloc[-1])


def test_rsi_bounds_and_extremes():
    up = pd.Series(np.arange(1, 40, dtype=float))
    assert rsi(up, 14).iloc[-1] == pytest.approx(100.0)
    down = pd.Series(np.arange(40, 1, -1, dtype=float))
    assert rsi(down, 14).iloc[-1] == pytest.approx(0.0)
    df = random_walk(300)
    r = rsi(df["close"], 14).dropna()
    assert ((r >= 0) & (r <= 100)).all()


def test_atr_constant_range():
    n = 40
    close = pd.Series([100.0] * n)
    high = close + 5
    low = close - 5
    assert atr(high, low, close, 14).iloc[-1] == pytest.approx(10.0)


def test_bollinger_and_donchian():
    df = random_walk(100)
    bb = bollinger(df["close"], 20, 2).dropna()
    assert (bb["upper"] >= bb["mid"]).all() and (bb["lower"] <= bb["mid"]).all()
    dc = donchian(df["high"], df["low"], 20).dropna()
    assert (dc["upper"] >= dc["lower"]).all()


def test_volume_ratio_uses_past_only():
    v = pd.Series([1.0] * 25 + [10.0])
    assert volume_ratio(v, 20).iloc[-1] == pytest.approx(10.0)


@pytest.mark.parametrize("fn", [
    lambda d: sma(d["close"], 20),
    lambda d: ema(d["close"], 20),
    lambda d: rsi(d["close"], 14),
    lambda d: atr(d["high"], d["low"], d["close"], 14),
    lambda d: bollinger(d["close"], 20)["upper"],
])
def test_no_lookahead(fn):
    """미래 봉을 붙여도 과거 시점의 지표값은 바뀌지 않아야 한다."""
    df = random_walk(300, seed=3)
    short = fn(df.iloc[:200]).iloc[-1]
    full = fn(df).iloc[199]
    assert short == pytest.approx(full)
