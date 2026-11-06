"""성과 지표 (DESIGN.md 6-3)."""
from __future__ import annotations

import math
import sqlite3
from typing import Any

import numpy as np
import pandas as pd

LABELS = {
    "start_ts": "기간 시작",
    "end_ts": "기간 끝",
    "initial": "시작 자금",
    "final": "최종 자산",
    "total_return": "누적 수익률",
    "cagr": "연환산 수익률",
    "mdd": "최대 낙폭",
    "sharpe": "샤프지수",
    "calmar": "수익/낙폭 비",
    "trades": "거래 횟수",
    "win_rate": "승률",
    "payoff": "손익비",
    "profit_factor": "총이익/총손실",
    "avg_trade": "거래당 평균 수익률",
    "stop_exits": "손절 청산",
    "exposure": "보유 시간 비율",
    "bh_return": "단순 보유 수익률",
    "bh_mdd": "단순 보유 최대 낙폭",
}
PCT_KEYS = {"total_return", "cagr", "mdd", "win_rate", "avg_trade", "exposure", "bh_return", "bh_mdd"}


def max_drawdown(equity: np.ndarray | pd.Series) -> float:
    eq = np.asarray(equity, float)
    if len(eq) == 0:
        return 0.0
    peak = np.maximum.accumulate(eq)
    return float(((eq - peak) / peak).min())


def compute_metrics(
    equity: np.ndarray,
    initial: float,
    trades: pd.DataFrame,
    start_ts: int,
    end_ts: int,
    closes: np.ndarray,
    unit: int,
    in_position: np.ndarray | None = None,
) -> dict[str, Any]:
    """equity: 각 봉 종가 기준 자산, closes: 같은 봉들의 종가."""
    eq = np.concatenate([[initial], np.asarray(equity, float)])
    final = float(eq[-1])
    years = (end_ts - start_ts) / (365.25 * 86400)
    total = final / initial - 1
    cagr = (final / initial) ** (1 / years) - 1 if years > 0 and final > 0 else float("nan")
    rets = eq[1:] / eq[:-1] - 1
    bars_per_year = 365.25 * 24 * 60 / unit
    sd = rets.std(ddof=1) if len(rets) > 1 else 0.0
    sharpe = float(rets.mean() / sd * math.sqrt(bars_per_year)) if sd > 0 else float("nan")
    mdd = max_drawdown(eq)

    n = len(trades)
    pnl = trades["pnl"].to_numpy(float) if n else np.array([])
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gross_win, gross_loss = wins.sum(), -losses.sum()
    closes = np.asarray(closes, float)
    return {
        "start_ts": int(start_ts),
        "end_ts": int(end_ts),
        "initial": initial,
        "final": final,
        "total_return": total,
        "cagr": cagr,
        "mdd": mdd,
        "sharpe": sharpe,
        "calmar": cagr / abs(mdd) if mdd < 0 and not math.isnan(cagr) else float("nan"),
        "trades": n,
        "win_rate": len(wins) / n if n else float("nan"),
        "payoff": (wins.mean() / -losses.mean()) if len(wins) and len(losses) and losses.sum() < 0 else float("nan"),
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else float("nan"),
        "avg_trade": float(trades["pnl_pct"].mean()) if n else float("nan"),
        "stop_exits": int((trades["exit_kind"] == "stop").sum()) if n else 0,
        "exposure": float(np.mean(in_position)) if in_position is not None and len(in_position) else float("nan"),
        "bh_return": float(closes[-1] / closes[0] - 1) if len(closes) else float("nan"),
        "bh_mdd": max_drawdown(closes) if len(closes) else float("nan"),
    }


def result_metrics(market, result) -> dict[str, Any]:
    """engine.BacktestResult → 지표"""
    s, e = result.start, result.end
    return compute_metrics(
        result.equity, result.initial_cash, result.trades,
        int(market.ts[s]), int(market.ts[e]), market.c[s: e + 1], market.unit, result.in_position,
    )


def summarize(conn: sqlite3.Connection, run_id: str, track: str, unit: int) -> dict[str, Any]:
    """DB에 기록된 트랙 실행 결과 → 지표"""
    eq = pd.read_sql_query(
        """SELECT e.bar_ts, e.equity, e.position_value, c.close FROM equity e
           LEFT JOIN candles c ON c.ts = e.bar_ts AND c.unit = ?
           WHERE e.run_id=? AND e.track=? ORDER BY e.bar_ts""",
        conn, params=(unit, run_id, track),
    )
    if eq.empty:
        return {}
    trades = pd.read_sql_query(
        "SELECT pnl, pnl_pct, exit_kind FROM trades WHERE run_id=? AND track=? ORDER BY exit_ts",
        conn, params=(run_id, track),
    )
    initial = conn.execute(
        "SELECT initial_cash FROM tracks WHERE run_id=? AND track=?", (run_id, track)
    ).fetchone()[0]
    return compute_metrics(
        eq["equity"].to_numpy(), initial, trades, int(eq["bar_ts"].iloc[0]), int(eq["bar_ts"].iloc[-1]),
        eq["close"].to_numpy(), unit, (eq["position_value"] > 0).to_numpy(),
    )
