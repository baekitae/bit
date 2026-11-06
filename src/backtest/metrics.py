"""성과 지표 (DESIGN.md 6-3). 1단계는 기본 지표만, 2단계에서 국면별·워크포워드 확장."""
from __future__ import annotations

import math
import sqlite3
from typing import Any

import pandas as pd


def max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    return float(((equity - peak) / peak).min()) if len(equity) else 0.0


def summarize(conn: sqlite3.Connection, run_id: str, track: str, unit: int) -> dict[str, Any]:
    eq = pd.read_sql_query(
        "SELECT bar_ts, equity FROM equity WHERE run_id=? AND track=? ORDER BY bar_ts",
        conn, params=(run_id, track),
    )
    trades = pd.read_sql_query(
        "SELECT pnl, pnl_pct, exit_kind FROM trades WHERE run_id=? AND track=? ORDER BY exit_ts",
        conn, params=(run_id, track),
    )
    if eq.empty:
        return {}
    initial = conn.execute(
        "SELECT initial_cash FROM tracks WHERE run_id=? AND track=?", (run_id, track)
    ).fetchone()[0]

    first_ts, last_ts = int(eq["bar_ts"].iloc[0]), int(eq["bar_ts"].iloc[-1])
    closes = conn.execute(
        """SELECT (SELECT close FROM candles WHERE unit=? AND ts=?),
                  (SELECT close FROM candles WHERE unit=? AND ts=?)""",
        (unit, first_ts, unit, last_ts),
    ).fetchone()

    final = float(eq["equity"].iloc[-1])
    years = (last_ts - first_ts) / (365.25 * 86400)
    total_ret = final / initial - 1
    cagr = (final / initial) ** (1 / years) - 1 if years > 0 and final > 0 else float("nan")

    rets = pd.concat([pd.Series([initial]), eq["equity"]]).pct_change().dropna()
    bars_per_year = 365.25 * 24 * 60 / unit
    sharpe = (
        float(rets.mean() / rets.std() * math.sqrt(bars_per_year))
        if len(rets) > 1 and rets.std() > 0 else float("nan")
    )

    wins = trades[trades["pnl"] > 0]
    losses = trades[trades["pnl"] <= 0]
    avg_win = float(wins["pnl"].mean()) if len(wins) else 0.0
    avg_loss = float(-losses["pnl"].mean()) if len(losses) else 0.0

    bh = (closes[1] / closes[0] - 1) if closes[0] and closes[1] else float("nan")
    return {
        "기간_시작": first_ts,
        "기간_끝": last_ts,
        "시작자금": initial,
        "최종자산": final,
        "누적수익률": total_ret,
        "연환산수익률": cagr,
        "최대낙폭": max_drawdown(pd.concat([pd.Series([initial]), eq["equity"]])),
        "샤프지수": sharpe,
        "거래횟수": int(len(trades)),
        "승률": float(len(wins) / len(trades)) if len(trades) else float("nan"),
        "손익비": avg_win / avg_loss if avg_loss > 0 else float("nan"),
        "손절청산": int((trades["exit_kind"] == "stop").sum()) if len(trades) else 0,
        "단순보유수익률": bh,
    }
