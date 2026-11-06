"""무한매수법 시뮬레이터 (사용자 설계, 비트코인 일봉용으로 옮김).

이 방법은 손절가 없이 물타기를 하고 자금을 최대 100%까지 쓰므로, 리스크 관문(DESIGN.md 7장:
손절가 필수, 1회 30%, 추가매수 금지)과 맞지 않는다. 그래서 트랙 엔진이 아닌 별도 시뮬레이터로 검증한다.

규칙 (하루 = 일봉 1개, KST 09:00 경계):
- 사이클 시작 시 현재 자산을 `splits`(40) 등분 → 1회분 = 사이클 자산 / 40
- 첫날: 1회분 전액을 종가에 매수
- 이후 매일 장 마감(종가) 지정가 매수(LOC):
    전반 0.5회분: 종가 ≤ 평단가 일 때 체결
    후반 0.5회분: 종가 ≤ 평단가 × (1 + loc_premium) 일 때 체결
- 매일 보유 전량에 평단가 × (1 + take_profit) 지정가 매도: 고가가 닿으면 체결 (시가가 이미 위면 시가)
  → 사이클 종료, 다음 날 새 사이클 시작 (복리)
- 쿼터 손절: 남은 현금이 1회분 미만이면 그날 종가에 보유 수량의 quarter_frac(25%) 매도, 확보한 현금으로 계속 매수
- 모든 체결에 수수료·슬리피지 반영. 평단가는 수수료 제외 체결가 기준 (부분 매도해도 평단 유지)

하루 안의 순서: 지정가 매도(장중) → 매도 안 됐으면 종가 매수 또는 쿼터 손절.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from src.backtest.metrics import compute_metrics
from src.paper.fills import Costs


@dataclass
class InfiniteParams:
    splits: int = 40
    loc_premium: float = 0.05
    take_profit: float = 0.10
    quarter_frac: float = 0.25


@dataclass
class InfiniteResult:
    equity: np.ndarray
    exposure: np.ndarray        # 자산 중 코인 비중
    cycles: pd.DataFrame        # start_idx, end_idx, result(익절/진행중), pnl_pct, max_splits_used, quarter_stops
    trades: pd.DataFrame        # 사이클 단위 손익 (metrics 계산용)
    metrics: dict[str, Any]


def run_infinite(daily: pd.DataFrame, params: InfiniteParams, costs: Costs, initial: float,
                 unit: int = 1440, start: int = 0, end: int | None = None) -> InfiniteResult:
    end = len(daily) - 1 if end is None else end
    o, h, c = (daily[k].to_numpy(float) for k in ("open", "high", "close"))
    ts = daily["ts"].to_numpy(np.int64)
    fee, slip = costs.fee_rate, costs.slippage_rate

    cash = initial
    qty = 0.0
    basis = 0.0          # 평단가 × 수량 (수수료 제외)
    cycle_start_equity = None
    unit_cash = 0.0
    cycle = None         # dict
    cycles = []
    eq_arr = np.empty(end - start + 1)
    expo = np.empty(end - start + 1)

    def buy(amount: float, price: float) -> None:
        nonlocal cash, qty, basis
        amount = min(amount, cash)
        if amount <= 0:
            return
        p = price * (1 + slip)
        q = amount / (p * (1 + fee))
        cash -= amount
        qty += q
        basis += q * p
        cycle["splits_used"] += amount / unit_cash

    def sell(q: float, price: float) -> None:
        nonlocal cash, qty, basis
        p = price * (1 - slip)
        avg = basis / qty
        cash += q * p * (1 - fee)
        basis -= q * avg
        qty -= q

    for i in range(start, end + 1):
        if cycle is None:
            # 새 사이클
            cycle_start_equity = cash
            unit_cash = cycle_start_equity / params.splits
            cycle = {"start_idx": i, "start_equity": cycle_start_equity, "splits_used": 0.0,
                     "max_splits_used": 0.0, "quarter_stops": 0}
            buy(unit_cash, c[i])
        else:
            avg = basis / qty
            target = avg * (1 + params.take_profit)
            if h[i] >= target:
                sell(qty, max(o[i], target))
                qty, basis = 0.0, 0.0
                cycle.update(end_idx=i, result="익절", pnl_pct=cash / cycle["start_equity"] - 1)
                cycles.append(cycle)
                cycle = None
            elif cash < unit_cash * 0.999:
                sell(qty * params.quarter_frac, c[i])
                cycle["quarter_stops"] += 1
            else:
                half = unit_cash / 2
                if c[i] <= avg:
                    buy(half, c[i])
                if c[i] <= avg * (1 + params.loc_premium):
                    buy(half, c[i])
        if cycle is not None:
            cycle["max_splits_used"] = max(cycle["max_splits_used"], cycle["splits_used"])
        eq = cash + qty * c[i]
        eq_arr[i - start] = eq
        expo[i - start] = qty * c[i] / eq if eq > 0 else 0.0

    if cycle is not None:
        eq = eq_arr[-1]
        cycle.update(end_idx=end, result="진행중", pnl_pct=eq / cycle["start_equity"] - 1)
        cycles.append(cycle)

    cdf = pd.DataFrame(cycles)
    done = [x for x in cycles if x["result"] == "익절"]     # 끝난 사이클만 "거래"로 센다
    trades = pd.DataFrame({
        "pnl": [x["pnl_pct"] * x["start_equity"] for x in done],
        "pnl_pct": [x["pnl_pct"] for x in done],
    })
    trades["exit_kind"] = "take_profit"
    m = compute_metrics(eq_arr, initial, trades, int(ts[start]), int(ts[end]), c[start: end + 1], unit,
                        expo > 0)
    m["avg_exposure"] = float(expo.mean())
    m["max_exposure"] = float(expo.max())
    return InfiniteResult(eq_arr, expo, cdf, trades, m)
