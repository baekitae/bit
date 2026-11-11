"""모의매매 대시보드 (Streamlit). DB 는 읽기만 한다.

실행:  streamlit run src/report/dashboard.py
"""
from __future__ import annotations

import json
import os
import sqlite3
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
BAR_SEC = 4 * 3600

# 색: 검증된 범주형 팔레트 (dataviz 검증 통과). 같은 대상은 어느 차트에서든 같은 색
STRATEGY_COLORS = {
    "donchian": "#2a78d6", "ma_cross": "#eb6834", "rsi_reversion": "#1baf7a",
    "bollinger_reversion": "#eda100", "titan_rest": "#e87ba4",
}
STRATEGY_LABELS = {
    "donchian": "돈치안 돌파", "ma_cross": "이평 교차", "rsi_reversion": "RSI 평균회귀",
    "bollinger_reversion": "볼린저 평균회귀", "titan_rest": "타이탄 휴식",
}
# 메인 비교(같은 돈치안 전략, 방식만 다름): 방식별 색
MAIN = {"A1": ("A1 · 규칙", "#2a78d6"), "B1": ("B1 · Claude", "#eb6834"),
        "G1": ("G1 · Gemini", "#1baf7a"), "C": ("C · 진화", "#6250d6")}
BENCH = ("비트코인 30% 보유", "#8a8a85")
DECISION_LABELS = {"follow": "따름", "reduce": "비중 축소", "skip": "건너뜀", "error": "오류(건너뜀)"}
KIND_LABELS = {"entry": "진입", "exit_signal": "신호 청산", "stop": "손절", "trail_stop": "트레일링 손절",
               "delayed_stop": "지연 손절"}
GROUPS = ["A · 규칙", "B · Claude 위원회", "G · Gemini 위원회", "C · 진화"]

CSS = """
<style>
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css');
/* 아이콘 글꼴(Material Symbols)은 건드리지 않는다 */
.stApp :is(p, li, label, input, textarea, select, button, h1, h2, h3, h4, h5, h6, td, th,
           div:not([data-testid="stIconMaterial"]), span:not([data-testid="stIconMaterial"])) {
  font-family: Pretendard, -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", "Malgun Gothic", sans-serif;
}
.block-container { padding-top: 3.4rem; padding-bottom: 3rem; max-width: 1360px; }
[data-testid="stMetricValue"] { font-size: 1.55rem; font-weight: 700; letter-spacing: -0.03em; }
[data-testid="stMetricLabel"] p { font-size: .8rem; opacity: .75; }
@media (max-width: 640px) { .app-title { font-size: 1.35rem !important; } .kpi .val { font-size: 1.35rem; } }
h1, h2, h3 { letter-spacing: -0.02em; }
.app-title { font-size: 1.75rem !important; font-weight: 750; letter-spacing: -0.035em; line-height: 1.25; }
.app-sub { opacity: .62; font-size: .86rem; margin: .2rem 0 .9rem 0; }
.pills { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 1.1rem; }
.pill { display: inline-flex; align-items: center; gap: 6px; padding: 4px 11px; border-radius: 999px;
        font-size: .8rem; border: 1px solid rgba(128,128,128,.28); background: rgba(128,128,128,.06); }
.pill b { font-weight: 650; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin-bottom: .6rem; }
.kpi { border: 1px solid rgba(128,128,128,.22); border-radius: 14px; padding: 14px 16px 12px 16px;
       background: rgba(128,128,128,.045); }
.kpi .lab { font-size: .78rem; opacity: .68; display: flex; align-items: center; gap: 7px; }
.kpi .sw { width: 10px; height: 10px; border-radius: 3px; display: inline-block; }
.kpi .val { font-size: 1.6rem; font-weight: 700; letter-spacing: -0.03em; margin-top: 2px; }
.kpi .sub { font-size: .78rem; opacity: .7; margin-top: 1px; }
.up { color: #1a7f37; } .down { color: #cf222e; }
@media (prefers-color-scheme: dark) { .up { color: #3fb950; } .down { color: #f85149; } }
.note { font-size: .82rem; opacity: .7; }
.card { border: 1px solid rgba(128,128,128,.22); border-radius: 14px; padding: 4px 14px 10px 14px;
        background: rgba(128,128,128,.03); }
div[data-testid="stTabs"] button p { font-size: .95rem; font-weight: 600; }
div[data-testid="stExpander"] details { border-radius: 12px; }
</style>
"""


