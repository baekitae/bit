"""평균회귀 전략 2종.

RSI 평균회귀
- 매수: RSI 가 과매도 기준선을 아래에서 위로 회복한 봉
- 청산: RSI 가 청산 기준선을 아래에서 위로 넘은 봉

볼린저밴드 평균회귀
- 매수: 하단 밴드 아래에 있던 종가가 하단 밴드 위로 복귀한 봉
- 청산: 종가가 (중심선 + exit_std × 표준편차) 를 아래에서 위로 넘은 봉

손절가는 둘 다 신호 봉 종가 - ATR × 배수.
"""
from __future__ import annotations

import pandas as pd

from src.indicators.core import atr, bollinger, rsi
from src.strategies.base import ParamRange, Strategy


class RSIReversion(Strategy):
    name = "rsi_reversion"
    label = "RSI 평균회귀"
    param_space = {
        "rsi_period": ParamRange(5, 30, int),
        "oversold": ParamRange(10, 45, float),
        "exit_level": ParamRange(40, 85, float),
        "atr_period": ParamRange(7, 30, int),
        "atr_stop_mult": ParamRange(1.0, 6.0, float),
    }
    defaults = {"rsi_period": 14, "oversold": 30.0, "exit_level": 55.0, "atr_period": 14, "atr_stop_mult": 3.0}
    grid = {
        "rsi_period": [7, 10, 14, 21],
        "oversold": [20.0, 25.0, 30.0, 35.0],
        "exit_level": [50.0, 55.0, 60.0, 70.0],
        "atr_stop_mult": [2.0, 3.0, 4.0],
    }
    reasons = {"buy": "RSI{rsi_period}이 {oversold:g} 위로 회복", "sell": "RSI{rsi_period}이 {exit_level:g} 돌파"}

    def check_params(self) -> None:
        if self.params["exit_level"] <= self.params["oversold"]:
            raise ValueError("rsi_reversion: exit_level 은 oversold 보다 커야 합니다")

    @property
    def min_bars(self) -> int:
        return max(self.params["rsi_period"], self.params["atr_period"]) + 2

    def compute(self, candles: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        close = candles["close"]
        r = rsi(close, p["rsi_period"])
        a = atr(candles["high"], candles["low"], close, p["atr_period"])
        prev = r.shift(1)
        buy = (prev < p["oversold"]) & (r >= p["oversold"])
        sell = (prev < p["exit_level"]) & (r >= p["exit_level"])
        return pd.DataFrame({
            "action": self._actions(buy, sell),
            "stop_loss": close - p["atr_stop_mult"] * a,
            "strength": (p["oversold"] - r.rolling(3, min_periods=1).min()) / p["oversold"],
            "rsi": r, "atr": a,
        }, index=candles.index)


class BollingerReversion(Strategy):
    name = "bollinger_reversion"
    label = "볼린저밴드 평균회귀"
    param_space = {
        "bb_period": ParamRange(10, 60, int),
        "bb_std": ParamRange(1.0, 3.5, float),
        "exit_std": ParamRange(-0.5, 2.5, float),
        "atr_period": ParamRange(7, 30, int),
        "atr_stop_mult": ParamRange(1.0, 6.0, float),
    }
    defaults = {"bb_period": 20, "bb_std": 2.0, "exit_std": 0.0, "atr_period": 14, "atr_stop_mult": 3.0}
    grid = {
        "bb_period": [14, 20, 30, 40],
        "bb_std": [1.5, 2.0, 2.5, 3.0],
        "exit_std": [0.0, 0.5, 1.0, 2.0],
        "atr_stop_mult": [2.0, 3.0, 4.0],
    }
    reasons = {"buy": "종가가 볼린저 하단({bb_period}, {bb_std:g}σ) 위로 복귀",
               "sell": "종가가 중심선 + {exit_std:g}σ 돌파"}

    def check_params(self) -> None:
        if self.params["exit_std"] >= self.params["bb_std"]:
            raise ValueError("bollinger_reversion: exit_std 는 bb_std 보다 작아야 합니다")

    @property
    def min_bars(self) -> int:
        return max(self.params["bb_period"], self.params["atr_period"]) + 2

    def compute(self, candles: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        close = candles["close"]
        bb = bollinger(close, p["bb_period"], p["bb_std"])
        sd = (bb["upper"] - bb["mid"]) / p["bb_std"]
        exit_line = bb["mid"] + p["exit_std"] * sd
        a = atr(candles["high"], candles["low"], close, p["atr_period"])
        buy = (close.shift(1) < bb["lower"].shift(1)) & (close >= bb["lower"])
        sell = (close.shift(1) < exit_line.shift(1)) & (close >= exit_line)
        return pd.DataFrame({
            "action": self._actions(buy, sell),
            "stop_loss": close - p["atr_stop_mult"] * a,
            "strength": (bb["mid"] - close) / sd,
            "bb_mid": bb["mid"], "bb_lower": bb["lower"], "exit_line": exit_line, "atr": a,
        }, index=candles.index)
