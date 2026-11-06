"""메인 실행 파일.

사용법:
  python run.py fetch                 # 업비트 과거 캔들 수집 + 신규 봉 업데이트
  python run.py replay                # 과거 데이터로 A 트랙을 처음부터 끝까지 실행
  python run.py replay --run-id X     # 중단된 재생을 이어서 실행
  python run.py summary --run-id X    # 결과 요약
  python run.py runs                  # 실행 목록

실시간 모의매매 루프(A·B 동시)는 3단계에서 추가합니다.
"""
from __future__ import annotations

import argparse
import math
import sys

from src.backtest.metrics import summarize
from src.backtest.replay import new_run_id, replay
from src.config import db_path, load_config
from src.data.upbit import UpbitClient, find_gaps, kst_str, load_candles, since_years, sync_candles
from src.db import connect


def _fmt(k: str, v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    if k in ("기간_시작", "기간_끝"):
        return kst_str(v)
    if k in ("누적수익률", "연환산수익률", "최대낙폭", "승률", "단순보유수익률"):
        return f"{v:+.2%}" if k != "승률" else f"{v:.1%}"
    if k in ("시작자금", "최종자산"):
        return f"{v:,.0f}원"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def cmd_fetch(cfg, conn, args) -> None:
    market, unit = cfg["market"], int(cfg["timeframe_minutes"])
    since = since_years(float(cfg["data"]["history_years"]))
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
    run_id = args.run_id or new_run_id()
    tracks = args.tracks or cfg["tracks"]["enabled"]
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
        print(f"  {k:<10} {_fmt(k, v)}")
    print("  * 단순보유는 자금 100% 보유 기준, 전략은 1회 최대 30% 진입 기준입니다.")


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
    sub.add_parser("fetch", help="캔들 수집·업데이트")
    rp = sub.add_parser("replay", help="과거 데이터로 트랙 실행")
    rp.add_argument("--run-id", default=None)
    rp.add_argument("--tracks", nargs="*", default=None)
    sp = sub.add_parser("summary", help="결과 요약")
    sp.add_argument("--run-id", required=True)
    sub.add_parser("runs", help="실행 목록")
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    conn = connect(db_path(cfg))
    try:
        {"fetch": cmd_fetch, "replay": cmd_replay, "summary": cmd_summary, "runs": cmd_runs}[args.cmd](cfg, conn, args)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
