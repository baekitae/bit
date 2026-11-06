"""이동평균 교차 (추세추종).

- 매수: 단기 SMA 가 장기 SMA 를 아래에서 위로 교차 (골든크로스)
- 청산: 단기 SMA 가 장기 SMA 를 위에서 아래로 교차 (데드크로스)
- 손절가: 신호 봉 종가 - ATR × 배수
"""
from __future__ import annotations

import pandas as pd

from src.indicators.core import atr, sma
from src.strategies.base import ParamRange, Strategy


class MACross(Strategy):
    name = "ma_cross"
    label = "이동평균 교차"
    param_space = {
        "fast": ParamRange(5, 50, int),
        "slow": ParamRange(20, 200, int),
        "atr_period": ParamRange(7, 30, int),
        "atr_stop_mult": ParamRange(1.0, 5.0, float),
    }
    defaults = {"fast": 20, "slow": 60, "atr_period": 14, "atr_stop_mult": 2.0}
    grid = {
        "fast": [10, 15, 20, 25, 30],
        "slow": [40, 50, 60, 80, 100, 120],
        "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
    }
    reasons = {"buy": "SMA{fast}이 SMA{slow}을 상향 돌파", "sell": "SMA{fast}이 SMA{slow}을 하향 돌파"}

    def check_params(self) -> None:
        if self.params["fast"] >= self.params["slow"]:
            raise ValueError("ma_cross: fast 는 slow 보다 작아야 합니다")

    @property
    def min_bars(self) -> int:
        return max(self.params["slow"], self.params["atr_period"]) + 1

    def compute(self, candles: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        close = candles["close"]
        fast = sma(close, p["fast"])
        slow = sma(close, p["slow"])
        a = atr(candles["high"], candles["low"], close, p["atr_period"])
        buy = (fast.shift(1) <= slow.shift(1)) & (fast > slow)
        sell = (fast.shift(1) >= slow.shift(1)) & (fast < slow)
        return pd.DataFrame({
            "action": self._actions(buy, sell),
            "stop_loss": close - p["atr_stop_mult"] * a,
            "strength": (fast - slow) / slow,
            "sma_fast": fast, "sma_slow": slow, "atr": a,
        }, index=candles.index)