# ---------- 데이터 ----------
def kst(ts, fmt: str = "%m-%d %H:%M") -> str:
    return datetime.fromtimestamp(int(ts), tz=KST).strftime(fmt) if ts is not None and pd.notna(ts) else "-"


def pct(v: float | None, digits: int = 2) -> str:
    return "-" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v:+.{digits}%}"


def tone(v: float | None) -> str:
    return "" if v is None or (isinstance(v, float) and np.isnan(v)) or v == 0 else ("up" if v > 0 else "down")


def group_of(name: str, defs: dict) -> str:
    d = defs.get(name.split("_")[0], {})
    if d.get("type") == "evolving":
        return GROUPS[3]
    if d.get("type") == "council":
        return GROUPS[2] if d.get("council") == "gemini" else GROUPS[1]
    return GROUPS[0]


def connect_ro(cfg) -> sqlite3.Connection:
    path = Path(os.environ.get("BOT_DB") or db_path(cfg))   # BOT_DB: 다른 DB 파일 보기
    if not path.exists():
        st.error(f"DB 파일이 없습니다: {path}. 먼저 `python run.py live` 를 실행하세요.")
        st.stop()
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def q(conn, sql, params=()):
    return pd.read_sql_query(sql, conn, params=params)


def load(conn, run_id: str) -> dict:
    d = {
        "tracks": q(conn, "SELECT * FROM tracks WHERE run_id=? ORDER BY track", (run_id,)),
        "equity": q(conn, "SELECT track, bar_ts, equity FROM equity WHERE run_id=? ORDER BY bar_ts", (run_id,)),
        "trades": q(conn, "SELECT * FROM trades WHERE run_id=?", (run_id,)),
        "positions": q(conn, "SELECT * FROM positions WHERE run_id=?", (run_id,)),
        "meetings": q(conn, "SELECT * FROM council_meetings WHERE run_id=? ORDER BY id DESC", (run_id,)),
        "outcomes": q(conn, "SELECT * FROM signal_outcomes WHERE run_id=?", (run_id,)),
        "log": q(conn, "SELECT created_at, event, detail FROM run_log WHERE run_id=? ORDER BY id DESC LIMIT 200", (run_id,)),
    }
    start = d["log"].loc[d["log"]["event"] == "comparison_start", "created_at"]
    d["start"] = start.min() if len(start) else None
    return d


def benchmark(conn, eq: pd.DataFrame, pct_invested: float) -> pd.DataFrame:
    """같은 기간 비트코인을 자금의 pct_invested 만큼 들고 있었을 때의 수익률 (전략과 공정 비교)."""
    if eq.empty:
        return pd.DataFrame(columns=["bar_ts", "ret"])
    ts = sorted(eq["bar_ts"].unique())
    px = q(conn, "SELECT ts AS bar_ts, close FROM candles WHERE unit=240 AND ts >= ? AND ts <= ? ORDER BY ts",
           (int(ts[0]), int(ts[-1])))
    if px.empty:
        return pd.DataFrame(columns=["bar_ts", "ret"])
    px["ret"] = (px["close"] / px["close"].iloc[0] - 1) * pct_invested
    return px[["bar_ts", "ret"]]


