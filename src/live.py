"""실시간 모의매매 루프 (DESIGN.md 3단계, 12장).

봉이 마감될 때마다:
  1. 업비트에서 새 캔들을 받는다 (실패하면 이번 회차는 건너뛰고 다음에 다시 시도)
  2. 트랙마다 새로 마감된 봉을 처리한다 (4시간봉 트랙, 일봉 트랙)
     - 마감 직후(허용 지연 안)의 봉만 정상 처리한다
     - 그보다 늦은 봉(프로그램이 꺼져 있었거나 데이터가 늦은 경우)은 소급 매매하지 않고 catch_up 한다
  3. 신호별 짝비교 결과(signal_outcomes)를 갱신한다
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Callable

import pandas as pd

from src.data.upbit import UpbitClient, kst_str, load_candles, resample, since_years, sync_candles
from src.db import log_event
from src.tracks.base import Track
from src.tracks.registry import build_tracks


def update_signal_outcomes(conn: sqlite3.Connection, run_id: str) -> int:
    """짝비교 행에 A·B 의 실제 거래 결과를 채운다 (거래가 끝난 것만)."""
    n = 0
    rows = conn.execute(
        "SELECT * FROM signal_outcomes WHERE run_id=? AND (a_trade_id IS NULL OR b_trade_id IS NULL)",
        (run_id,),
    ).fetchall()
    for r in rows:
        a = None
        if r["a_signal_id"] is not None and r["a_trade_id"] is None:
            a = conn.execute(
                """SELECT t.id, t.pnl_pct FROM trades t JOIN paper_orders o ON o.id = t.entry_order_id
                   WHERE t.run_id=? AND o.signal_id=?""", (run_id, r["a_signal_id"])).fetchone()
        b = None
        if r["b_meeting_id"] is not None and r["b_trade_id"] is None:
            b = conn.execute(
                """SELECT t.id, t.pnl_pct FROM trades t JOIN paper_orders o ON o.id = t.entry_order_id
                   JOIN council_meetings m ON m.signal_id = o.signal_id
                   WHERE t.run_id=? AND m.id=?""", (run_id, r["b_meeting_id"])).fetchone()
        if a:
            conn.execute("UPDATE signal_outcomes SET a_trade_id=?, a_pnl_pct=? WHERE id=?", (a[0], a[1], r["id"]))
            n += 1
        if b:
            conn.execute("UPDATE signal_outcomes SET b_trade_id=?, b_pnl_pct=? WHERE id=?", (b[0], b[1], r["id"]))
            n += 1
    conn.commit()
    return n


class LiveRunner:
    def __init__(self, conn: sqlite3.Connection, cfg: dict[str, Any], client: UpbitClient,
                 council: Any = None, run_id: str = "live", now: Callable[[], float] = time.time,
                 log: Callable[[str], None] = print):
        self.conn = conn
        self.cfg = cfg
        self.client = client
        self.run_id = run_id
        self.now = now
        self.log = log
        self.market = cfg["market"]
        self.base_unit = 240
        live = cfg.get("live", {})
        self.max_late_sec = int(live.get("max_late_sec", 900))
        self.window = int(cfg["strategy"]["window_bars"])
        self.boundary = int(cfg.get("day_boundary_utc_hour", 0)) * 3600
        self.max_delay_bars = float(cfg["data"]["max_candle_delay_bars"])
        self.council = council
        self.tracks: list[Track] = build_tracks(conn, cfg, run_id, council=council)
        self.evolutions = self._make_evolutions()
        units = {t.unit for t in self.tracks}
        if not units <= {240, 1440}:
            raise ValueError(f"실시간 루프는 4시간봉(240)·일봉(1440) 트랙만 지원합니다: {units}")
        log_event(conn, run_id, "live_start", ", ".join(f"{t.name}({t.strategy.name},{t.unit})" for t in self.tracks))
        conn.commit()

    def _make_evolutions(self) -> list[Any]:
        from src.evolution.manager import Evolution
        from src.tracks.registry import track_defs
        out = []
        for name, d in track_defs(self.cfg).items():
            if d["type"] == "evolving":
                c = self.council.get("default") if isinstance(self.council, dict) else self.council
                out.append(Evolution(self.conn, self.cfg, self.run_id, name, d["strategy"], d["params"], c))
        return out

    def _tick_evolution(self, now: int) -> list[str]:
        """C 트랙 진화 단계 진행. 버전이 바뀌면 트랙을 다시 만든다."""
        notes = []
        changed = False
        for ev in self.evolutions:
            try:
                changed = ev.tick(now) or changed
            except Exception as e:   # 진화 루프 오류가 매매를 멈추게 하면 안 된다
                log_event(self.conn, self.run_id, "evolution_error", f"{ev.track}: {type(e).__name__}: {e}")
                self.conn.commit()
                notes.append(f"{ev.track} 진화 루프 오류: {e}")
        if changed:
            self.tracks = build_tracks(self.conn, self.cfg, self.run_id, council=self.council)
            notes.append("C 트랙 버전 변경 → 트랙 다시 구성: " + ", ".join(t.name for t in self.tracks))
        return notes

    # ---- 데이터 ----
    def sync(self) -> str | None:
        """새 캔들 받기. 실패하면 오류 메시지."""
        now = int(self.now())
        try:
            sync_candles(self.conn, self.client, self.market, self.base_unit,
                         since_years(float(self.cfg["data"]["history_years"]), now), now_ts=now)
            return None
        except Exception as e:  # 네트워크·API 오류
            msg = f"캔들 수집 실패: {type(e).__name__}: {e}"
            log_event(self.conn, self.run_id, "data_error", msg)
            self.conn.commit()
            return msg

    def frames(self) -> dict[int, pd.DataFrame]:
        now = int(self.now())
        need_days = self.window + 30        # 일봉 window 개 + 여유
        df = load_candles(self.conn, self.market, self.base_unit, start_ts=now - need_days * 86400)
        return {240: df, 1440: resample(df, 240, 1440, self.boundary)}

    # ---- 한 회차 ----
    def step(self) -> dict[str, Any]:
        err = self.sync()
        frames = self.frames()
        now = int(self.now())
        summary: dict[str, Any] = {"error": err, "processed": [], "caught_up": [], "notes": []}
        if frames[240].empty:
            return summary
        for ev in self.evolutions:
            ev.ensure_initial(now)
        for t in self.tracks:
            frame = frames[t.unit]
            if len(frame) < 2:
                continue
            last = t.account.state.last_bar_ts
            ts = frame["ts"].to_numpy()
            bar_sec = t.unit * 60
            new_idx = [i for i in range(len(frame)) if last is None or ts[i] > last]
            if not new_idx:
                continue
            newest = new_idx[-1]
            fresh = now - (int(ts[newest]) + bar_sec) <= self.max_late_sec
            if last is None:
                # 처음 시작: 과거 봉으로 매매하지 않는다. 마감 직후가 아니면 기준점만 기록
                missed_idx = [] if fresh else [newest]
            else:
                missed_idx = new_idx[:-1] if fresh else new_idx
            if missed_idx:
                note = "첫 시작" if last is None else ("데이터 지연·프로그램 중단")
                t.catch_up(frame.iloc[missed_idx], float(frame["close"].iloc[-1]), note)
                summary["caught_up"].append((t.name, len(missed_idx)))
            if fresh:
                w = frame.iloc[max(0, newest - self.window + 1): newest + 1]
                t.on_bar(w, int(ts[newest - 1]), now_ts=now, max_delay_bars=self.max_delay_bars)
                summary["processed"].append((t.name, kst_str(int(ts[newest]))))
        update_signal_outcomes(self.conn, self.run_id)
        if summary["processed"]:
            summary["notes"] += self._tick_evolution(now)
        return summary

    # ---- 반복 ----
    def next_run_at(self, now: float) -> float:
        bar = self.base_unit * 60
        delay = float(self.cfg.get("live", {}).get("delay_after_close_sec", 15))
        return (now // bar + 1) * bar + delay

    def run_forever(self) -> None:
        self.log(f"실시간 모의매매 시작 (run_id={self.run_id}, 트랙 {len(self.tracks)}개). Ctrl+C 로 종료")
        self._report(self.step())
        target = self.next_run_at(self.now())
        while True:
            now = self.now()
            if now >= target:
                self._report(self.step())
                target = self.next_run_at(self.now())
            else:
                time.sleep(min(30.0, max(1.0, target - now)))   # 절전 복귀·시계 변경에도 30초 안에 다시 확인

    def _report(self, s: dict[str, Any]) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        if s["error"]:
            self.log(f"[{stamp}] {s['error']}")
        for name, n in s["caught_up"]:
            self.log(f"[{stamp}] {name}: 놓친 봉 {n}개 (소급 매매 없음)")
        for name, bar in s["processed"]:
            self.log(f"[{stamp}] {name}: {bar} 봉 처리")
        for n in s.get("notes", []):
            self.log(f"[{stamp}] {n}")
        if not s["processed"] and not s["caught_up"]:
            self.log(f"[{stamp}] 새로 마감된 봉 없음")
