"""타이탄 휴식 기법 (사용자 설계, 비트코인용으로 옮김).

원래 규칙 (일봉 기준):
- 선별: 이동평균 정배열 5 > 20 > 60, 우상향 / 상대강도 RS > 0
- 진입: 종가의 20일선 대비 이격도가 -2% ~ +3% 안으로 들어온 봉 (휴식 구간), 1회 예산 전액
- 청산: 초기 손절 = 진입가 - 3 × ATR, 트레일링 = 진입 후 최고가 대비 -12%

비트코인용 변경:
- RS 는 비교할 지수가 없으므로 "최근 rs_period 봉 수익률 > 0"(절대 모멘텀)으로 대신한다.
- "우상향"은 20일선·60일선이 rise_bars 봉 전보다 높은 것으로 정의한다.
- 1회 예산은 리스크 관문의 1회 최대 비중(30%)을 따른다.
- 청산 신호(sell)는 없고, 손절·트레일링 스탑으로만 청산한다.
"""
from __future__ import annotations

import pandas as pd

from src.indicators.core import atr, sma
from src.strategies.base import ParamRange, Strategy


class TitanRest(Strategy):
    name = "titan_rest"
    label = "타이탄 휴식 기법"
    param_space = {
        "ma_short": ParamRange(3, 10, int),
        "ma_mid": ParamRange(10, 40, int),
        "ma_long": ParamRange(30, 120, int),
        "rise_bars": ParamRange(1, 20, int),
        "rs_period": ParamRange(10, 250, int),
        "band_low": ParamRange(-0.10, 0.0, float),
        "band_high": ParamRange(0.0, 0.10, float),
        "atr_period": ParamRange(7, 30, int),
        "atr_stop_mult": ParamRange(1.0, 6.0, float),
        "trail_pct": ParamRange(0.03, 0.40, float),
    }
    defaults = {"ma_short": 5, "ma_mid": 20, "ma_long": 60, "rise_bars": 5, "rs_period": 60,
                "band_low": -0.02, "band_high": 0.03, "atr_period": 14, "atr_stop_mult": 3.0,
                "trail_pct": 0.12}
    grid = {
        "rs_period": [20, 60, 120],
        "band_high": [0.02, 0.03, 0.05],
        "atr_stop_mult": [2.0, 3.0, 4.0],
        "trail_pct": [0.08, 0.12, 0.16, 0.20],
    }
    reasons = {"buy": "정배열({ma_short}>{ma_mid}>{ma_long}) 중 {ma_mid}선 부근 휴식 구간 진입"}

    def check_params(self) -> None:
        p = self.params
        if not (p["ma_short"] < p["ma_mid"] < p["ma_long"]):
            raise ValueError("titan_rest: ma_short < ma_mid < ma_long 이어야 합니다")
        if p["band_low"] >= p["band_high"]:
            raise ValueError("titan_rest: band_low < band_high 이어야 합니다")

    @property
    def min_bars(self) -> int:
        p = self.params
        return max(p["ma_long"] + p["rise_bars"], p["rs_period"] + 1, p["atr_period"]) + 1

    def compute(self, candles: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        close = candles["close"]
        s, m, l = sma(close, p["ma_short"]), sma(close, p["ma_mid"]), sma(close, p["ma_long"])
        a = atr(candles["high"], candles["low"], close, p["atr_period"])
        aligned = (s > m) & (m > l) & (m > m.shift(p["rise_bars"])) & (l > l.shift(p["rise_bars"]))
        rs = close / close.shift(p["rs_period"]) - 1
        dev = close / m - 1
        cond = aligned & (rs > 0) & (dev >= p["band_low"]) & (dev <= p["band_high"])
        buy = cond & ~cond.shift(1, fill_value=False)
        sell = pd.Series(False, index=candles.index)
        return pd.DataFrame({
            "action": self._actions(buy, sell),
            "stop_loss": close - p["atr_stop_mult"] * a,
            "strength": rs,
            "sma_short": s, "sma_mid": m, "sma_long": l, "deviation": dev, "rs": rs, "atr": a,
        }, index=candles.index)
