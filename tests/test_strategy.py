import pytest

from src.strategies import make_strategy
from src.strategies.base import BUY, HOLD, SELL
from tests.conftest import make_candles


def series_candles(closes):
    rows = []
    prev = closes[0]
    for c in closes:
        rows.append((prev, max(prev, c) * 1.001, min(prev, c) * 0.999, c))
        prev = c
    return make_candles(rows)


def test_param_range_validation():
    with pytest.raises(ValueError):
        make_strategy("ma_cross", {"fast": 1})
    with pytest.raises(ValueError):
        make_strategy("ma_cross", {"fast": 30, "slow": 25})
    with pytest.raises(ValueError):
        make_strategy("ma_cross", {"unknown": 1})


def test_golden_and_dead_cross():
    s = make_strategy("ma_cross", {"fast": 5, "slow": 20, "atr_period": 14})
    closes = [100.0] * 30 + [90.0] * 10   # 하락 → 단기가 장기 아래
    df = series_candles(closes)
    actions = []
    # 상승 전환
    closes2 = closes + [130.0] * 15
    df = series_candles(closes2)
    for i in range(s.min_bars, len(df)):
        actions.append((i, s.generate(df.iloc[: i + 1])))
    buys = [(i, sig) for i, sig in actions if sig.action == BUY]
    assert len(buys) == 1
    sig = buys[0][1]
    assert sig.stop_loss is not None and sig.stop_loss < sig.price

    closes3 = closes2 + [80.0] * 15
    df = series_candles(closes3)
    sells = [i for i in range(len(closes2), len(df)) if s.generate(df.iloc[: i + 1]).action == SELL]
    assert len(sells) == 1


def test_insufficient_data_holds():
    s = make_strategy("ma_cross", {"fast": 5, "slow": 20})
    df = series_candles([100.0] * 10)
    assert s.generate(df).action == HOLD
