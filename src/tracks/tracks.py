"""A/B/C 트랙 정의 (DESIGN.md 1장)."""
from __future__ import annotations

from src.strategies.base import Signal
from src.tracks.base import EntryDecision, Track


class TrackA(Track):
    """규칙 전략 그대로. AI 없음."""
    name = "A"

    def decide_entry(self, signal: Signal, bar_ts: int) -> EntryDecision:
        return EntryDecision(size_multiplier=1.0, stop_loss=signal.stop_loss)


class TrackB(Track):
    """A와 같은 규칙 전략 + AI 위원회 필터. 3단계에서 구현."""
    name = "B"

    def __init__(self, *args, **kwargs):
        raise NotImplementedError("B 트랙은 3단계에서 구현합니다")


class TrackC(Track):
    """규칙 전략 + AI 위원회 필터 + 전략 진화. 4단계에서 구현."""
    name = "C"

    def __init__(self, *args, **kwargs):
        raise NotImplementedError("C 트랙은 4단계에서 구현합니다")


TRACKS: dict[str, type[Track]] = {"A": TrackA, "B": TrackB, "C": TrackC}
