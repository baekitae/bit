"""위원회 입력 요약 (DESIGN.md 8-3). 모든 숫자는 여기서 코드가 계산한다."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

from src.backtest.regime import classify
from src.indicators.core import atr, rsi, sma, volume_ratio

KST = timezone(timedelta(hours=9))


def _r(x: float, nd: int = 4) -> float | None:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(float(x), nd)


def _bars(hours: float, unit: int) -> int:
    return max(int(round(hours * 60 / unit)), 1)


def build_context(track, signal, bar_ts: int, cfg: dict[str, Any]) -> dict[str, Any]:
    w: pd.DataFrame = track.window.reset_index(drop=True)
    unit = track.unit
    close = w["close"]
    c = float(close.iloc[-1])

    def ret(hours: float) -> float | None:
        n = _bars(hours, unit)
        return _r(c / float(close.iloc[-1 - n]) - 1) if len(close) > n else None

    def ma_gap(hours: float) -> float | None:
        n = _bars(hours, unit)
        m = sma(close, n).iloc[-1]
        return _r(c / m - 1) if not pd.isna(m) else None

    rets = close.pct_change()
    vol_n = _bars(30 * 24, unit)
    bars_per_year = 365.25 * 24 * 60 / unit
    realized = rets.iloc[-vol_n:].std() * math.sqrt(bars_per_year) if len(rets) > vol_n else float("nan")
    a = atr(w["high"], w["low"], close, 14).iloc[-1]
    hi = _bars(30 * 24, unit)
    recent = w.iloc[-13:]                    # 13봉 → 변화율 12개
    regime = classify(w, cfg["regime"], unit)[-1]

    acc = track.account
    pos = acc.position()
    equity = acc.equity_at(c)
    trades = acc.conn.execute(
        "SELECT pnl_pct, exit_kind FROM trades WHERE run_id=? AND track=? ORDER BY id DESC LIMIT 10",
        acc.key,
    ).fetchall()
    pnls = [t["pnl_pct"] for t in trades]

    return {
        "시장": cfg["market"],
        "봉주기": "일봉" if unit == 1440 else f"{unit // 60}시간봉",
        "신호봉_마감시각_KST": datetime.fromtimestamp(bar_ts + unit * 60, tz=KST).strftime("%Y-%m-%d %H:%M"),
        "규칙전략_신호": {
            "전략": f"{getattr(track.strategy, 'label', '')} ({track.strategy.name})",
            "파라미터": track.strategy.params,
            "행동": "매수(진입)",
            "근거": signal.reason,
            "신호봉_종가": _r(c, 0),
            "규칙_손절가": _r(signal.stop_loss, 0),
            "손절까지_거리": _r(signal.stop_loss / c - 1) if signal.stop_loss else None,
            "트레일링_스탑": track.strategy.trail_pct,
            "신호_강도": _r(signal.strength),
            "전략_지표": {k: _r(v) for k, v in signal.indicators.items()},
        },
        "가격_흐름": {
            "수익률_1일": ret(24), "수익률_7일": ret(24 * 7), "수익률_30일": ret(24 * 30), "수익률_90일": ret(24 * 90),
            "30일_최고가_대비": _r(c / float(w["high"].iloc[-hi:].max()) - 1),
            "30일_최저가_대비": _r(c / float(w["low"].iloc[-hi:].min()) - 1),
            "최근_12봉_종가_변화율": [_r(x) for x in recent["close"].pct_change().iloc[1:]],
        },
        "지표": {
            "이동평균_대비_이격_5일": ma_gap(24 * 5), "이동평균_대비_이격_20일": ma_gap(24 * 20),
            "이동평균_대비_이격_60일": ma_gap(24 * 60),
            "RSI14": _r(rsi(close, 14).iloc[-1], 1),
            "ATR14_가격대비": _r(a / c),
            "30일_실현변동성_연환산": _r(realized),
            "거래량_20봉평균_대비": _r(volume_ratio(w["volume"], 20).iloc[-1], 2),
            "국면_코드분류": regime,
        },
        "계좌": {
            "트랙": track.name,
            "자산": _r(equity, 0),
            "현금": _r(acc.state.cash, 0),
            "보유_포지션": None if pos is None else {"수량": pos.qty, "진입가": pos.entry_price},
            "당일_손익률": _r(equity / acc.state.day_start_equity - 1) if acc.state.day_start_equity else None,
            "연속_손실": acc.state.loss_streak,
            "규칙상_최대_진입비중": cfg["risk"]["max_position_pct"],
        },
        "최근_거래_성과": {
            "최근_거래_수": len(pnls),
            "거래별_수익률_최신순": [_r(p) for p in pnls],
            "승률": _r(float(np.mean([p > 0 for p in pnls]))) if pnls else None,
        },
    }
