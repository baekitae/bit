"""무한매수법 시뮬레이션 리포트."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from src.backtest.metrics import max_drawdown

KST = timezone(timedelta(hours=9))


def _d(ts) -> str:
    return datetime.fromtimestamp(int(ts), tz=KST).strftime("%Y-%m-%d")


def _p(v) -> str:
    return "-" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v:+.1%}"


def write(runs: dict, p, costs, cfg: dict[str, Any], out_dir: Path) -> Path:
    L: list[str] = []
    a = L.append
    a("# 무한매수법 시뮬레이션 리포트 (비트코인 일봉)")
    a("")
    a(f"- 생성: {datetime.now(KST).strftime('%Y-%m-%d %H:%M')} KST")
    a(f"- 규칙: {p.splits}분할, 종가 지정가 매수(평단가 / 평단가+{p.loc_premium:.0%}), "
      f"평단가+{p.take_profit:.0%} 익절, 현금 소진 시 보유량 {p.quarter_frac:.0%} 쿼터 손절")
    a(f"- 비용: 수수료 {costs.fee_rate:.2%} + 슬리피지 {costs.slippage_rate:.2%} (편도, 모든 체결)")
    a("- 손절가 없이 자금을 최대 100%까지 쓰므로 리스크 관문(7장) 밖의 별도 시뮬레이터로 검증함")
    a("")
    a("## 요약")
    a("")
    a("| 기간 | 수익률 | 연환산 | MDD | 샤프 | 익절 사이클 | 쿼터 손절 | 평균 코인 비중 | 단순보유 | 단순보유 MDD |")
    a("|---|---|---|---|---|---|---|---|---|---|")
    for years, (d, r) in runs.items():
        m = r.metrics
        qs = int(r.cycles["quarter_stops"].sum()) if len(r.cycles) else 0
        a(f"| {_d(d['ts'].iloc[0])} ~ {_d(d['ts'].iloc[-1])} | {_p(m['total_return'])} | {_p(m['cagr'])} | "
          f"{_p(m['mdd'])} | {m['sharpe']:.2f} | {m['trades']} | {qs} | {m['avg_exposure']:.0%} | "
          f"{_p(m['bh_return'])} | {_p(m['bh_mdd'])} |")
    for years, (d, r) in runs.items():
        a("")
        a(f"## 최근 {years:g}년: 연도별")
        a("")
        a("| 연도 | 전략 | 단순보유 | 연중 MDD | 평균 코인 비중 |")
        a("|---|---|---|---|---|")
        yrs = np.array([datetime.fromtimestamp(int(t), tz=KST).year for t in d["ts"]])
        eq = np.concatenate([[r.equity[0]], r.equity])
        c = d["close"].to_numpy(float)
        for y in sorted(set(yrs)):
            idx = np.flatnonzero(yrs == y)
            s, e = idx[0], idx[-1]
            prev_eq = eq[s]  # 전날 자산 (첫날은 자기 자신)
            prev_c = c[s - 1] if s > 0 else c[s]
            a(f"| {y} | {_p(r.equity[e] / prev_eq - 1)} | {_p(c[e] / prev_c - 1)} | "
              f"{_p(max_drawdown(r.equity[s:e + 1]))} | {r.exposure[s:e + 1].mean():.0%} |")
        a("")
        a("가장 길었던 사이클 5개:")
        a("")
        a("| 시작 | 끝 | 일수 | 결과 | 사이클 수익률 | 최대 사용 회차 | 쿼터 손절 |")
        a("|---|---|---|---|---|---|---|")
        cy = r.cycles.assign(days=r.cycles["end_idx"] - r.cycles["start_idx"]).sort_values("days", ascending=False)
        for _, x in cy.head(5).iterrows():
            a(f"| {_d(d['ts'].iloc[int(x['start_idx'])])} | {_d(d['ts'].iloc[int(x['end_idx'])])} | {int(x['days'])} | "
              f"{x['result']} | {_p(x['pnl_pct'])} | {x['max_splits_used']:.1f} | {int(x['quarter_stops'])} |")
        done = r.cycles[r.cycles["result"] == "익절"]
        if len(done):
            dd = done["end_idx"] - done["start_idx"]
            a("")
            a(f"- 익절 사이클 {len(done)}회: 평균 {dd.mean():.0f}일, 중앙값 {dd.median():.0f}일, "
              f"평균 수익 {done['pnl_pct'].mean():+.1%}")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "infinite_1440m.md"
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    return path
