"""메인 실행 파일.

사용법:
  python run.py fetch                 # 업비트 과거 캔들 수집 + 신규 봉 업데이트
  python run.py fetch --unit 60       # 1시간봉 수집
  python run.py backtest              # 2단계: 후보 전략 4개 비교 리포트 (reports/ 폴더)
  python run.py backtest --unit 60    # 1시간봉으로 비교
  python run.py replay                # 과거 데이터로 A 트랙을 처음부터 끝까지 실행
  python run.py replay --run-id X     # 중단된 재생을 이어서 실행
  python run.py summary --run-id X    # 결과 요약
  python run.py runs                  # 실행 목록

  python run.py live                  # 3단계: 실시간 모의매매 (A1~A5, B1~B4)
  python run.py council-test          # 실제 AI 로 위원회 1회 시험 (매매 없음)
  streamlit run src/report/dashboard.py   # 대시보드
"""
from __future__ import annotations

import argparse
import math
import sys

from src.backtest.metrics import LABELS, PCT_KEYS, summarize
from src.backtest.replay import new_run_id, replay
from src.config import db_path, load_config
from src.data.upbit import UpbitClient, find_gaps, kst_str, load_candles, resample, since_years, sync_candles
from src.db import connect


def _fmt(k: str, v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    if k in ("start_ts", "end_ts"):
        return kst_str(v)
    if k in PCT_KEYS:
        return f"{v:+.2%}" if k not in ("win_rate", "exposure") else f"{v:.1%}"
    if k in ("initial", "final"):
        return f"{v:,.0f}원"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def cmd_fetch(cfg, conn, args) -> None:
    market, unit = cfg["market"], int(args.unit or cfg["timeframe_minutes"])
    since = since_years(float(args.years or cfg["data"]["history_years"]))
    print(f"{market} {unit}분봉 수집 (시작: {kst_str(since)})")
    client = UpbitClient(interval_sec=float(cfg["data"]["request_interval_sec"]))
    n = sync_candles(conn, client, market, unit, since, progress=print)
    df = load_candles(conn, market, unit)
    print(f"새로 저장/갱신: {n}개, DB 전체: {len(df)}개 ({df['kst'].iloc[0]} ~ {df['kst'].iloc[-1]} KST)")
    gaps = find_gaps(df, unit)
    if gaps:
        print(f"주의: 연속되지 않는 구간 {len(gaps)}곳 (업비트 측 누락). 해당 봉에서는 신규 진입이 차단됩니다.")
        for a, b in gaps[:10]:
            print(f"  {kst_str(a)} -> {kst_str(b)}")


def cmd_replay(cfg, conn, args) -> None:
    from src.tracks.registry import track_defs
    run_id = args.run_id or new_run_id()
    base = int(cfg["timeframe_minutes"])
    tracks = args.tracks or [n for n, d in track_defs(cfg).items()
                             if d["type"] == "rule" and int(d["timeframe"]) == base]
    print(f"재생 시작: run_id={run_id}, 트랙={tracks}")
    n = replay(conn, cfg, run_id, tracks, progress=print)
    print(f"처리한 봉: {n}")
    for t in tracks:
        print_summary(cfg, conn, run_id, t)


def print_summary(cfg, conn, run_id: str, track: str) -> None:
    s = summarize(conn, run_id, track, int(cfg["timeframe_minutes"]))
    print(f"\n[{run_id}] 트랙 {track}")
    if not s:
        print("  기록 없음")
        return
    for k, v in s.items():
        print(f"  {LABELS.get(k, k):<12} {_fmt(k, v)}")
    print("  * 단순보유는 자금 100% 보유 기준, 전략은 1회 최대 30% 진입 기준입니다.")


def cmd_backtest(cfg, conn, args) -> None:
    from pathlib import Path

    from src.backtest.engine import Market
    from src.backtest.study import Study
    from src.config import ROOT
    from src.report.stage2 import write
    from src.strategies import REGISTRY

    unit = int(args.unit or cfg["timeframe_minutes"])
    years = float(args.years or cfg["data"]["history_years"])
    boundary = int(cfg.get("day_boundary_utc_hour", 0)) * 3600
    src_unit = 240 if unit == 1440 else unit           # 일봉은 4시간봉을 합쳐서 만든다
    df = load_candles(conn, cfg["market"], src_unit)
    if df.empty:
        print(f"{src_unit}분봉 데이터가 없습니다. 먼저 `python run.py fetch --unit {src_unit}` 를 실행하세요.")
        return
    if unit == 1440:
        df = resample(df, 240, 1440, boundary)
    df = df[df["ts"] >= since_years(years, int(df["ts"].iloc[-1]))].reset_index(drop=True)
    names = args.strategies or cfg["backtest"]["strategies"]
    print(f"{cfg['market']} {unit}분봉 {len(df):,}개로 전략 {len(names)}개 비교")
    market = Market(df, unit, boundary)
    study = Study(market, cfg)
    fixed = cfg["backtest"].get("fixed_params", {})
    reports = [study.evaluate(REGISTRY[n], progress=print,
                              fixed=fixed[n] if args.fixed and n in fixed else None) for n in names]
    path = write(study, reports, cfg, ROOT / "reports", args.tag)
    print(f"\n리포트: {path}")
    for r in reports:
        print(f"  {r.label:<14} {'통과' if r.passed else '탈락'}  " + "; ".join(f"{f.level}:{f.rule}" for f in r.flags))


def cmd_infinite(cfg, conn, args) -> None:
    """무한매수법 시뮬레이션 (일봉). 리스크 관문 밖의 별도 시뮬레이터."""
    from src.backtest.infinite import InfiniteParams, run_infinite
    from src.config import ROOT
    from src.paper.fills import Costs
    from src.report.infinite import write as write_inf

    boundary = int(cfg.get("day_boundary_utc_hour", 0)) * 3600
    df = resample(load_candles(conn, cfg["market"], 240), 240, 1440, boundary)
    costs = Costs(float(cfg["costs"]["fee_rate"]), float(cfg["costs"]["slippage_rate"]))
    p = InfiniteParams(**cfg.get("infinite", {}))
    initial = float(cfg["tracks"]["initial_cash"])
    runs = {}
    for years in args.years:
        d = df[df["ts"] >= since_years(years, int(df["ts"].iloc[-1]))].reset_index(drop=True)
        runs[years] = (d, run_infinite(d, p, costs, initial))
    path = write_inf(runs, p, costs, cfg, ROOT / "reports")
    print(f"리포트: {path}")
    for years, (d, r) in runs.items():
        m = r.metrics
        print(f"  최근 {years:g}년: 수익 {m['total_return']:+.1%}, MDD {m['mdd']:+.1%}, 익절 사이클 {m['trades']}회, "
              f"단순보유 {m['bh_return']:+.1%} (MDD {m['bh_mdd']:+.1%})")


def make_council(cfg):
    """{위원회 이름: Council}. default = Claude (config council), 그 외는 config councils.<이름>."""
    from src.council.council import Council
    out = {"default": Council(cfg)}
    for name, settings in (cfg.get("councils") or {}).items():
        out[name] = Council(cfg, settings=settings, name=name)
    return out


def cmd_live(cfg, conn, args) -> None:
    """실시간 모의매매 (A·B 트랙 동시)."""
    import os

    from src.live import LiveRunner
    from src.tracks.registry import track_defs

    defs = track_defs(cfg).values()
    needs_ai = any(d["type"] in ("council", "evolving") for d in defs)
    if needs_ai and not os.environ.get("ANTHROPIC_API_KEY"):
        print("주의: ANTHROPIC_API_KEY 가 없습니다 (.env 확인). B·C 트랙 위원회는 오류로 처리되어 모두 skip 됩니다.")
    if any(d.get("council") == "gemini" for d in defs) and not os.environ.get("GEMINI_API_KEY"):
        print("주의: GEMINI_API_KEY 가 없습니다 (.env 확인). G 트랙(Gemini 비교) 위원회는 모두 skip 됩니다.")
    client = UpbitClient(interval_sec=float(cfg["data"]["request_interval_sec"]))
    runner = LiveRunner(conn, cfg, client, council=make_council(cfg) if needs_ai else None, run_id=args.run_id)
    if args.once:
        runner._report(runner.step())
        return
    try:
        runner.run_forever()
    except KeyboardInterrupt:
        print("종료합니다. 다시 실행하면 DB에서 상태를 복원해 이어서 진행합니다.")


def cmd_council_test(cfg, conn, args) -> None:
    """실제 AI 로 위원회 1회 시험 (과거 마지막 진입 신호 기준, 매매 없음). 비용이 조금 듭니다."""
    import json

    from src.council.context import build_context
    from src.strategies.base import BUY
    from src.tracks.registry import build_tracks, track_defs

    name = args.track
    d = track_defs(cfg)[name]
    if d["type"] not in ("council", "evolving"):
        print(f"{name} 은 위원회 트랙이 아닙니다")
        return
    run_id = "council-test"
    track = build_tracks(conn, cfg, run_id, [name], council=make_council(cfg))[0]
    if d.get("council") == "gemini":
        print("Gemini 위원회로 시험합니다 (무료 한도 중 4회 사용)")
    df = load_candles(conn, cfg["market"], 240)
    if track.unit == 1440:
        df = resample(df, 240, 1440, int(cfg.get("day_boundary_utc_hour", 0)) * 3600)
    sig_df = track.strategy.compute(df)
    idx = [i for i in range(len(df)) if sig_df["action"].iloc[i] == BUY]
    if not idx:
        print("과거 데이터에 진입 신호가 없습니다")
        return
    i = idx[-1]
    window = int(cfg["strategy"]["window_bars"])
    track.window = df.iloc[max(0, i - window + 1): i + 1]
    sig = track.strategy.generate(track.window)
    ts = int(df["ts"].iloc[i])
    print(f"{name} ({track.strategy.label}) 의 마지막 진입 신호: {kst_str(ts)} 봉, 종가 {sig.price:,.0f}원")
    print("위원회 소집 중... (역할 4개, 수십 초~몇 분)")
    sid = track.account.record_signal(track.strategy.name, track.strategy_version, ts, sig)
    ctx = build_context(track, sig, ts, cfg)
    dec = track.council.decide(conn, run_id, name, sid, ts, ctx, sig.stop_loss, sig.price)
    conn.commit()
    row = conn.execute("SELECT * FROM council_meetings WHERE id=?", (dec.meeting_id,)).fetchone()
    if dec.error:
        print(f"실패 → skip 처리: {dec.error}")
        return
    for role, v in json.loads(row["transcript"]).items():
        print(f"\n[{role}]\n{json.dumps(v, ensure_ascii=False, indent=2)}")
    print(f"\n최종: {dec.decision} (비중 x{dec.size_multiplier:.2f}, 손절가 {dec.stop_loss:,.0f}, 확신 {dec.confidence:.2f})")
    for n in dec.notes:
        print(f"  - {n}")
    print(f"토큰: 입력 {row['input_tokens']:,} / 출력 {row['output_tokens']:,}, 비용 약 {row['cost_krw']:,.0f}원, "
          f"소요 {row['latency_sec']:.0f}초")


def cmd_evolution(cfg, conn, args) -> None:
    """C 트랙 버전 이력 보기 / 되돌리기."""
    import json
    import time

    from src.evolution.manager import Evolution
    from src.tracks.registry import track_defs

    name = args.track
    d = track_defs(cfg)[name]
    rows = conn.execute("SELECT * FROM strategy_versions WHERE run_id=? AND track=? ORDER BY id",
                        (args.run_id, name)).fetchall()
    if args.rollback:
        ev = Evolution(conn, cfg, args.run_id, name, d["strategy"], d["params"], make_council(cfg)["default"])
        ev.rollback(args.rollback, int(time.time()))
        print(f"{args.rollback} 파라미터로 되돌렸습니다. 실행 중인 live 는 다시 시작해야 반영됩니다.")
        return
    st = conn.execute("SELECT * FROM evolution_state WHERE run_id=? AND track=?", (args.run_id, name)).fetchone()
    if st:
        print(f"{name}: 단계 {st['phase']}, 기간 시작 {kst_str(st['cycle_start_ts'])}, 누적 수정안 시도 {st['attempts']}회")
    for r in rows:
        print(f"  {r['version']:<4} {r['status']:<9} {r['created_at']}  {r['change_summary'] or ''}")
        print(f"       {json.loads(r['params'])}")


def cmd_summary(cfg, conn, args) -> None:
    rows = conn.execute("SELECT track FROM tracks WHERE run_id=?", (args.run_id,)).fetchall()
    if not rows:
        print(f"run_id={args.run_id} 기록이 없습니다.")
        return
    for r in rows:
        print_summary(cfg, conn, args.run_id, r["track"])


def cmd_runs(cfg, conn, args) -> None:
    rows = conn.execute(
        "SELECT run_id, track, strategy, strategy_version, last_equity, updated_at FROM tracks ORDER BY updated_at"
    ).fetchall()
    for r in rows:
        eq = f"{r['last_equity']:,.0f}원" if r["last_equity"] is not None else "-"
        print(f"{r['run_id']:<28} {r['track']}  {r['strategy']}/{r['strategy_version']}  {eq}  ({r['updated_at']} UTC)")


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔 한글 출력
    p = argparse.ArgumentParser(description="BTC AI 위원회 모의매매 봇")
    p.add_argument("--config", default=None, help="설정 파일 경로 (기본: config.yaml)")
    sub = p.add_subparsers(dest="cmd", required=True)
    fp = sub.add_parser("fetch", help="캔들 수집·업데이트")
    fp.add_argument("--unit", type=int, default=None, help="봉 주기(분). 기본: config")
    fp.add_argument("--years", type=float, default=None, help="수집 기간(년). 기본: config")
    bp = sub.add_parser("backtest", help="2단계 전략 비교 리포트")
    bp.add_argument("--unit", type=int, default=None, help="봉 주기(분). 기본: config")
    bp.add_argument("--strategies", nargs="*", default=None)
    bp.add_argument("--fixed", action="store_true", help="config backtest.fixed_params 의 값 그대로 검증 (탐색 안 함)")
    bp.add_argument("--tag", default=None, help="리포트 파일 이름 꼬리표")
    bp.add_argument("--years", type=float, default=None, help="최근 몇 년으로 검증할지. 기본: config history_years")
    rp = sub.add_parser("replay", help="과거 데이터로 트랙 실행")
    rp.add_argument("--run-id", default=None)
    rp.add_argument("--tracks", nargs="*", default=None)
    lp = sub.add_parser("live", help="실시간 모의매매 (A·B 트랙)")
    lp.add_argument("--run-id", default="live")
    lp.add_argument("--once", action="store_true", help="한 회차만 실행하고 종료")
    cp = sub.add_parser("council-test", help="실제 AI 로 위원회 1회 시험 (매매 없음)")
    cp.add_argument("--track", default="B1", help="B1 (Claude) 또는 G1 (Gemini) 등")
    ep = sub.add_parser("evolution", help="C 트랙 버전 이력 / 되돌리기")
    ep.add_argument("--track", default="C")
    ep.add_argument("--run-id", default="live")
    ep.add_argument("--rollback", default=None, help="이 버전의 파라미터로 되돌림 (예: v1)")
    ip = sub.add_parser("infinite", help="무한매수법 시뮬레이션 (일봉)")
    ip.add_argument("--years", type=float, nargs="*", default=[4, 10])
    sp = sub.add_parser("summary", help="결과 요약")
    sp.add_argument("--run-id", required=True)
    sub.add_parser("runs", help="실행 목록")
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    conn = connect(db_path(cfg))
    try:
        {"fetch": cmd_fetch, "backtest": cmd_backtest, "infinite": cmd_infinite, "live": cmd_live,
         "council-test": cmd_council_test, "evolution": cmd_evolution, "replay": cmd_replay, "summary": cmd_summary, "runs": cmd_runs}[args.cmd](cfg, conn, args)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
