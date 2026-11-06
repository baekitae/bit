"""C 트랙 전략 진화 루프 (DESIGN.md 9장).

상태 (evolution_state 테이블):
  fixed   현재 버전을 고정 운영 중. fixed_days 가 지나면 → 수정안 제안·검증
          - 검증 통과 → 새 버전을 섀도로 시작 (phase=shadow)
          - 유지(keep)·검증 탈락·AI 오류 → 기록만 하고 새 고정 기간 시작
  shadow  현재 버전과 섀도 버전을 나란히 운영 중.
          - shadow_days 가 지나고 두 버전의 진입 신호 합계 ≥ min_shadow_signals → 수익률 비교로 교체/폐기
          - 신호가 모자라면 max_shadow_days 까지 연장, 그래도 모자라면 판정 불가로 폐기

버전 (strategy_versions): active / shadow / retired / rejected / no_change
모든 시각은 봉 마감 시각(UTC epoch)을 쓴다.
"""
from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

from src.council.llm import LLMError
from src.db import log_event
from src.evolution import proposer
from src.evolution.validate import validate_proposal
from src.strategies import REGISTRY

DAY = 86400


def _clean(o: Any) -> Any:
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, float) and math.isnan(o):
        return None
    return o


# ---- 버전 조회 (registry 에서도 쓴다) ----
def get_version(conn: sqlite3.Connection, run_id: str, track: str, status: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM strategy_versions WHERE run_id=? AND track=? AND status=? ORDER BY id DESC LIMIT 1",
        (run_id, track, status)).fetchone()


def shadow_track_name(track: str, version: str) -> str:
    return f"{track}_{version}"


