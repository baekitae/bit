"""이동평균 교차 (추세추종). 1단계 임시 전략.

- 매수: 단기 SMA 가 장기 SMA 를 아래에서 위로 교차 (골든크로스)
- 청산: 단기 SMA 가 장기 SMA 를 위에서 아래로 교차 (데드크로스)
- 손절가: 신호 봉 종가 - ATR × 배수
"""
from __future__ import annotations

import pandas as pd

from src.indicators.core import atr, sma
from src.strategies.base import BUY, HOLD, SELL, ParamRange, Signal, Strategy


class MACross(Strategy):
    name = "ma_cross"
    param_space = {
        "fast": ParamRange(5, 50, int),
        "slow": ParamRange(20, 200, int),
        "atr_period": ParamRange(7, 30, int),
        "atr_stop_mult": ParamRange(1.0, 5.0, float),
    }
    defaults = {"fast": 20, "slow": 60, "atr_period": 14, "atr_stop_mult": 2.0}

    def check_params(self) -> None:
        if self.params["fast"] >= self.params["slow"]:
            raise ValueError("ma_cross: fast 는 slow 보다 작아야 합니다")

    @property
    def min_bars(self) -> int:
        return max(self.params["slow"], self.params["atr_period"]) + 1

    def generate(self, candles: pd.DataFrame) -> Signal:
        close = candles["close"]
        price = float(close.iloc[-1])
        if len(candles) < self.min_bars:
            return Signal(HOLD, price, reason="데이터 부족")

        p = self.params
        fast = sma(close, p["fast"])
        slow = sma(close, p["slow"])
        a = atr(candles["high"], candles["low"], close, p["atr_period"])
        f0, f1 = fast.iloc[-2], fast.iloc[-1]
        s0, s1 = slow.iloc[-2], slow.iloc[-1]
        atr_now = float(a.iloc[-1])
        ind = {"sma_fast": float(f1), "sma_slow": float(s1), "atr": atr_now}
        strength = float((f1 - s1) / s1)

        if f0 <= s0 and f1 > s1:
            stop = price - p["atr_stop_mult"] * atr_now
            return Signal(
                BUY, price, strength, stop,
                f"SMA{p['fast']}이 SMA{p['slow']}을 상향 돌파", ind,
            )
        if f0 >= s0 and f1 < s1:
            return Signal(
                SELL, price, strength, None,
                f"SMA{p['fast']}이 SMA{p['slow']}을 하향 돌파", ind,
            )
        return Signal(HOLD, price, strength, None, "", ind)