def track_table(d: dict, defs: dict) -> pd.DataFrame:
    rows = []
    for _, t in d["tracks"].iterrows():
        name = t["track"]
        e = d["equity"][d["equity"]["track"] == name]["equity"].to_numpy()
        tr = d["trades"][d["trades"]["track"] == name]
        pos = d["positions"][d["positions"]["track"] == name]
        df = defs.get(name.split("_")[0], {})
        eq = t["last_equity"] if pd.notna(t["last_equity"]) else t["cash"]
        rows.append({
            "트랙": name,
            "구분": group_of(name, defs).split(" · ")[1],
            "전략": STRATEGY_LABELS.get(t["strategy"], t["strategy"]),
            "봉": "일봉" if int(df.get("timeframe", 240)) == 1440 else "4시간",
            "자산": eq,
            "수익률": eq / t["initial_cash"] - 1,
            "최대낙폭": max_drawdown(np.concatenate([[t["initial_cash"]], e])) if len(e) else 0.0,
            "거래": len(tr),
            "승률": (tr["pnl"] > 0).mean() if len(tr) else np.nan,
            "포지션": "보유" if len(pos) else "—",
            "손절가": f'{pos["stop_loss"].iloc[0]:,.0f}' if len(pos) else "—",
        })
    return pd.DataFrame(rows)


# ---------- 화면 조각 ----------
def header(d: dict, cfg: dict, run_id: str) -> None:
    now = datetime.now(timezone.utc).timestamp()
    last = d["tracks"]["last_bar_ts"].max() if not d["tracks"].empty else None
    lag = now - (last + BAR_SEC) if last is not None and pd.notna(last) else None
    ok = lag is not None and lag < BAR_SEC + 1800
    status = ("#2da44e", "정상 실행 중") if ok else ("#d1242f", "지연 · 확인 필요")
    nxt = (int(now) // BAR_SEC + 1) * BAR_SEC
    cost = d["meetings"]["cost_krw"].fillna(0).sum() if not d["meetings"].empty else 0.0
    months = int(cfg["comparison"]["months"])
    days = "-"
    if d["start"]:
        d0 = datetime.fromisoformat(d["start"]).replace(tzinfo=timezone.utc)
        days = f"{(datetime.now(timezone.utc) - d0).days + 1}일째 / {months * 30}일"
    st.markdown('<div class="app-title">BTC AI 위원회 · 모의매매</div>'
                f'<div class="app-sub">업비트 KRW-BTC · 4시간봉 · 모든 매매는 가상(paper)입니다 · run: {run_id}</div>',
                unsafe_allow_html=True)
    pills = [
        f'<span class="pill"><span class="dot" style="background:{status[0]}"></span><b>{status[1]}</b></span>',
        f'<span class="pill">마지막 봉 <b>{kst(last)}</b></span>',
        f'<span class="pill">다음 처리 <b>{kst(nxt, "%H:%M")}</b></span>',
        f'<span class="pill">6개월 비교 <b>{days}</b></span>',
        f'<span class="pill">누적 AI 비용 <b>{cost:,.0f}원</b></span>',
    ]
    st.markdown(f'<div class="pills">{"".join(pills)}</div>', unsafe_allow_html=True)


def kpis(d: dict, table: pd.DataFrame, bench_ret: float | None) -> None:
    cards = []
    t = table.set_index("트랙") if not table.empty else table
    for key, (label, color) in MAIN.items():
        if key not in getattr(t, "index", []):
            continue
        r = t.loc[key]
        sub = f'{r["자산"]:,.0f}원 · {int(r["거래"])}회{" · 보유 중" if r["포지션"] == "보유" else ""}'
        cards.append(f'<div class="kpi"><div class="lab"><span class="sw" style="background:{color}"></span>{label}</div>'
                     f'<div class="val {tone(r["수익률"])}">{pct(r["수익률"])}</div><div class="sub">{sub}</div></div>')
    cards.append(f'<div class="kpi"><div class="lab"><span class="sw" style="background:{BENCH[1]}"></span>{BENCH[0]}</div>'
                 f'<div class="val {tone(bench_ret)}">{pct(bench_ret)}</div>'
                 f'<div class="sub">같은 기간 비교 기준</div></div>')
    st.markdown(f'<div class="kpis">{"".join(cards)}</div>', unsafe_allow_html=True)


def line_chart(df: pd.DataFrame, color_field: str, domain: list, colors: list, height: int = 320,
               dashed: str | None = None, legend_cols: int = 6) -> alt.Chart:
    hover = alt.selection_point(fields=["시각"], nearest=True, on="pointerover", empty=False)
    enc = dict(
        x=alt.X("시각:T", title=None, axis=alt.Axis(grid=False, format="%m/%d", labelColor="#888", tickColor="#ccc")),
        y=alt.Y("수익률:Q", title=None, axis=alt.Axis(format="%", gridOpacity=0.18, labelColor="#888", domain=False)),
        color=alt.Color(f"{color_field}:N", scale=alt.Scale(domain=domain, range=colors),
                        legend=alt.Legend(orient="top", title=None, symbolType="stroke", symbolStrokeWidth=3,
                                          labelFontSize=12, columns=legend_cols)),
    )
    base = alt.Chart(df).encode(**enc)
    stroke = alt.condition(f"datum['{color_field}'] == '{dashed}'", alt.value([5, 4]), alt.value([0])) if dashed else alt.value([0])
    lines = base.mark_line(strokeWidth=2, interpolate="step-after").encode(strokeDash=stroke)
    pts = base.mark_point(size=70, filled=True, stroke="white", strokeWidth=1.5).encode(
        opacity=alt.condition(hover, alt.value(1), alt.value(0)),
        tooltip=[alt.Tooltip(f"{color_field}:N", title="항목"), alt.Tooltip("시각:T", format="%Y-%m-%d %H:%M"),
                 alt.Tooltip("수익률:Q", format="+.2%")],
    ).add_params(hover)
    rule = alt.Chart(df).mark_rule(color="#999", strokeWidth=1, opacity=.6).encode(x="시각:T").transform_filter(hover)
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color="#999", opacity=.35).encode(y="y:Q")
    return (zero + lines + pts + rule).properties(height=height).configure_view(strokeWidth=0)


