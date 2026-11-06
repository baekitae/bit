import copy
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import connect  # noqa: E402
from src.strategies.base import BUY, HOLD, SELL, Signal, Strategy  # noqa: E402

UNIT = 240
BAR = UNIT * 60
T0 = 1_700_006_400  # 2023-11-15 00:00 UTC (= KST 09:00, 4시간봉 경계)

BASE_CFG = {
    "market": "KRW-BTC",
    "timeframe_minutes": UNIT,
    "day_boundary_utc_hour": 0,
    "costs": {"fee_rate": 0.0005, "slippage_rate": 0.0005},
    "tracks": {"initial_cash": 1_000_000, "enabled": ["A"]},
    "risk": {"max_position_pct": 0.30, "daily_loss_pct": 0.03,
             "max_consecutive_losses": 5, "cooldown_hours": 24},
    "strategy": {"name": "ma_cross", "version": "v1", "window_bars": 500,
                 "params": {"fast": 5, "slow": 20, "atr_period": 14, "atr_stop_mult": 2.0}},
}


@pytest.fixture
def cfg():
    return copy.deepcopy(BASE_CFG)


@pytest.fixture
def conn():
    c = connect(":memory:")
    yield c
    c.close()


def make_candles(rows):
    """rows: [(open, high, low, close), ...] → T0 부터 연속된 4시간봉 DataFrame"""
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], dtype=float)
    df.insert(0, "ts", [T0 + i * BAR for i in range(len(rows))])
    df["kst"] = ""
    df["volume"] = 1.0
    df["value"] = df["close"]
    return df


def random_walk(n, seed=0, start=50_000_000.0):
    rng = np.random.default_rng(seed)
    closes = start * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    opens = np.concatenate([[start], closes[:-1]])
    highs = np.maximum(opens, closes) * (1 + rng.uniform(0, 0.01, n))
    lows = np.minimum(opens, closes) * (1 - rng.uniform(0, 0.01, n))
    return make_candles(list(zip(opens, highs, lows, closes)))


def store_candles(conn, df, market="KRW-BTC", unit=UNIT):
    conn.executemany(
        "INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(market, unit, int(r.ts), r.kst, r.open, r.high, r.low, r.close, r.volume, r.value)
         for r in df.itertuples()],
    )
    conn.commit()


class Scripted(Strategy):
    """테스트용: 봉 번호 → 신호를 미리 정해 둔 전략."""
    name = "scripted"
    param_space = {}
    defaults = {}

    def __init__(self, script, stop_pct=0.05):
        super().__init__({})
        self.script = script
        self.stop_pct = stop_pct

    @property
    def min_bars(self):
        return 1

    def compute(self, candles):
        idx = ((candles["ts"] - T0) // BAR).astype(int)
        action = [self.script.get(int(i), HOLD) for i in idx]
        return pd.DataFrame({"action": action, "stop_loss": candles["close"] * (1 - self.stop_pct),
                             "strength": 0.0}, index=candles.index)

    def generate(self, candles):
        i = int((candles["ts"].iloc[-1] - T0) // BAR)
        price = float(candles["close"].iloc[-1])
        act = self.script.get(i, HOLD)
        stop = price * (1 - self.stop_pct) if act == BUY else None
        return Signal(act, price, 0.0, stop, f"script {i}")


def run_bars(track, df, upto=None, window=500):
    n = len(df) if upto is None else upto
    for i in range(n):
        w = df.iloc[max(0, i - window + 1): i + 1]
        track.on_bar(w, int(df["ts"].iloc[i - 1]) if i > 0 else None)
