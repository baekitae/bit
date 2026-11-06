"""모의매매 대시보드 (Streamlit).

실행:  streamlit run src/report/dashboard.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.backtest.metrics import max_drawdown  # noqa: E402
from src.config import db_path, load_config  # noqa: E402
from src.tracks.registry import track_defs  # noqa: E402

KST = timezone(timedelta(hours=9))
# 전략(=트랙 짝)마다 고정 색. A와 B 차트에서 같은 전략은 같은 색 (검증된 범주형 팔레트 1~5번)
STRATEGY_COLORS = {
    "donchian": "#2a78d6", "ma_cross": "#eb6834", "rsi_reversion": "#1baf7a",
    "bollinger_reversion": "#eda100", "titan_rest": "#e87ba4",
}
STRATEGY_LABELS = {
    "donchian": "돈치안 돌파", "ma_cross": "이평 교차", "rsi_reversion": "RSI 평균회귀",
    "bollinger_reversion": "볼린저 평균회귀", "titan_rest": "타이탄 휴식",
}
DECISION_LABELS = {"follow": "따름", "reduce": "비중 축소", "skip": "건너뜀", "error": "오류(건너뜀)"}


def kst(ts) -> str:
    return datetime.fromtimestamp(int(ts), tz=KST).strftime("%m-%d %H:%M") if pd.notna(ts) else "-"


def get_conn():
    """읽기 전용 연결 (대시보드는 DB를 절대 고치지 않는다). 새로고침마다 새로 연다."""
    import os
    import sqlite3
    cfg = load_config()
    path = Path(os.environ.get("BOT_DB") or db_path(cfg))   # BOT_DB: 다른 DB 파일 보기
    if not path.exists():
        st.error(f"DB 파일이 없습니다: {path}. 먼저 `python run.py live` 를 실행하세요.")
        st.stop()
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return cfg, conn


def q(conn, sql, params=()):
    return pd.read_sql_query(sql, conn, params=params)


def main() -> None:
    st.set_page_config(page_title="BTC 위원회 모의매매", layout="wide")
    cfg, conn = get_conn()
    defs = track_defs(cfg)

    runs = q(conn, "SELECT DISTINCT run_id FROM tracks ORDER BY run_id")["run_id"].tolist()
    if not runs:
        st.info("아직 기록이 없습니다. `python run.py live` 를 먼저 실행하세요.")
        return
    run_id = st.sidebar.selectbox("실행 (run_id)", runs, index=runs.index("live") if "live" in runs else 0)
    if st.sidebar.button("새로고침"):
        st.rerun()

    tracks = q(conn, "SELECT * FROM tracks WHERE run_id=? ORDER BY track", (run_id,))
    equity = q(conn, "SELECT track, bar_ts, equity FROM equity WHERE run_id=? ORDER BY bar_ts", (run_id,))
    trades = q(conn, "SELECT * FROM trades WHERE run_id=?", (run_id,))
    positions = q(conn, "SELECT * FROM positions WHERE run_id=?", (run_id,))
    meetings = q(conn, "SELECT * FROM council_meetings WHERE run_id=? ORDER BY id DESC", (run_id,))

    st.title("BTC AI 위원회 모의매매")
    last = tracks["updated_at"].max()
    st.caption(f"run_id: {run_id} · 마지막 갱신 {last} (UTC) · 모든 매매는 모의(paper)입니다")

    # ---- 1. 트랙 요약 ----
    st.header("트랙별 성과")
    rows = []
    for _, t in tracks.iterrows():
        name = t["track"]
        d = defs.get(name, {})
        e = equity[equity["track"] == name]["equity"].to_numpy()
        tr = trades[trades["track"] == name]
        pos = positions[positions["track"] == name]
        rows.append({
            "트랙": name,
            "종류": "AI 위원회" if d.get("type") == "council" else "규칙",
            "전략": STRATEGY_LABELS.get(t["strategy"], t["strategy"]),
            "봉": "일봉" if int(d.get("timeframe", 240)) == 1440 else "4시간봉",
            "자산(원)": round(t["last_equity"] or t["cash"]),
            "수익률": (t["last_equity"] or t["cash"]) / t["initial_cash"] - 1,
            "최대낙폭": max_drawdown(np.concatenate([[t["initial_cash"]], e])) if len(e) else 0.0,
            "거래": len(tr),
            "승률": (tr["pnl"] > 0).mean() if len(tr) else np.nan,
            "보유 중": "예" if len(pos) else "-",
            "손절가": round(pos["stop_loss"].iloc[0]) if len(pos) else np.nan,
            "마지막 봉": kst(t["last_bar_ts"]),
        })
    summary = pd.DataFrame(rows)
    st.dataframe(summary, hide_index=True, width="stretch", column_config={
        "수익률": st.column_config.NumberColumn(format="percent"),
        "최대낙폭": st.column_config.NumberColumn(format="percent"),
        "승률": st.column_config.NumberColumn(format="percent"),
        "자산(원)": st.column_config.NumberColumn(format="localized"),
        "손절가": st.column_config.NumberColumn(format="localized"),
    })

    # ---- 2. 자산 곡선 (A, B 따로: 같은 전략은 같은 색) ----
    st.header("자산 곡선")
    if not equity.empty:
        meta = tracks.set_index("track")
        eq = equity.copy()
        eq["수익률"] = eq.apply(lambda r: r["equity"] / meta.loc[r["track"], "initial_cash"] - 1, axis=1)
        eq["시각"] = pd.to_datetime(eq["bar_ts"], unit="s", utc=True).dt.tz_convert("Asia/Seoul")
        eq["strategy"] = eq["track"].map(meta["strategy"])
        eq["전략"] = eq["strategy"].map(STRATEGY_LABELS)
        eq["종류"] = eq["track"].map(lambda n: "B · AI 위원회" if defs.get(n, {}).get("type") == "council" else "A · 규칙")
        domain = [STRATEGY_LABELS[k] for k in STRATEGY_COLORS]
        scale = alt.Scale(domain=domain, range=list(STRATEGY_COLORS.values()))
        cols = st.columns(2)
        for col, kind in zip(cols, ["A · 규칙", "B · AI 위원회"]):
            sub = eq[eq["종류"] == kind]
            with col:
                st.subheader(kind)
                if sub.empty:
                    st.caption("기록 없음")
                    continue
                hover = alt.selection_point(fields=["시각"], nearest=True, on="pointerover", empty=False)
                base = alt.Chart(sub).encode(
                    x=alt.X("시각:T", title=None, axis=alt.Axis(grid=False, format="%m-%d")),
                    y=alt.Y("수익률:Q", title=None, axis=alt.Axis(format="%", gridOpacity=0.3)),
                    color=alt.Color("전략:N", scale=scale, legend=alt.Legend(orient="top", title=None, columns=3)),
                )
                lines = base.mark_line(strokeWidth=2)
                points = base.mark_point(size=60, filled=True).encode(
                    opacity=alt.condition(hover, alt.value(1), alt.value(0)),
                    tooltip=[alt.Tooltip("track:N", title="트랙"), alt.Tooltip("전략:N"),
                             alt.Tooltip("시각:T", format="%Y-%m-%d %H:%M"),
                             alt.Tooltip("수익률:Q", format="+.2%"),
                             alt.Tooltip("equity:Q", title="자산", format=",.0f")],
                ).add_params(hover)
                rule = alt.Chart(sub).mark_rule(color="#888", strokeWidth=1).encode(x="시각:T").transform_filter(hover)
                st.altair_chart((lines + points + rule).properties(height=280), width="stretch")

    # ---- 3. AI 필터 효과 (신호 단위 짝비교, DESIGN.md 1-1) ----
    st.header("AI 필터 효과 (B vs A, 신호 단위)")
    so = q(conn, "SELECT * FROM signal_outcomes WHERE run_id=?", (run_id,))
    min_sig = int(cfg["comparison"]["min_signals"])
    if so.empty:
        st.caption("아직 위원회가 열린 신호가 없습니다.")
    else:
        done = so[so["a_pnl_pct"].notna()]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("위원회 소집 신호", f"{len(so)}건", help=f"판정 기준: {min_sig}건 이상")
        c2.metric("A 결과 확정", f"{len(done)}건")
        follow = done[done["b_decision"] == "follow"]["a_pnl_pct"]
        skip = done[done["b_decision"].isin(["skip", "error"])]["a_pnl_pct"]
        c3.metric("따른 신호의 A 평균 수익", f"{follow.mean():+.2%}" if len(follow) else "-")
        c4.metric("건너뛴 신호의 A 평균 수익", f"{skip.mean():+.2%}" if len(skip) else "-",
                  help="음수일수록 AI가 손실 신호를 잘 걸렀다는 뜻")
        if len(so) < min_sig:
            st.caption(f"표본 {len(so)}건 / 기준 {min_sig}건: 아직 결론을 내기에는 이릅니다.")
        tab = (so.assign(결정=so["b_decision"].map(DECISION_LABELS).fillna(so["b_decision"]))
                 .groupby("결정").agg(신호=("id", "count"), A_결과확정=("a_pnl_pct", "count"),
                                     A_평균수익=("a_pnl_pct", "mean"), B_평균수익=("b_pnl_pct", "mean"))
                 .reset_index())
        st.dataframe(tab, hide_index=True, column_config={
            "A_평균수익": st.column_config.NumberColumn("A 평균 수익(거래당)", format="percent"),
            "B_평균수익": st.column_config.NumberColumn("B 평균 수익(거래당)", format="percent")})

    # ---- 4. 위원회 회의록 ----
    st.header("위원회 회의록")
    if meetings.empty:
        st.caption("아직 회의가 없습니다.")
    else:
        total_cost = meetings["cost_krw"].fillna(0).sum()
        c1, c2, c3 = st.columns(3)
        c1.metric("회의 수", f"{len(meetings)}회")
        c2.metric("누적 AI 비용", f"{total_cost:,.0f}원")
        c3.metric("오류·건너뜀", f"{meetings['error'].notna().sum()}회")
        for _, m in meetings.head(50).iterrows():
            dec = json.loads(m["decision"] or "{}").get("applied", {})
            title = (f"#{m['id']} · {m['track']} · {kst(m['bar_ts'])} 봉 · "
                     f"{DECISION_LABELS.get(dec.get('decision'), dec.get('decision', '-'))}"
                     + (f" · 오류: {m['error']}" if m["error"] else f" · 확신 {dec.get('confidence', 0):.2f}"))
            with st.expander(title):
                raw = json.loads(m["decision"] or "{}").get("raw") or {}
                if raw:
                    st.markdown(f"**위원장 결정**: {raw.get('decision')} · 비중 x{dec.get('size_multiplier', 0):.2f} · "
                                f"적용 손절가 {dec.get('stop_loss') or 0:,.0f}원")
                    st.markdown(f"**근거**: {raw.get('reasoning', '')}")
                    st.markdown(f"**레드팀 핵심 반론**: {raw.get('dissent', '')}")
                for n in dec.get("notes", []):
                    st.caption(f"코드 보정: {n}")
                tr = json.loads(m["transcript"] or "{}")
                names = {"trend": "추세 분석가", "regime": "국면·위험 분석가", "red_team": "레드팀"}
                cols = st.columns(3)
                for col, (k, label) in zip(cols, names.items()):
                    v = tr.get(k)
                    with col:
                        st.markdown(f"**{label}** ({v.get('stance', '-') if v else '-'})")
                        if v:
                            st.write(v.get("summary", ""))
                            for p in v.get("points", []):
                                st.markdown(f"- {p}")
                            for r in v.get("risks", []):
                                st.markdown(f"- ⚠ {r}")
                prov = json.loads(m["providers"] or "{}")
                if prov:
                    models = ", ".join(sorted({p["model"] for p in prov.values()}))
                    st.caption(f"모델: {models} · 토큰 {m['input_tokens']:,}/{m['output_tokens']:,} · "
                               f"비용 {m['cost_krw'] or 0:,.0f}원 · {m['latency_sec']:.0f}초 · 프롬프트 {m['prompt_version']}")
                with st.popover("입력 데이터 보기"):
                    st.json(json.loads(m["input"] or "{}"))

    # ---- 5. 최근 기록 ----
    st.header("최근 기록")
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("리스크 관문 거부")
        rc = q(conn, """SELECT track, bar_ts, reasons FROM risk_checks WHERE run_id=? AND approved=0
                        ORDER BY id DESC LIMIT 30""", (run_id,))
        if rc.empty:
            st.caption("없음")
        else:
            rc["봉"] = rc["bar_ts"].map(kst)
            rc["이유"] = rc["reasons"].map(lambda s: ", ".join(json.loads(s)))
            st.dataframe(rc[["track", "봉", "이유"]], hide_index=True, width="stretch")
    with c2:
        st.subheader("최근 거래")
        if trades.empty:
            st.caption("없음")
        else:
            t = trades.sort_values("exit_ts", ascending=False).head(30).copy()
            t["진입"] = t["entry_ts"].map(kst)
            t["청산"] = t["exit_ts"].map(kst)
            st.dataframe(t[["track", "진입", "청산", "pnl_pct", "exit_kind"]], hide_index=True,
                         width="stretch",
                         column_config={"pnl_pct": st.column_config.NumberColumn("수익률", format="percent")})
    log = q(conn, "SELECT created_at, event, detail FROM run_log WHERE run_id=? ORDER BY id DESC LIMIT 30", (run_id,))
    with st.expander("실행 로그"):
        st.dataframe(log, hide_index=True, width="stretch")


main()
