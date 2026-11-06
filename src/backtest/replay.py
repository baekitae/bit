"""과거 데이터 재생: DB에 저장된 캔들을 한 봉씩 트랙에 넣어 끝까지 돌린다.

실시간 루프와 똑같은 `Track.on_bar` 를 쓰므로, 재생 결과가 실시간 동작을 그대로 보여준다.
같은 run_id 로 다시 실행하면 이미 처리한 봉은 건너뛰고 이어서 진행한다 (재시작 복원).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Callable

from src.data.upbit import load_candles
from src.db import log_event
from src.strategies import make_strategy
from src.tracks.base import Track
from src.tracks.tracks import TRACKS


def build_tracks(conn: sqlite3.Connection, cfg: dict[str, Any], run_id: str,
                 names: list[str]) -> list[Track]:
    s = cfg["strategy"]
    out = []
    for n in names:
        strategy = make_strategy(s["name"], s.get("params"))
        out.append(TRACKS[n](conn, run_id, cfg, strategy, s["version"]))
    return out


def new_run_id(prefix: str = "replay") -> str:
    return f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def replay(
    conn: sqlite3.Connection,
    cfg: dict[str, Any],
    run_id: str,
    track_names: list[str],
    start_ts: int | None = None,
    end_ts: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> int:
    """처리한 봉 수를 반환."""
    df = load_candles(conn, cfg["market"], int(cfg["timeframe_minutes"]), start_ts, end_ts)
    if df.empty:
        raise RuntimeError("캔들 데이터가 없습니다. 먼저 `python run.py fetch` 를 실행하세요.")
    tracks = build_tracks(conn, cfg, run_id, track_names)
    window = int(cfg["strategy"]["window_bars"])
    log_event(conn, run_id, "replay_start",
              f"tracks={track_names} bars={len(df)} from={df['kst'].iloc[0]} to={df['kst'].iloc[-1]}")
    conn.commit()

    ts_list = df["ts"].tolist()
    processed = 0
    step = max(len(df) // 10, 1)
    for i in range(len(df)):
        w = df.iloc[max(0, i - window + 1): i + 1]
        prev_ts = ts_list[i - 1] if i > 0 else None
        for t in tracks:
            if t.on_bar(w, prev_ts):
                processed += 1
        if progress and (i + 1) % step == 0:
            progress(f"  {df['kst'].iloc[i]} 까지 처리 ({i + 1}/{len(df)})")

    log_event(conn, run_id, "replay_end", f"processed={processed}")
    conn.commit()
    return processed
