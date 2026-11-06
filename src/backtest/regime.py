"""시장 국면 분류 (DESIGN.md 6-3). 기준은 백테스트 전에 고정하고 결과를 본 뒤 바꾸지 않는다.

- 기준선: 종가의 이동평균 (기간은 시간 단위로 지정 → 봉 주기가 바뀌어도 같은 의미)
- 기울기: 이동평균의 최근 구간 변화율
- 상승장: 종가 > 기준선 이고 기울기 > +임계값
- 하락장: 종가 < 기준선 이고 기울기 < -임계값
- 그 외: 횡보장
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from src.indicators.core import sma

UP, DOWN, SIDE, UNKNOWN = "상승장", "하락장", "횡보장", "판정불가"
REGIMES = (UP, DOWN, SIDE)


def classify(candles: pd.DataFrame, cfg: dict[str, Any], unit: int) -> np.ndarray:
    ma_bars = max(int(round(cfg["ma_hours"] * 60 / unit)), 2)
    slope_bars = max(int(round(cfg["slope_hours"] * 60 / unit)), 1)
    th = float(cfg["slope_threshold"])
    close = candles["close"].reset_index(drop=True)
    ma = sma(close, ma_bars)
    slope = ma / ma.shift(slope_bars) - 1
    out = np.full(len(close), SIDE, dtype=object)
    out[((close > ma) & (slope > th)).to_numpy()] = UP
    out[((close < ma) & (slope < -th)).to_numpy()] = DOWN
    out[(ma.isna() | slope.isna()).to_numpy()] = UNKNOWN
    return out


def regime_breakdown(market, result, regimes: np.ndarray) -> list[dict[str, Any]]:
    """국면별 성과. 봉 t 의 수익(t-1 종가 → t 종가)은 t-1 시점의 국면에 귀속한다."""
    s, e = result.start, result.end
    eq = np.concatenate([[result.initial_cash], result.equity])
    strat_r = eq[1:] / eq[:-1] - 1
    closes = market.c
    prev_close = closes[s - 1: e] if s > 0 else np.concatenate([[closes[0]], closes[s: e]])
    bh_r = closes[s: e + 1] / prev_close - 1
    reg = regimes[max(s - 1, 0): e] if s > 0 else np.concatenate([[regimes[0]], regimes[s: e]])

    trades = result.trades
    entry_reg = regimes[trades["entry_idx"].to_numpy(int)] if len(trades) else np.array([])

    rows = []
    for name in REGIMES:
        m = reg == name
        tm = entry_reg == name
        tr = trades[tm] if len(trades) else trades
        rows.append({
            "regime": name,
            "share": float(m.mean()) if len(m) else 0.0,
            "strategy_return": float(np.prod(1 + strat_r[m]) - 1) if m.any() else 0.0,
            "bh_return": float(np.prod(1 + bh_r[m]) - 1) if m.any() else 0.0,
            "trades": int(tm.sum()),
            "win_rate": float((tr["pnl"] > 0).mean()) if len(tr) else float("nan"),
            "avg_trade": float(tr["pnl_pct"].mean()) if len(tr) else float("nan"),
        })
    return rows
