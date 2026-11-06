"""지표 계산 (DESIGN.md 2장 원칙 2: 계산은 코드가 한다).

모든 함수는 pandas Series 를 받아 같은 길이의 Series 를 돌려준다.
값을 계산할 만큼 데이터가 쌓이기 전에는 NaN.
각 값은 해당 봉과 그 이전 데이터만 사용한다 (미래 참조 없음).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(s: pd.Series, period: int) -> pd.Series:
    return s.rolling(period, min_periods=period).mean()


def ema(s: pd.Series, period: int) -> pd.Series:
    return s.ewm(span=period, adjust=False, min_periods=period).mean()


def _wilder(s: pd.Series, period: int) -> pd.Series:
    """와일더 평활 (첫 값은 단순평균으로 시작)."""
    vals = s.to_numpy(dtype=float)
    res = np.full(len(vals), np.nan)
    valid = ~np.isnan(vals)
    # 첫 유효값부터 period 개를 모아 시작
    start = None
    count = 0
    for i, ok in enumerate(valid):
        if ok:
            count += 1
            if count == period:
                start = i
                break
        else:
            count = 0
    if start is not None:
        prev = vals[start - period + 1 : start + 1].mean()
        res[start] = prev
        for i in range(start + 1, len(vals)):
            prev = (prev * (period - 1) + vals[i]) / period
            res[i] = prev
    return pd.Series(res, index=s.index)


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = _wilder(gain, period)
    avg_loss = _wilder(loss, period)
    rs = avg_gain / avg_loss
    out = 100 - 100 / (1 + rs)
    # 손실이 0이면 RSI 100
    out = out.where(avg_loss != 0, 100.0)
    return out.where(avg_gain.notna())


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    tr.iloc[0] = high.iloc[0] - low.iloc[0]
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    return _wilder(true_range(high, low, close), period)


def bollinger(close: pd.Series, period: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    mid = sma(close, period)
    std = close.rolling(period, min_periods=period).std(ddof=0)
    return pd.DataFrame({"mid": mid, "upper": mid + num_std * std, "lower": mid - num_std * std})


def donchian(high: pd.Series, low: pd.Series, period: int = 20) -> pd.DataFrame:
    """돈치안 채널. 돌파 판정에 쓸 때는 직전 봉까지의 채널(shift(1))과 비교할 것."""
    upper = high.rolling(period, min_periods=period).max()
    lower = low.rolling(period, min_periods=period).min()
    return pd.DataFrame({"upper": upper, "lower": lower, "mid": (upper + lower) / 2})


def volume_ratio(volume: pd.Series, period: int = 20) -> pd.Series:
    """현재 거래량 / 직전 period 봉 평균 거래량."""
    return volume / volume.shift(1).rolling(period, min_periods=period).mean()
