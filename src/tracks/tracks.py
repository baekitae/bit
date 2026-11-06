"""트랙 종류 (DESIGN.md 1장).

- RuleTrack (A 트랙): 규칙 전략 신호를 그대로 따른다. AI 없음.
- CouncilTrack (B 트랙): 같은 규칙 전략 + AI 위원회 필터. 짝이 되는 A 트랙(mirror)과 신호별로 비교 기록한다.
- C 트랙(전략 진화)은 4단계에서 추가한다.
"""
from __future__ import annotations

from typing import Any

from src.strategies.base import Signal
from src.tracks.base import EntryDecision, Track


class RuleTrack(Track):
    name = "A"
    kind = "rule"

    def decide_entry(self, signal: Signal, bar_ts: int, signal_id: int) -> EntryDecision:
        return EntryDecision(size_multiplier=1.0, stop_loss=signal.stop_loss)


class CouncilTrack(Track):
    name = "B"
    kind = "council"

    def __init__(self, *args: Any, council: Any, mirror: str | None, cfg: dict[str, Any], **kwargs: Any):
        super().__init__(*args, cfg=cfg, **kwargs)
        self.council = council
        self.mirror = mirror
        self.cfg = cfg

    def decide_entry(self, signal: Signal, bar_ts: int, signal_id: int) -> EntryDecision:
        from src.council.context import build_context

        context = build_context(self, signal, bar_ts, self.cfg)
        d = self.council.decide(self.conn, self.run_id, self.name, signal_id, bar_ts, context,
                                signal.stop_loss, signal.price)
        self._record_pair(bar_ts, d)
        note = f"위원회 #{d.meeting_id}: {d.decision}"
        return EntryDecision(d.size_multiplier, d.stop_loss, error=d.error, note=note)

    def _record_pair(self, bar_ts: int, d: Any) -> None:
        """짝 A 트랙의 같은 봉 신호와 이 결정을 묶어 둔다 (DESIGN.md 1-1 신호 단위 짝비교)."""
        if not self.mirror:
            return
        row = self.conn.execute(
            "SELECT id FROM signals WHERE run_id=? AND track=? AND bar_ts=? AND action='buy'",
            (self.run_id, self.mirror, bar_ts),
        ).fetchone()
        self.conn.execute(
            """INSERT INTO signal_outcomes (run_id, a_track, a_signal_id, b_track, b_meeting_id,
                                            b_decision, b_size_multiplier)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (self.run_id, self.mirror, row["id"] if row else None, self.name, d.meeting_id,
             "error" if d.error else d.decision, d.size_multiplier),
        )


# 이전 이름 호환 (1·2단계 코드와 테스트)
TrackA = RuleTrack


class TrackB(CouncilTrack):
    pass


class TrackC(Track):
    """규칙 전략 + AI 위원회 필터 + 전략 진화. 4단계에서 구현."""
    name = "C"

    def __init__(self, *args: Any, **kwargs: Any):
        raise NotImplementedError("C 트랙은 4단계에서 구현합니다")
