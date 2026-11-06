"""빠른 백테스트 엔진 (메모리 내 실행, DB 기록 없음).

2단계의 파라미터 탐색·워크포워드처럼 수천 번 돌려야 하는 백테스트용이다.
규칙은 `src/tracks/base.py` 의 Track.on_bar 와 완전히 같다:
  체결 규칙(6-5)은 같은 함수(fills.py), 리스크 관문(7장)도 같은 RiskGate 를 쓴다.
두 엔진의 결과가 같은지는 tests/test_engine.py 가 검증한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from src.paper.fills import Costs, buy_fill, sell_fill, stop_trigger_price
from src.risk.gate import EntryRequest, RiskGate, check_bar
from src.strategies.base import BUY, SELL


@dataclass
class Market:
    """백테스트 대상 캔들과, 매번 다시 계산할 필요 없는 값들."""
    candles: pd.DataFrame
    unit: int
    day_boundary_sec: int = 0
    ts: np.ndarray = field(init=False)
    o: np.ndarray = field(init=False)
    h: np.ndarray = field(init=False)
    l: np.ndarray = field(init=False)
    c: np.ndarray = field(init=False)
    data_ok: np.ndarray = field(init=False)
    day_key: np.ndarray = field(init=False)

    def __post_init__(self) -> None:
        df = self.candles.reset_index(drop=True)
        self.candles = df
        self.ts = df["ts"].to_numpy(np.int64)
        self.o, self.h, self.l, self.c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        ok = np.ones(len(df), bool)
        for i in range(len(df)):
            bar = {"ts": int(self.ts[i]), "open": self.o[i], "high": self.h[i], "low": self.l[i], "close": self.c[i]}
            ok[i] = check_bar(bar, int(self.ts[i - 1]) if i > 0 else None, self.unit)[0]
        self.data_ok = ok
        self.day_key = np.array([
            datetime.fromtimestamp(int(t) - self.day_boundary_sec, tz=timezone.utc).strftime("%Y-%m-%d")
            for t in self.ts
        ])

    def __len__(self) -> int:
        return len(self.ts)

    def index_at(self, ts: int) -> int:
        """ts 이상인 첫 봉의 인덱스."""
        return int(np.searchsorted(self.ts, ts, side="left"))


@dataclass
class Signals:
    action: np.ndarray       # "buy" / "sell" / "hold"
    stop_loss: np.ndarray

    @classmethod
    def from_strategy(cls, strategy, market: Market) -> "Signals":
        df = strategy.compute(market.candles)
        action = df["action"].to_numpy().copy()
        action[: strategy.min_bars - 1] = "hold"   # generate() 와 동일하게 데이터 부족 구간은 관망
        return cls(action, df["stop_loss"].to_numpy(float))


@dataclass
class BacktestResult:
    start: int
    end: int
    initial_cash: float
    equity: np.ndarray            # 각 봉 종가 기준 자산 (start..end)
    in_position: np.ndarray       # 각 봉 종가 시점 포지션 보유 여부
    trades: pd.DataFrame          # entry_idx, exit_idx, entry_price, exit_price, entry_cost, pnl, pnl_pct, exit_kind
    entry_signals: int            # 진입 신호 수 (관문 통과 여부 무관)
    rejections: dict[str, int]    # 관문 거부 이유별 횟수 (포지션 보유 중 신호 제외)


def run_backtest(
    market: Market,
    signals: Signals,
    cfg: dict[str, Any],
    start: int = 0,
    end: int | None = None,
    costs: Costs | None = None,
) -> BacktestResult:
    """[start, end] 봉에서만 매매한다. start 이전 봉은 지표 계산(워밍업)에만 쓰였다."""
    end = len(market) - 1 if end is None else end
    costs = costs or Costs(float(cfg["costs"]["fee_rate"]), float(cfg["costs"]["slippage_rate"]))
    gate = RiskGate(cfg["risk"])
    initial = float(cfg["tracks"]["initial_cash"])
    bar_sec = market.unit * 60
    o, h, l, c, ts = market.o, market.h, market.l, market.c, market.ts
    data_ok, day_key = market.data_ok, market.day_key
    action, stops = signals.action, signals.stop_loss

    n = end - start + 1
    eq_arr = np.empty(n)
    inpos = np.zeros(n, bool)
    trades: list[tuple] = []
    rejections: dict[str, int] = {}
    entry_signals = 0

    cash = initial
    pos = None            # (qty, entry_price, entry_cost, entry_idx, stop)
    pending = None        # ("buy", target, stop) | ("sell",)
    streak, cooldown = 0, None
    cur_day, day_start, last_eq = None, None, None

    def close_pos(i: int, ref: float, kind: str) -> None:
        nonlocal cash, pos, streak, cooldown
        qty, ep, cost, eidx, _ = pos
        f = sell_fill(ref, qty, costs)
        cash += f.cash_delta
        pnl = f.cash_delta - cost
        trades.append((eidx, i, ep, f.price, cost, pnl, pnl / cost, kind))
        pos = None
        streak, cd = gate.after_trade(streak, pnl, int(ts[i]) + bar_sec)
        if cd is not None:
            cooldown = cd

    for i in range(start, end + 1):
        if day_key[i] != cur_day:
            cur_day = day_key[i]
            day_start = last_eq if last_eq is not None else cash

        if pending is not None:
            if pending[0] == "buy":
                if data_ok[i]:
                    spend = min(pending[1], cash)
                    if spend > 0:
                        f = buy_fill(o[i], spend, costs)
                        cash += f.cash_delta
                        pos = (f.qty, f.price, -f.cash_delta, i, pending[2])
            elif pos is not None:
                close_pos(i, o[i], "exit_signal")
            pending = None

        if pos is not None:
            ref = stop_trigger_price(o[i], l[i], pos[4])
            if ref is not None:
                close_pos(i, ref, "stop")

        a = action[i]
        if a == BUY:
            entry_signals += 1
            if pos is None and pending is None:
                eq = cash
                stop = stops[i]
                req = EntryRequest(
                    price=c[i], stop_loss=None if np.isnan(stop) else float(stop), size_multiplier=1.0,
                    has_position=False, has_pending_order=False, equity=eq,
                    day_start_equity=day_start or eq, decision_ts=int(ts[i]) + bar_sec,
                    cooldown_until=cooldown, data_ok=bool(data_ok[i]),
                )
                res = gate.check_entry(req)
                if res.approved:
                    pending = ("buy", res.approved_pct * eq, req.stop_loss)
                else:
                    for r in res.reasons:
                        key = r.split(" (")[0].split(":")[0]
                        rejections[key] = rejections.get(key, 0) + 1
        elif a == SELL and pos is not None and pending is None:
            pending = ("sell",)

        eq = cash + (pos[0] * c[i] if pos is not None else 0.0)
        eq_arr[i - start] = eq
        inpos[i - start] = pos is not None
        last_eq = eq

    tdf = pd.DataFrame(trades, columns=["entry_idx", "exit_idx", "entry_price", "exit_price",
                                        "entry_cost", "pnl", "pnl_pct", "exit_kind"])
    return BacktestResult(start, end, initial, eq_arr, inpos, tdf, entry_signals, rejections)
