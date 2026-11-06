"""돈치안 채널 돌파 (추세추종).

- 매수: 종가가 직전 N봉 최고가를 처음 넘은 봉
- 청산: 종가가 직전 M봉 최저가를 처음 밑돈 봉
- 손절가: 신호 봉 종가 - ATR × 배수
"""
from __future__ import annotations

import pandas as pd

from src.indicators.core import atr
from src.strategies.base import ParamRange, Strategy


class DonchianBreakout(Strategy):
    name = "donchian"
    label = "돈치안 채널 돌파"
    param_space = {
        "entry_period": ParamRange(10, 120, int),
        "exit_period": ParamRange(5, 60, int),
        "atr_period": ParamRange(7, 30, int),
        "atr_stop_mult": ParamRange(1.0, 6.0, float),
    }
    defaults = {"entry_period": 40, "exit_period": 20, "atr_period": 14, "atr_stop_mult": 3.0}
    grid = {
        "entry_period": [20, 30, 40, 55, 70, 90],
        "exit_period": [10, 15, 20, 30],
        "atr_stop_mult": [2.0, 2.5, 3.0, 4.0],
    }
    reasons = {"buy": "종가가 직전 {entry_period}봉 최고가 돌파", "sell": "종가가 직전 {exit_period}봉 최저가 이탈"}

    def check_params(self) -> None:
        if self.params["exit_period"] > self.params["entry_period"]:
            raise ValueError("donchian: exit_period 는 entry_period 이하여야 합니다")

    @property
    def min_bars(self) -> int:
        p = self.params
        return max(p["entry_period"], p["exit_period"], p["atr_period"]) + 2

    def compute(self, candles: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        close, high, low = candles["close"], candles["high"], candles["low"]
        upper = high.rolling(p["entry_period"], min_periods=p["entry_period"]).max().shift(1)
        lower = low.rolling(p["exit_period"], min_periods=p["exit_period"]).min().shift(1)
        a = atr(high, low, close, p["atr_period"])
        brk = close > upper
        brk_dn = close < lower
        buy = brk & ~brk.shift(1, fill_value=False)
        sell = brk_dn & ~brk_dn.shift(1, fill_value=False)
        return pd.DataFrame({
            "action": self._actions(buy, sell),
            "stop_loss": close - p["atr_stop_mult"] * a,
            "strength": (close - upper) / a,
            "channel_upper": upper, "channel_lower": lower, "atr": a,
        }, index=candles.index)