def equity_frame(d: dict) -> pd.DataFrame:
    eq = d["equity"].copy()
    if eq.empty:
        return eq
    init = d["tracks"].set_index("track")["initial_cash"]
    eq["수익률"] = eq["equity"] / eq["track"].map(init) - 1
    eq["시각"] = pd.to_datetime(eq["bar_ts"], unit="s", utc=True).dt.tz_convert("Asia/Seoul")
    return eq


def tab_overview(d, conn, defs, table, eq) -> None:
    st.markdown("#### 메인 비교 · 같은 전략(돈치안), 판단 방식만 다름")
    st.markdown('<p class="note">A1 은 규칙 그대로, B1·G1 은 AI 위원회가 진입을 거르고, C 는 AI 가 전략도 고칩니다. '
                '회색 점선은 같은 기간 비트코인을 자금의 30% 만큼 들고 있었을 때입니다 (전략의 최대 진입 비중과 같은 조건).</p>',
                unsafe_allow_html=True)
    if eq.empty:
        st.info("아직 자산 기록이 없습니다. 첫 봉이 처리되면 나타납니다.")
    else:
        main = eq[eq["track"].isin(MAIN)].copy()
        main["항목"] = main["track"].map(lambda k: MAIN[k][0])
        b = benchmark(conn, eq, 0.30)
        if not b.empty:
            b = b.assign(항목=BENCH[0], 시각=pd.to_datetime(b["bar_ts"], unit="s", utc=True).dt.tz_convert("Asia/Seoul"),
                         수익률=b["ret"])
            main = pd.concat([main[["항목", "시각", "수익률"]], b[["항목", "시각", "수익률"]]])
        present = [v[0] for k, v in MAIN.items() if k in set(eq["track"])] + ([BENCH[0]] if not b.empty else [])
        colors = [v[1] for k, v in MAIN.items() if k in set(eq["track"])] + ([BENCH[1]] if not b.empty else [])
        st.altair_chart(line_chart(main, "항목", present, colors, 340, dashed=BENCH[0]), width="stretch")

    st.markdown("#### 전체 트랙 순위")
    if table.empty:
        st.caption("기록 없음")
        return
    t = table.sort_values("수익률", ascending=False)
    st.dataframe(
        t, hide_index=True, width="stretch", height=min(38 * (len(t) + 1) + 4, 600),
        column_config={
            "자산": st.column_config.NumberColumn(format="%,.0f 원"),
            "수익률": st.column_config.NumberColumn(format="percent"),
            "최대낙폭": st.column_config.NumberColumn(format="percent"),
            "승률": st.column_config.NumberColumn(format="percent"),
        })


