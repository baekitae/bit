"""2단계 전략 검증 (DESIGN.md 6-3, 6-4).

전략 하나에 대해:
1. 학습 구간에서 파라미터 격자 탐색 → "이웃까지 고르게 좋은" 파라미터 선택 (과최적화 완화)
2. 선택한 파라미터로 검증 구간 성과 측정 (학습에 쓰지 않은 데이터)
3. 파라미터 민감도: 선택값의 이웃 파라미터 성과
4. 워크포워드: 학습 18개월 → 다음 6개월 검증을 반복, 표본 외 구간만 이어 붙여 성과 측정
5. 국면별 성과, 수수료 영향(0배·2배), 6개월당 신호 수
6. 탈락 기준 판정
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd

from src.backtest.engine import BacktestResult, Market, Signals, run_backtest
from src.backtest.metrics import compute_metrics, result_metrics
from src.backtest.regime import classify, regime_breakdown
from src.paper.fills import Costs
from src.strategies.base import Strategy

MONTH_SEC = int(30.4375 * 86400)


def _key(params: dict[str, Any]) -> tuple:
    return tuple(sorted(params.items()))


def param_grid(cls: type[Strategy]) -> list[dict[str, Any]]:
    keys = list(cls.grid)
    out = []
    for combo in itertools.product(*(cls.grid[k] for k in keys)):
        params = {**cls.defaults, **dict(zip(keys, combo))}
        try:
            cls(params)
        except ValueError:
            continue
        out.append(params)
    return out


def neighbors(cls: type[Strategy], params: dict[str, Any]) -> list[dict[str, Any]]:
    """격자에서 파라미터 하나만 한 칸 움직인 조합들."""
    out = []
    for k, values in cls.grid.items():
        if params[k] not in values:
            continue
        i = values.index(params[k])
        for j in (i - 1, i + 1):
            if 0 <= j < len(values):
                p = {**params, k: values[j]}
                try:
                    cls(p)
                except ValueError:
                    continue
                out.append(p)
    return out


def _sharpe0(m: dict[str, Any]) -> float:
    s = m.get("sharpe", float("nan"))
    return 0.0 if s is None or math.isnan(s) else float(s)


@dataclass
class Flag:
    level: str      # "탈락" 또는 "주의"
    rule: str
    detail: str


@dataclass
class StrategyReport:
    name: str
    label: str
    params: dict[str, Any]
    grid_size: int
    train: dict[str, Any]
    val: dict[str, Any]
    full: dict[str, Any]
    full_gross: dict[str, Any]
    full_stress: dict[str, Any]
    neighbor_stats: dict[str, Any]
    walk_forward: dict[str, Any]
    regimes: list[dict[str, Any]]
    entry_signals_per_6m: float
    rejections: dict[str, int]
    flags: list[Flag] = field(default_factory=list)
    curves: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not any(f.level == "탈락" for f in self.flags)


class Study:
    def __init__(self, market: Market, cfg: dict[str, Any]):
        self.market = market
        self.cfg = cfg
        self.bt = cfg["backtest"]
        self.regimes = classify(market.candles, cfg["regime"], market.unit)
        self._signals: dict[tuple, Signals] = {}
        n = len(market)
        self.split = int(n * float(self.bt["train_ratio"]))

    # ---- 기본 실행 ----
    def signals(self, cls: type[Strategy], params: dict[str, Any]) -> Signals:
        k = (cls.name, _key(params))
        if k not in self._signals:
            self._signals[k] = Signals.from_strategy(cls(params), self.market)
        return self._signals[k]

    def run(self, cls, params, start: int, end: int, costs: Costs | None = None) -> tuple[BacktestResult, dict]:
        r = run_backtest(self.market, self.signals(cls, params), self.cfg, start, end, costs)
        return r, result_metrics(self.market, r)

    def search(self, cls, start: int, end: int) -> tuple[dict[str, Any], dict[tuple, dict]]:
        """격자 탐색. 선택 기준 = 자기 자신과 이웃들의 샤프 평균 (최소 거래 수 충족한 것만)."""
        grid = param_grid(cls)
        table = {_key(p): self.run(cls, p, start, end)[1] for p in grid}
        min_trades = int(self.bt["min_trades_train"])
        best, best_score = None, -float("inf")
        for p in grid:
            m = table[_key(p)]
            if m["trades"] < min_trades:
                continue
            group = [m] + [table[_key(q)] for q in neighbors(cls, p)]
            score = float(np.mean([_sharpe0(x) for x in group]))
            if score > best_score or (score == best_score and _sharpe0(m) > _sharpe0(table[_key(best)])):
                best, best_score = p, score
        if best is None:  # 모두 거래 부족 → 샤프 최고값
            best = max(grid, key=lambda p: _sharpe0(table[_key(p)]))
        return best, table

    # ---- 워크포워드 ----
    def folds(self) -> list[tuple[int, int, int, int]]:
        wf = self.bt["walk_forward"]
        tr, te = int(wf["train_months"]) * MONTH_SEC, int(wf["test_months"]) * MONTH_SEC
        ts = self.market.ts
        out = []
        t0 = int(ts[0])
        while True:
            a, b, c = t0, t0 + tr, t0 + tr + te
            if c > int(ts[-1]) + self.market.unit * 60:
                break
            ia, ib, ic = self.market.index_at(a), self.market.index_at(b), self.market.index_at(c)
            out.append((ia, ib - 1, ib, ic - 1))
            t0 += te
        return out

    def walk_forward(self, cls) -> dict[str, Any]:
        initial = float(self.cfg["tracks"]["initial_cash"])
        rets, trades, closes, inpos, fold_rows = [], [], [], [], []
        first_ts = last_ts = None
        for (a, b, c, d) in self.folds():
            p, _ = self.search(cls, a, b)
            r, m = self.run(cls, p, c, d)
            eq = np.concatenate([[initial], r.equity])
            rets.append(eq[1:] / eq[:-1] - 1)
            trades.append(r.trades)
            closes.append(self.market.c[c: d + 1])
            inpos.append(r.in_position)
            first_ts = first_ts if first_ts is not None else int(self.market.ts[c])
            last_ts = int(self.market.ts[d])
            fold_rows.append({"test_start": int(self.market.ts[c]), "test_end": last_ts, "params": p,
                              "total_return": m["total_return"], "bh_return": m["bh_return"],
                              "trades": m["trades"], "mdd": m["mdd"]})
        if not fold_rows:
            return {"folds": [], "oos": {}}
        chained = initial * np.cumprod(1 + np.concatenate(rets))
        oos = compute_metrics(chained, initial, pd.concat(trades, ignore_index=True), first_ts, last_ts,
                              np.concatenate(closes), self.market.unit, np.concatenate(inpos))
        return {"folds": fold_rows, "oos": oos, "equity": chained}

    # ---- 전체 평가 ----
    def evaluate(self, cls: type[Strategy], progress: Callable[[str], None] | None = None) -> StrategyReport:
        say = progress or (lambda s: None)
        n = len(self.market)
        a, b, c, d = 0, self.split - 1, self.split, n - 1
        say(f"  [{cls.label}] 학습 구간 격자 탐색")
        best, table = self.search(cls, a, b)
        train_m = table[_key(best)]
        val_r, val_m = self.run(cls, best, c, d)

        nb = [table[_key(q)] for q in neighbors(cls, best)]
        nb_sharpes = [_sharpe0(x) for x in nb]
        neighbor_stats = {
            "count": len(nb),
            "profitable_share": float(np.mean([x["total_return"] > 0 for x in nb])) if nb else float("nan"),
            "median_sharpe": float(np.median(nb_sharpes)) if nb else float("nan"),
            "min_sharpe": float(np.min(nb_sharpes)) if nb else float("nan"),
            "grid_profitable_share": float(np.mean([x["total_return"] > 0 for x in table.values()])),
        }

        say(f"  [{cls.label}] 전체 기간·수수료 영향")
        full_r, full_m = self.run(cls, best, a, d)
        fee, slip = float(self.cfg["costs"]["fee_rate"]), float(self.cfg["costs"]["slippage_rate"])
        mult = float(self.bt["reject"]["cost_stress_mult"])
        _, gross_m = self.run(cls, best, a, d, Costs(0.0, 0.0))
        _, stress_m = self.run(cls, best, a, d, Costs(fee * mult, slip * mult))

        say(f"  [{cls.label}] 워크포워드")
        wf = self.walk_forward(cls)

        months = (int(self.market.ts[d]) - int(self.market.ts[a])) / MONTH_SEC
        rep = StrategyReport(
            name=cls.name, label=cls.label, params=best, grid_size=len(table),
            train=train_m, val=val_m, full=full_m, full_gross=gross_m, full_stress=stress_m,
            neighbor_stats=neighbor_stats, walk_forward={k: v for k, v in wf.items() if k != "equity"},
            regimes=regime_breakdown(self.market, full_r, self.regimes),
            entry_signals_per_6m=full_r.entry_signals / months * 6 if months > 0 else float("nan"),
            rejections=full_r.rejections,
        )
        rep.curves = {
            "full_ts": self.market.ts[a: d + 1], "full_equity": full_r.equity,
            "val_equity": val_r.equity, "wf_equity": wf.get("equity"),
        }
        rep.flags = self.judge(rep)
        return rep

    def judge(self, r: StrategyReport) -> list[Flag]:
        rj = self.bt["reject"]
        flags: list[Flag] = []
        ts, vs = r.train["sharpe"], r.val["sharpe"]
        ts0, vs0 = _sharpe0(r.train), _sharpe0(r.val)
        if ts0 <= 0:
            flags.append(Flag("탈락", "학습 구간 성과 없음", f"학습 샤프 {ts0:.2f}"))
        if r.val["total_return"] <= 0:
            flags.append(Flag("탈락", "검증 구간 성과 붕괴", f"검증 수익률 {r.val['total_return']:+.1%}"))
        elif ts0 > 0 and vs0 < float(rj["val_sharpe_ratio_min"]) * ts0:
            flags.append(Flag("탈락", "검증 구간 성과 붕괴",
                              f"검증 샤프 {vs0:.2f} < 학습 샤프 {ts0:.2f} × {rj['val_sharpe_ratio_min']}"))
        oos = r.walk_forward.get("oos") or {}
        if oos and oos["total_return"] <= 0:
            flags.append(Flag("탈락", "워크포워드 표본 외 손실", f"표본 외 수익률 {oos['total_return']:+.1%}"))

        ns = r.neighbor_stats
        if ns["count"]:
            if ns["profitable_share"] < float(rj["neighbor_profitable_min"]):
                flags.append(Flag("탈락", "파라미터 민감 (과최적화)",
                                  f"이웃 파라미터 중 수익 비율 {ns['profitable_share']:.0%}"))
            gap = ts0 - ns["median_sharpe"]
            if gap > float(rj["neighbor_sharpe_gap_max"]):
                flags.append(Flag("탈락", "파라미터 민감 (과최적화)",
                                  f"선택값 샤프 {ts0:.2f}, 이웃 중앙값 {ns['median_sharpe']:.2f}"))

        if r.full["trades"] < int(rj["min_trades_total"]):
            flags.append(Flag("탈락", "거래 수 부족", f"전체 기간 {r.full['trades']}회"))
        if r.full["total_return"] <= 0:
            detail = f"수수료 반영 {r.full['total_return']:+.1%} (수수료 0일 때 {r.full_gross['total_return']:+.1%})"
            flags.append(Flag("탈락", "수수료 반영 후 수익 없음", detail))
        elif r.full_stress["total_return"] <= 0:
            flags.append(Flag("주의", "비용 2배에서 손실",
                              f"수수료·슬리피지 2배 시 {r.full_stress['total_return']:+.1%}"))

        min_sig = int(self.cfg["comparison"]["min_signals"])
        if r.entry_signals_per_6m < min_sig:
            flags.append(Flag("주의", "6개월 신호 수 부족 예상",
                              f"6개월당 진입 신호 약 {r.entry_signals_per_6m:.0f}건 (기준 {min_sig}건)"))
        return flags
