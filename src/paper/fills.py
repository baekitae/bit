"""모의 체결 규칙 (DESIGN.md 6-5). 순수 함수라 백테스트·실시간이 같은 규칙을 쓴다."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Costs:
    fee_rate: float
    slippage_rate: float


@dataclass(frozen=True)
class Fill:
    ref_price: float   # 슬리피지 전 기준가
    price: float       # 체결가
    qty: float
    fee: float
    cash_delta: float  # 현금 변화 (매수 음수, 매도 양수)


def buy_fill(ref_price: float, spend: float, costs: Costs) -> Fill:
    """`spend` 원(수수료 포함)을 써서 매수. 체결가 = 기준가 × (1 + 슬리피지)."""
    if spend <= 0 or ref_price <= 0:
        raise ValueError("spend, ref_price 는 양수여야 합니다")
    price = ref_price * (1 + costs.slippage_rate)
    qty = spend / (price * (1 + costs.fee_rate))
    fee = price * qty * costs.fee_rate
    return Fill(ref_price, price, qty, fee, -(price * qty + fee))


def sell_fill(ref_price: float, qty: float, costs: Costs) -> Fill:
    """전량 매도. 체결가 = 기준가 × (1 - 슬리피지)."""
    if qty <= 0 or ref_price <= 0:
        raise ValueError("qty, ref_price 는 양수여야 합니다")
    price = ref_price * (1 - costs.slippage_rate)
    gross = price * qty
    fee = gross * costs.fee_rate
    return Fill(ref_price, price, qty, fee, gross - fee)


def stop_trigger_price(bar_open: float, bar_low: float, stop: float) -> float | None:
    """봉 안에서 손절이 걸렸는지 판정. 걸렸다면 기준가 = min(시가, 손절가) (갭 하락 반영)."""
    if bar_low <= stop:
        return min(bar_open, stop)
    return None