def tab_groups(d, defs, eq) -> None:
    if eq.empty:
        st.info("아직 자산 기록이 없습니다.")
        return
    meta = d["tracks"].set_index("track")
    e = eq.copy()
    e["묶음"] = e["track"].map(lambda n: group_of(n, defs))
    e["전략"] = e["track"].map(meta["strategy"]).map(STRATEGY_LABELS)
    e["계열"] = np.where(e["묶음"] == GROUPS[3], e["track"], e["전략"])
    color_of = {STRATEGY_LABELS[k]: v for k, v in STRATEGY_COLORS.items()}
    for name, col in zip(sorted(e.loc[e["묶음"] == GROUPS[3], "track"].unique()), ["#6250d6", "#008300", "#e34948"]):
        color_of[name] = col
    st.markdown('<p class="note">같은 전략은 어느 묶음에서든 같은 색입니다. A 와 B·G 를 나란히 보면 AI 필터의 효과가 보입니다.</p>',
                unsafe_allow_html=True)
    rows = [st.columns(2), st.columns(2)]
    for col, g in zip([rows[0][0], rows[0][1], rows[1][0], rows[1][1]], GROUPS):
        sub = e[e["묶음"] == g]
        with col:
            st.markdown(f"##### {g}")
            if sub.empty:
                st.caption("기록 없음")
                continue
            present = [k for k in color_of if k in set(sub["계열"])]
            st.altair_chart(line_chart(sub, "계열", present, [color_of[k] for k in present], 250, legend_cols=3),
                            width="stretch")


