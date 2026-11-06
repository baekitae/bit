"""2단계 전략 비교 리포트 (Markdown)."""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from src.backtest.study import StrategyReport, Study

KST = timezone(timedelta(hours=9))


def _d(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=KST).strftime("%Y-%m-%d")


def _p(v: float | None, signed: bool = True) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    return f"{v:+.1%}" if signed else f"{v:.0%}"


def _f(v: float | None, nd: int = 2) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    return f"{v:.{nd}f}"


def _params(p: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v:g}" if isinstance(v, float) else f"{k}={v}" for k, v in p.items())


def render(study: Study, reports: list[StrategyReport], cfg: dict[str, Any]) -> str:
    m = study.market
    n = len(m)
    unit = m.unit
    L: list[str] = []
    a = L.append
    tf = "일봉" if unit == 1440 else (f"{unit // 60}시간봉" if unit >= 60 else f"{unit}분봉")
    a(f"# 규칙 전략 비교 리포트 ({tf})")
    a("")
    a(f"- 생성: {datetime.now(KST).strftime('%Y-%m-%d %H:%M')} KST")
    a(f"- 데이터: {cfg['market']} {unit}분봉 {n:,}개, {_d(m.ts[0])} ~ {_d(m.ts[-1])}")
    a(f"- 학습 구간: {_d(m.ts[0])} ~ {_d(m.ts[study.split - 1])} / 검증 구간: {_d(m.ts[study.split])} ~ {_d(m.ts[-1])}")
    wf = cfg["backtest"]["walk_forward"]
    a(f"- 워크포워드: 학습 {wf['train_months']}개월 → 검증 {wf['test_months']}개월, {len(study.folds())}회 반복")
    c = cfg["costs"]
    a(f"- 비용: 수수료 {c['fee_rate']:.2%} + 슬리피지 {c['slippage_rate']:.2%} (편도), 1회 진입 최대 {cfg['risk']['max_position_pct']:.0%}")
    a("- 파라미터 선택: 학습 구간에서 \"자기 자신 + 이웃 파라미터\"의 샤프 평균이 가장 높은 조합 (한 점만 튀는 값 배제)")
    a("")
    a("> 주의: 전략은 자금의 최대 30%만 투자합니다. 단순 보유(100% 투자)와 수익률을 직접 비교하면 불리하므로, "
      "\"30% 단순 보유\"(단순 보유 수익률 × 0.3)와 낙폭도 함께 보세요.")
    a("")

    # ---- 요약표 ----
    a("## 1. 요약")
    a("")
    a("| 전략 | 판정 | 검증 수익률 | 검증 샤프 | 워크포워드 표본 외 수익률 | 전체 수익률 | 전체 MDD | 거래 수 | 6개월당 신호 |")
    a("|---|---|---|---|---|---|---|---|---|")
    for r in reports:
        oos = r.walk_forward.get("oos") or {}
        verdict = "통과" if r.passed else "탈락"
        if r.passed and any(f.level == "주의" for f in r.flags):
            verdict = "통과(주의)"
        a(f"| {r.label} | **{verdict}** | {_p(r.val['total_return'])} | {_f(r.val['sharpe'])} | "
          f"{_p(oos.get('total_return'))} | {_p(r.full['total_return'])} | {_p(r.full['mdd'])} | "
          f"{r.full['trades']} | {r.entry_signals_per_6m:.0f} |")
    full0 = reports[0].full
    val0 = reports[0].val
    oos0 = reports[0].walk_forward.get("oos") or {}
    a(f"| 단순 보유 (100%) | - | {_p(val0['bh_return'])} | - | {_p(oos0.get('bh_return'))} | "
      f"{_p(full0['bh_return'])} | {_p(full0['bh_mdd'])} | - | - |")
    a(f"| 단순 보유 (30%) | - | {_p(val0['bh_return'] * 0.3)} | - | "
      f"{_p((oos0.get('bh_return') or float('nan')) * 0.3)} | {_p(full0['bh_return'] * 0.3)} | - | - | - |")
    a("")

    # ---- 전략별 상세 ----
    a("## 2. 전략별 상세")
    for r in reports:
        a("")
        a(f"### {r.label} (`{r.name}`)")
        a("")
        if r.fixed:
            a(f"- 파라미터: `{_params(r.params)}` (**고정값 그대로 검증**, 탐색하지 않음 / 민감도 비교용 격자 {r.grid_size}개)")
        else:
            a(f"- 선택 파라미터: `{_params(r.params)}` (격자 {r.grid_size}개 중)")
        if r.flags:
            for f in r.flags:
                a(f"- **{f.level}** — {f.rule}: {f.detail}")
        else:
            a("- 탈락 기준 해당 없음")
        a("")
        a("| 구간 | 수익률 | 연환산 | MDD | 샤프 | 거래 | 승률 | 손익비 | 보유 비율 | 단순보유 |")
        a("|---|---|---|---|---|---|---|---|---|---|")
        oos = r.walk_forward.get("oos") or {}
        for name, x in (("학습", r.train), ("검증", r.val), ("워크포워드 표본 외", oos), ("전체", r.full)):
            if not x:
                continue
            a(f"| {name} | {_p(x['total_return'])} | {_p(x['cagr'])} | {_p(x['mdd'])} | {_f(x['sharpe'])} | "
              f"{x['trades']} | {_p(x['win_rate'], False)} | {_f(x['payoff'])} | {_p(x['exposure'], False)} | "
              f"{_p(x['bh_return'])} |")
        a("")
        ns = r.neighbor_stats
        a(f"- 파라미터 민감도: 이웃 {ns['count']}개 중 수익 {_p(ns['profitable_share'], False)}, "
          f"이웃 샤프 중앙값 {_f(ns['median_sharpe'])} / 최저 {_f(ns['min_sharpe'])}, "
          f"격자 전체 중 수익 {_p(ns['grid_profitable_share'], False)}")
        a(f"- 수수료 영향 (전체 기간): 비용 0 → {_p(r.full_gross['total_return'])}, "
          f"현재 → {_p(r.full['total_return'])}, 비용 2배 → {_p(r.full_stress['total_return'])}")
        if r.rejections:
            rj = ", ".join(f"{k} {v}회" for k, v in sorted(r.rejections.items(), key=lambda x: -x[1]))
            a(f"- 리스크 관문 거부 (전체 기간): {rj}")
        a("")
        a("국면별 (전체 기간, 같은 국면 봉들의 수익을 이어 붙인 값):")
        a("")
        a("| 국면 | 시간 비중 | 전략 수익 | 단순보유 수익 | 진입 거래 | 승률 | 거래당 평균 |")
        a("|---|---|---|---|---|---|---|")
        for g in r.regimes:
            a(f"| {g['regime']} | {_p(g['share'], False)} | {_p(g['strategy_return'])} | {_p(g['bh_return'])} | "
              f"{g['trades']} | {_p(g['win_rate'], False)} | {_p(g['avg_trade'])} |")
        a("")
        a("워크포워드 구간별:")
        a("")
        a("| 검증 기간 | 선택 파라미터 | 수익률 | 단순보유 | 거래 | MDD |")
        a("|---|---|---|---|---|---|")
        for f in r.walk_forward.get("folds", []):
            a(f"| {_d(f['test_start'])} ~ {_d(f['test_end'])} | `{_params(f['params'])}` | "
              f"{_p(f['total_return'])} | {_p(f['bh_return'])} | {f['trades']} | {_p(f['mdd'])} |")

    a("")
    a("## 3. 판정 기준 (config.yaml `backtest.reject`)")
    a("")
    rj = cfg["backtest"]["reject"]
    a(f"- 탈락: 학습 샤프 ≤ 0 / 검증 수익 ≤ 0 또는 검증 샤프 < 학습 샤프 × {rj['val_sharpe_ratio_min']} / "
      f"워크포워드 표본 외 손실 / 이웃 파라미터 수익 비율 < {rj['neighbor_profitable_min']:.0%} 또는 "
      f"선택값-이웃 샤프 차이 > {rj['neighbor_sharpe_gap_max']} / 전체 거래 < {rj['min_trades_total']}회 / 수수료 반영 후 손실")
    a(f"- 주의: 비용 {rj['cost_stress_mult']:g}배에서 손실 / 6개월당 진입 신호 < {cfg['comparison']['min_signals']}건")
    a(f"- 국면 분류: 기준 이동평균 {cfg['regime']['ma_hours']}시간, 기울기 {cfg['regime']['slope_hours']}시간 "
      f"변화율 ±{cfg['regime']['slope_threshold']:.0%}")
    a("")
    return "\n".join(L)


