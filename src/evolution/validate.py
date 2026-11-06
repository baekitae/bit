"""수정안 검증 (DESIGN.md 9-3). AI 없이 규칙 전략만으로, 최근 N일을 제외한 과거 데이터에서 비교한다."""
from __future__ import annotations

import math
import sqlite3
from typing import Any

from src.backtest.engine import Market
from src.backtest.study import Study
from src.data.upbit import load_candles, since_years
from src.strategies import REGISTRY


def _s(x: float) -> float:
    return 0.0 if x is None or math.isnan(x) else float(x)


def validate_proposal(conn: sqlite3.Connection, cfg: dict[str, Any], strategy: str,
                      current: dict[str, Any], proposed: dict[str, Any], now_ts: int) -> dict[str, Any]:
    ev = cfg["evolution"]
    unit = 240
    end = now_ts - int(ev["exclude_recent_days"]) * 86400
    start = since_years(float(cfg["data"]["history_years"]), end)
    df = load_candles(conn, cfg["market"], unit, start, end)
    if len(df) < 500:
        return {"passed": False, "reason": f"검증 데이터 부족 ({len(df)}봉)"}
    market = Market(df, unit, int(cfg.get("day_boundary_utc_hour", 0)) * 3600)
    study = Study(market, cfg)
    cls = REGISTRY[strategy]
    cur = study.walk_forward(cls, {**cls.defaults, **current}).get("oos") or {}
    new = study.walk_forward(cls, {**cls.defaults, **proposed}).get("oos") or {}
    if not cur or not new:
        return {"passed": False, "reason": "워크포워드 구간이 없음"}

    need_sharpe = _s(cur["sharpe"]) + float(ev["min_sharpe_gain"])
    checks = {
        "sharpe": (_s(new["sharpe"]) >= need_sharpe,
                   f"표본 외 샤프 {_s(new['sharpe']):.2f} (현재 {_s(cur['sharpe']):.2f}, 필요 ≥ {need_sharpe:.2f})"),
        "mdd": (new["mdd"] >= cur["mdd"] - 1e-9,
                f"표본 외 MDD {new['mdd']:.1%} (현재 {cur['mdd']:.1%}, 나빠지면 안 됨)"),
        "trades": (new["trades"] >= float(ev["min_trades_ratio"]) * cur["trades"],
                   f"표본 외 거래 {new['trades']}회 (현재 {cur['trades']}회의 {ev['min_trades_ratio']:.0%} 이상)"),
    }
    passed = all(ok for ok, _ in checks.values())
    keep = ("total_return", "cagr", "mdd", "sharpe", "trades", "win_rate")
    return {
        "passed": passed,
        "reason": "; ".join(("통과 " if ok else "미달 ") + msg for ok, msg in checks.values()),
        "data_range": [int(df["ts"].iloc[0]), int(df["ts"].iloc[-1])],
        "folds": len(study.folds()),
        "current": {k: cur[k] for k in keep},
        "proposed": {k: new[k] for k in keep},
    }
