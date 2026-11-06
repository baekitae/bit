"""리스크 관문 (DESIGN.md 7장). 모든 트랙 공통, AI는 이 규칙을 바꿀 수 없다.

신규 진입만 심사한다. 청산(신호 청산, 손절)은 관문과 무관하게 항상 실행된다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EntryRequest:
    price: float                    # 신호 봉 종가
    stop_loss: float | None
    size_multiplier: float          # 규칙 전략 = 1.0, AI 위원회는 0.0~1.0
    has_position: bool
    has_pending_order: bool
    equity: float
    day_start_equity: float
    decision_ts: int                # 결정 시각 (신호 봉 마감 시각, UTC epoch)
    cooldown_until: int | None
    data_ok: bool
    data_reason: str = ""
    system_error: str | None = None  # API·AI 오류 내용


@dataclass
class RiskResult:
    approved: bool
    approved_pct: float
    requested_pct: float
    reasons: list[str] = field(default_factory=list)


class RiskGate:
    def __init__(self, cfg: dict[str, Any]):
        self.max_position_pct = float(cfg["max_position_pct"])
        self.daily_loss_pct = float(cfg["daily_loss_pct"])
        self.max_consecutive_losses = int(cfg["max_consecutive_losses"])
        self.cooldown_sec = int(float(cfg["cooldown_hours"]) * 3600)

    def check_entry(self, r: EntryRequest) -> RiskResult:
        mult = min(max(float(r.size_multiplier), 0.0), 1.0)  # 비중은 줄이기만 가능
        requested = self.max_position_pct * mult
        reasons: list[str] = []

        if r.stop_loss is None or not (0 < r.stop_loss < r.price):
            reasons.append("손절가 없음 또는 비정상")
        if requested <= 0:
            reasons.append("진입 비중 0")
        if r.has_position:
            reasons.append("이미 포지션 보유 (트랙당 1개)")
        if r.has_pending_order:
            reasons.append("대기 중인 주문 있음")
        if r.day_start_equity > 0:
            day_loss = (r.day_start_equity - r.equity) / r.day_start_equity
            if day_loss >= self.daily_loss_pct:
                reasons.append(f"일일 손실 한도 도달 ({day_loss:.2%})")
        if r.cooldown_until is not None and r.decision_ts < r.cooldown_until:
            reasons.append("연속 손실로 신규 진입 중단 중")
        if not r.data_ok:
            reasons.append(f"데이터 이상: {r.data_reason}")
        if r.system_error:
            reasons.append(f"시스템 오류: {r.system_error}")

        approved = not reasons
        return RiskResult(approved, requested if approved else 0.0, requested, reasons)

    def after_trade(self, loss_streak: int, pnl: float, exit_ts: int) -> tuple[int, int | None]:
        """거래 종료 후 연속 손실 상태 갱신. (새 연속손실 횟수, 새 cooldown_until 또는 None)"""
        if pnl < 0:
            loss_streak += 1
            if loss_streak >= self.max_consecutive_losses:
                return 0, exit_ts + self.cooldown_sec
            return loss_streak, None
        return 0, None


def check_bar(
    bar: dict[str, Any],
    prev_ts: int | None,
    unit: int,
    now_ts: int | None = None,
    max_delay_bars: float | None = None,
) -> tuple[bool, str]:
    """캔들 이상 여부 판정. (정상 여부, 이유)"""
    o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
    if min(o, h, l, c) <= 0:
        return False, "가격이 0 이하"
    if not (l <= min(o, c) and h >= max(o, c) and l <= h):
        return False, "고가·저가가 시가·종가 범위와 맞지 않음"
    if prev_ts is not None and bar["ts"] - prev_ts != unit * 60:
        return False, "직전 봉과 연속되지 않음 (캔들 누락)"
    if now_ts is not None and max_delay_bars is not None:
        close_ts = bar["ts"] + unit * 60
        if now_ts - close_ts > max_delay_bars * unit * 60:
            return False, "최신 캔들이 늦음"
    return True, ""