def to_json(reports: list[StrategyReport]) -> str:
    def clean(o):
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (float, np.floating)):
            return None if math.isnan(float(o)) else float(o)
        return o
    out = []
    for r in reports:
        d = {k: getattr(r, k) for k in ("name", "label", "params", "grid_size", "train", "val", "full",
                                        "full_gross", "full_stress", "neighbor_stats", "walk_forward",
                                        "regimes", "entry_signals_per_6m", "rejections")}
        d["flags"] = [f.__dict__ for f in r.flags]
        d["passed"] = r.passed
        out.append(clean(d))
    return json.dumps(out, ensure_ascii=False, indent=2)


def write(study: Study, reports: list[StrategyReport], cfg: dict[str, Any], out_dir: Path,
          suffix: str | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{study.market.unit}m" + (f"_{suffix}" if suffix else "")
    md = out_dir / f"stage2_{tag}.md"
    md.write_text(render(study, reports, cfg), encoding="utf-8")
    (out_dir / f"stage2_{tag}.json").write_text(to_json(reports), encoding="utf-8")
    # 자산 곡선 (차트용)
    import pandas as pd
    cols = {"ts": reports[0].curves["full_ts"], "close": study.market.c}
    for r in reports:
        cols[r.name] = r.curves["full_equity"]
    pd.DataFrame(cols).to_csv(out_dir / f"stage2_{tag}_equity.csv", index=False)
    return md