class Evolution:
    def __init__(self, conn: sqlite3.Connection, cfg: dict[str, Any], run_id: str, track: str,
                 strategy: str, base_params: dict[str, Any], council: Any):
        self.conn = conn
        self.cfg = cfg
        self.ev = cfg["evolution"]
        self.run_id = run_id
        self.track = track
        self.strategy = strategy
        self.base_params = base_params
        self.council = council

    # ---- 상태 ----
    def state(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM evolution_state WHERE run_id=? AND track=?",
                                 (self.run_id, self.track)).fetchone()

    def _set_state(self, **kw: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in kw)
        self.conn.execute(f"UPDATE evolution_state SET {cols} WHERE run_id=? AND track=?",
                          (*kw.values(), self.run_id, self.track))

    def active(self) -> sqlite3.Row:
        return get_version(self.conn, self.run_id, self.track, "active")

    def shadow(self) -> sqlite3.Row | None:
        return get_version(self.conn, self.run_id, self.track, "shadow")

    def _next_version(self) -> str:
        n = self.conn.execute("SELECT COUNT(*) FROM strategy_versions WHERE run_id=? AND track=?",
                              (self.run_id, self.track)).fetchone()[0]
        return f"v{n + 1}"

    def _add_version(self, status: str, params: dict[str, Any], parent: str | None, summary: str,
                     reason: str, validation: dict[str, Any] | None) -> str:
        v = self._next_version()
        self.conn.execute(
            """INSERT INTO strategy_versions (run_id, track, version, parent_version, strategy, params, status,
                                              change_summary, change_reason, validation)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (self.run_id, self.track, v, parent, self.strategy, json.dumps(params), status, summary, reason,
             json.dumps(_clean(validation), ensure_ascii=False) if validation is not None else None))
        return v

    def ensure_initial(self, now_ts: int) -> None:
        if self.state() is not None:
            return
        self._add_version("active", self.base_params, None, "초기 버전", "짝 A 트랙과 같은 파라미터로 시작", None)
        self.conn.execute(
            "INSERT INTO evolution_state (run_id, track, phase, cycle_start_ts, attempts) VALUES (?, ?, 'fixed', ?, 0)",
            (self.run_id, self.track, now_ts))
        log_event(self.conn, self.run_id, "comparison_start", f"{self.track} 시작 → 6개월 비교 기준일")
        self.conn.commit()

    # ---- 한 번 확인 ----
    def tick(self, now_ts: int) -> bool:
        """봉 처리 뒤 호출. 트랙 구성(활성·섀도 버전)이 바뀌면 True."""
        self.ensure_initial(now_ts)
        st = self.state()
        try:
            if st["phase"] == "fixed" and now_ts - st["cycle_start_ts"] >= int(self.ev["fixed_days"]) * DAY:
                changed = self._propose_and_validate(now_ts)
            elif st["phase"] == "shadow":
                changed = self._judge_shadow(now_ts)
            else:
                return False
            self.conn.commit()
            return changed
        except Exception:
            self.conn.rollback()
            raise

    def _history(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT version, status, params, change_summary FROM strategy_versions WHERE run_id=? AND track=? ORDER BY id",
            (self.run_id, self.track)).fetchall()
        return [{"버전": r["version"], "상태": r["status"], "파라미터": json.loads(r["params"]),
                 "요약": r["change_summary"]} for r in rows]

    def _propose_and_validate(self, now_ts: int) -> bool:
        st = self.state()
        act = self.active()
        cur = json.loads(act["params"])
        attempts = st["attempts"] + 1
        # 현재 파라미터의 과거 검증 수치 (AI 입력용, 코드 계산)
        base_check = validate_proposal(self.conn, self.cfg, self.strategy, cur, cur, now_ts)
        review = proposer.build_review_input(self.conn, self.run_id, self.track, self.strategy, cur,
                                             st["cycle_start_ts"], base_check.get("current", {}), self._history())
        s = self.council.role_setting("evolution")
        try:
            data, resp = proposer.propose(self.council.client(s["provider"]), s["model"], s["effort"],
                                          self.council.timeout, self.strategy, review)
            llm = {"provider": resp.provider, "model": resp.model, "input_tokens": resp.input_tokens,
                   "output_tokens": resp.output_tokens, "cost_krw": self.council.cost_krw(resp),
                   "prompt_version": proposer.PROMPT_VERSION}
        except LLMError as e:
            self._add_version("rejected", cur, act["version"], "AI 제안 실패", str(e), {"attempt": attempts})
            return self._new_cycle(now_ts, attempts, f"제안 실패: {e}")

        new = {**cur, **data["params"]}
        if data["action"] == "keep" or new == cur:
            self._add_version("no_change", cur, act["version"], "유지", data.get("reason", ""),
                              {"attempt": attempts, "llm": llm})
            return self._new_cycle(now_ts, attempts, "AI 가 유지를 제안")
        try:
            REGISTRY[self.strategy](new)          # 허용 범위·관계 검사
        except ValueError as e:
            self._add_version("rejected", new, act["version"], data.get("change_summary", ""),
                              data.get("reason", ""), {"attempt": attempts, "llm": llm, "reason": f"허용 범위 위반: {e}"})
            return self._new_cycle(now_ts, attempts, f"허용 범위 위반: {e}")

        result = validate_proposal(self.conn, self.cfg, self.strategy, cur, new, now_ts)
        result.update(attempt=attempts, llm=llm)
        if not result["passed"]:
            self._add_version("rejected", new, act["version"], data.get("change_summary", ""),
                              data.get("reason", ""), result)
            return self._new_cycle(now_ts, attempts, f"검증 탈락: {result['reason']}")
        v = self._add_version("shadow", new, act["version"], data.get("change_summary", ""), data.get("reason", ""), result)
        self._set_state(phase="shadow", shadow_version=v, shadow_start_ts=now_ts, attempts=attempts)
        log_event(self.conn, self.run_id, "evolution", f"{self.track}: {v} 섀도 운영 시작 ({result['reason']})")
        return True

    def _new_cycle(self, now_ts: int, attempts: int, note: str) -> bool:
        self._set_state(phase="fixed", cycle_start_ts=now_ts, attempts=attempts, shadow_version=None,
                        shadow_start_ts=None)
        log_event(self.conn, self.run_id, "evolution", f"{self.track}: {note} → 새 고정 기간 시작")
        return False

    def _period_return(self, track: str, since_ts: int) -> float | None:
        rows = self.conn.execute(
            "SELECT bar_ts, equity FROM equity WHERE run_id=? AND track=? ORDER BY bar_ts", (self.run_id, track)).fetchall()
        before = [r["equity"] for r in rows if r["bar_ts"] < since_ts]
        after = [r["equity"] for r in rows if r["bar_ts"] >= since_ts]
        if not after:
            return None
        start = before[-1] if before else float(self.cfg["tracks"]["initial_cash"])
        return after[-1] / start - 1

    def _judge_shadow(self, now_ts: int) -> bool:
        st = self.state()
        sh = self.shadow()
        start = st["shadow_start_ts"]
        elapsed = now_ts - start
        sh_name = shadow_track_name(self.track, sh["version"])
        signals = self.conn.execute(
            "SELECT COUNT(*) FROM signals WHERE run_id=? AND track IN (?, ?) AND action='buy' AND bar_ts >= ?",
            (self.run_id, self.track, sh_name, start)).fetchone()[0]
        enough = signals >= int(self.ev["min_shadow_signals"])
        if elapsed < int(self.ev["shadow_days"]) * DAY or (not enough and elapsed < int(self.ev["max_shadow_days"]) * DAY):
            return False
        val = json.loads(sh["validation"] or "{}")
        if not enough:
            val["shadow"] = {"signals": signals, "result": "판정 불가 (신호 부족)"}
            self._set_version_status(sh["version"], "retired", val)
            return self._end_shadow(now_ts, f"{sh['version']} 폐기: 섀도 기간 신호 {signals}건으로 판정 불가")
        cur_ret = self._period_return(self.track, start) or 0.0
        sh_ret = self._period_return(sh_name, start) or 0.0
        val["shadow"] = {"signals": signals, "current_return": cur_ret, "shadow_return": sh_ret,
                         "days": round(elapsed / DAY, 1)}
        if sh_ret > cur_ret:
            old = self.active()
            self._set_version_status(old["version"], "retired")
            val["shadow"]["result"] = "교체"
            self._set_version_status(sh["version"], "active", val)
            self.conn.execute("UPDATE tracks SET strategy_version=? WHERE run_id=? AND track=?",
                              (sh["version"], self.run_id, self.track))
            return self._end_shadow(now_ts, f"{sh['version']} 로 교체 (섀도 {sh_ret:+.2%} > 현재 {cur_ret:+.2%})")
        val["shadow"]["result"] = "폐기"
        self._set_version_status(sh["version"], "retired", val)
        return self._end_shadow(now_ts, f"{sh['version']} 폐기 (섀도 {sh_ret:+.2%} ≤ 현재 {cur_ret:+.2%})")

    def _set_version_status(self, version: str, status: str, validation: dict[str, Any] | None = None) -> None:
        if validation is None:
            self.conn.execute("UPDATE strategy_versions SET status=? WHERE run_id=? AND track=? AND version=?",
                              (status, self.run_id, self.track, version))
        else:
            self.conn.execute(
                "UPDATE strategy_versions SET status=?, validation=? WHERE run_id=? AND track=? AND version=?",
                (status, json.dumps(_clean(validation), ensure_ascii=False), self.run_id, self.track, version))

    def _end_shadow(self, now_ts: int, note: str) -> bool:
        st = self.state()
        self._set_state(phase="fixed", cycle_start_ts=now_ts, shadow_version=None, shadow_start_ts=None,
                        attempts=st["attempts"])
        log_event(self.conn, self.run_id, "evolution", f"{self.track}: {note}")
        return True

    # ---- 되돌리기 ----
    def rollback(self, version: str, now_ts: int) -> None:
        row = self.conn.execute("SELECT * FROM strategy_versions WHERE run_id=? AND track=? AND version=?",
                                (self.run_id, self.track, version)).fetchone()
        if row is None:
            raise ValueError(f"{self.track} 에 {version} 버전이 없습니다")
        params = json.loads(row["params"])
        REGISTRY[self.strategy](params)
        act = self.active()
        if act is not None:
            self._set_version_status(act["version"], "retired")
        sh = self.shadow()
        if sh is not None:
            self._set_version_status(sh["version"], "retired")
        v = self._add_version("active", params, act["version"] if act else None,
                              f"{version} 로 되돌림", "사용자 요청", None)
        self.conn.execute("UPDATE tracks SET strategy_version=? WHERE run_id=? AND track=?", (v, self.run_id, self.track))
        self._new_cycle(now_ts, self.state()["attempts"] if self.state() else 0, f"{version} 파라미터로 되돌림 ({v})")
        self.conn.commit()