def tab_council(d, cfg) -> None:
    so = d["outcomes"].copy()
    min_sig = int(cfg["comparison"]["min_signals"])
    st.markdown("#### AI 필터 효과 · 신호 단위 짝비교")
    st.markdown('<p class="note">같은 진입 신호를 A(규칙)는 그대로 따르고, 위원회는 따름/비중 축소/건너뜀을 고릅니다. '
                '<b>건너뛴 신호의 A 결과가 손실</b>일수록 AI 가 손실을 잘 걸렀다는 뜻입니다.</p>', unsafe_allow_html=True)
    if so.empty:
        st.info("아직 위원회가 열린 신호가 없습니다. 진입 신호는 며칠에 한 번꼴로 나옵니다.")
    else:
        so["위원회"] = so["b_track"].map(lambda n: "Gemini" if str(n).startswith("G") else "Claude")
        cols = st.columns(2)
        for col, who in zip(cols, ["Claude", "Gemini"]):
            sub = so[so["위원회"] == who]
            with col:
                st.markdown(f"##### {who} ({'B' if who == 'Claude' else 'G'} 트랙)")
                if sub.empty:
                    st.caption("기록 없음")
                    continue
                done = sub[sub["a_pnl_pct"].notna()]
                follow = done[done["b_decision"] == "follow"]["a_pnl_pct"]
                skip = done[done["b_decision"].isin(["skip", "error"])]["a_pnl_pct"]
                c1, c2, c3 = st.columns(3)
                c1.metric("소집 신호", f"{len(sub)}건", help=f"결론 기준: {min_sig}건 이상")
                c2.metric("따른 신호의 A 평균", pct(follow.mean()) if len(follow) else "-")
                c3.metric("건너뛴 신호의 A 평균", pct(skip.mean()) if len(skip) else "-")
                st.progress(min(len(sub) / min_sig, 1.0), text=f"표본 {len(sub)} / {min_sig}건")
                tab = (sub.assign(결정=sub["b_decision"].map(DECISION_LABELS).fillna(sub["b_decision"]))
                          .groupby("결정").agg(신호=("id", "count"), 결과확정=("a_pnl_pct", "count"),
                                              A=("a_pnl_pct", "mean"), 위원회=("b_pnl_pct", "mean"))
                          .reset_index())
                tab["A"] = tab["A"].map(lambda v: pct(v))
                tab["위원회"] = tab["위원회"].map(lambda v: pct(v) if pd.notna(v) else "진입 안 함")
                st.dataframe(tab.rename(columns={"결과확정": "결과 확정", "A": "A 거래당 평균",
                                                 "위원회": "위원회 거래당 평균"}),
                             hide_index=True, width="stretch")
        both = so.pivot_table(index="a_signal_id", columns="위원회", values="b_decision", aggfunc="first").dropna()
        if len(both) and {"Claude", "Gemini"} <= set(both.columns):
            st.caption(f"같은 신호를 두 위원회가 모두 판단한 {len(both)}건 중 결정 일치율 "
                       f"{(both['Claude'] == both['Gemini']).mean():.0%}")

    st.markdown("#### 회의록")
    m = d["meetings"]
    if m.empty:
        st.caption("아직 회의가 없습니다.")
        return
    c1, c2 = st.columns([1, 3])
    tracks = ["전체"] + sorted(m["track"].unique())
    pick = c1.selectbox("트랙", tracks, label_visibility="collapsed")
    if pick != "전체":
        m = m[m["track"] == pick]
    names = {"trend": "추세 분석가", "regime": "국면·위험 분석가", "red_team": "레드팀"}
    for _, r in m.head(40).iterrows():
        dec = json.loads(r["decision"] or "{}")
        a, raw = dec.get("applied", {}), dec.get("raw") or {}
        tag = DECISION_LABELS.get(a.get("decision"), a.get("decision", "-"))
        title = (f"{kst(r['bar_ts'])}  ·  {r['track']}  ·  {tag}"
                 + (f"  ·  ⚠ {str(r['error'])[:60]}" if r["error"] else f"  ·  확신 {a.get('confidence', 0):.2f}  ·  {r['cost_krw'] or 0:,.0f}원"))
        with st.expander(title):
            if raw:
                st.markdown(f"**결정** {raw.get('decision')} · 비중 ×{a.get('size_multiplier', 0):.2f} · "
                            f"적용 손절가 {a.get('stop_loss') or 0:,.0f}원")
                st.markdown(f"**근거** {raw.get('reasoning', '')}")
                st.markdown(f"**레드팀 핵심 반론** {raw.get('dissent', '')}")
            for n in a.get("notes", []):
                st.caption(f"코드 보정: {n}")
            tr = json.loads(r["transcript"] or "{}")
            cols = st.columns(3)
            for col, (k, label) in zip(cols, names.items()):
                v = tr.get(k)
                with col:
                    st.markdown(f"**{label}** · {v.get('stance', '-') if v else '-'}")
                    if v:
                        st.write(v.get("summary", ""))
            prov = json.loads(r["providers"] or "{}")
            if prov:
                models = ", ".join(sorted({p["model"] for p in prov.values()}))
                st.caption(f"{models} · 토큰 {r['input_tokens']:,}/{r['output_tokens']:,} · {r['latency_sec']:.0f}초 · "
                           f"프롬프트 {r['prompt_version']}")


def tab_evolution(conn, run_id) -> None:
    est = q(conn, "SELECT * FROM evolution_state WHERE run_id=?", (run_id,))
    vers = q(conn, "SELECT * FROM strategy_versions WHERE run_id=? ORDER BY id", (run_id,))
    if est.empty:
        st.info("C 트랙이 아직 시작되지 않았습니다.")
        return
    e = est.iloc[0]
    phase = "고정 운영" if e["phase"] == "fixed" else f"섀도 운영 중 ({e['shadow_version']})"
    c1, c2, c3 = st.columns(3)
    c1.metric("현재 단계", phase)
    c2.metric("이번 기간 시작", kst(e["cycle_start_ts"]))
    c3.metric("누적 수정안 시도", f"{e['attempts']}회", help="시도가 많을수록 우연히 검증을 통과할 가능성이 커집니다")
    st.markdown('<p class="note">14일마다 AI 가 파라미터 수정안을 내고, 코드가 과거 데이터로 검증합니다. '
                '통과하면 14일간 나란히 운영(섀도)한 뒤 더 나은 쪽을 남깁니다.</p>', unsafe_allow_html=True)
    if not vers.empty:
        v = vers.copy()
        v["파라미터"] = v["params"].map(lambda s: ", ".join(f"{k}={x}" for k, x in json.loads(s).items()))
        v["검증"] = v["validation"].map(lambda x: (json.loads(x) or {}).get("reason", "") if x else "")
        st.dataframe(v[["version", "status", "created_at", "change_summary", "파라미터", "검증"]].rename(columns={
            "version": "버전", "status": "상태", "created_at": "만든 시각(UTC)", "change_summary": "요약"}),
            hide_index=True, width="stretch")


def tab_records(d) -> None:
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("#### 최근 거래")
        t = d["trades"]
        if t.empty:
            st.caption("아직 끝난 거래가 없습니다.")
        else:
            t = t.sort_values("exit_ts", ascending=False).head(50).copy()
            t["진입"] = t["entry_ts"].map(kst)
            t["청산"] = t["exit_ts"].map(kst)
            t["종류"] = t["exit_kind"].map(KIND_LABELS).fillna(t["exit_kind"])
            st.dataframe(t[["track", "진입", "청산", "pnl_pct", "종류"]].rename(columns={"track": "트랙"}),
                         hide_index=True, width="stretch",
                         column_config={"pnl_pct": st.column_config.NumberColumn("수익률", format="percent")})
    with c2:
        st.markdown("#### 실행 기록")
        lg = d["log"].copy()
        if lg.empty:
            st.caption("없음")
        else:
            lg["시각(KST)"] = pd.to_datetime(lg["created_at"]).dt.tz_localize("UTC").dt.tz_convert("Asia/Seoul").dt.strftime("%m-%d %H:%M")
            st.dataframe(lg[["시각(KST)", "event", "detail"]].rename(columns={"event": "종류", "detail": "내용"}),
                         hide_index=True, width="stretch", height=420)


# ---------- 본문 ----------
def main() -> None:
    st.set_page_config(page_title="BTC 위원회 · 모의매매", page_icon="📈", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    cfg = load_config()
    conn = connect_ro(cfg)
    runs = q(conn, "SELECT DISTINCT run_id FROM tracks ORDER BY run_id")["run_id"].tolist()
    if not runs:
        st.info("아직 기록이 없습니다. `python run.py live` 를 먼저 실행하세요.")
        return
    with st.sidebar:
        st.markdown("### 설정")
        run_id = st.selectbox("실행 (run_id)", runs, index=runs.index("live") if "live" in runs else 0)
        st.caption("화면은 5분마다 자동으로 새로고침됩니다.")
        if st.button("지금 새로고침", width="stretch"):
            st.rerun()
    body(cfg, run_id)


@st.fragment(run_every=300)
def body(cfg: dict, run_id: str) -> None:
    conn = connect_ro(cfg)
    defs = track_defs(cfg)
    d = load(conn, run_id)
    table = track_table(d, defs)
    eq = equity_frame(d)
    b = benchmark(conn, eq, 0.30)
    header(d, cfg, run_id)
    kpis(d, table, float(b["ret"].iloc[-1]) if not b.empty else None)
    t1, t2, t3, t4, t5 = st.tabs(["개요", "전략별", "AI 위원회", "C 진화", "기록"])
    with t1:
        tab_overview(d, conn, defs, table, eq)
    with t2:
        tab_groups(d, defs, eq)
    with t3:
        tab_council(d, cfg)
    with t4:
        tab_evolution(conn, run_id)
    with t5:
        tab_records(d)


main()
