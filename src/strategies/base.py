"""규칙 전략 공통 인터페이스 (DESIGN.md 6-1)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

import pandas as pd

BUY, SELL, HOLD = "buy", "sell", "hold"


@dataclass(frozen=True)
class ParamRange:
    """C 트랙이 수정할 수 있는 허용 범위."""
    low: float
    high: float
    kind: type = float  # int 또는 float

    def validate(self, name: str, value: Any) -> Any:
        if self.kind is int and (not isinstance(value, int) or isinstance(value, bool)):
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            else:
                raise ValueError(f"{name}={value!r}: 정수여야 합니다")
        if not (self.low <= value <= self.high):
            raise ValueError(f"{name}={value!r}: 허용 범위 [{self.low}, {self.high}] 밖입니다")
        return self.kind(value)


@dataclass
class Signal:
    action: str                      # buy / sell / hold
    price: float                     # 신호 봉 종가
    strength: float = 0.0
    stop_loss: float | None = None   # buy 일 때 필수
    reason: str = ""
    indicators: dict[str, float] = field(default_factory=dict)


class Strategy:
    """입력: 마감된 캔들 DataFrame(마지막 행 = 방금 마감된 봉) + 파라미터
    출력: Signal

    - 현물 전용: sell 은 보유 포지션 청산만 뜻한다.
    - 전략은 포지션 상태를 모른다. 포지션에 따른 처리는 트랙이 한다.
    """

    name: ClassVar[str] = "base"
    param_space: ClassVar[dict[str, ParamRange]] = {}
    defaults: ClassVar[dict[str, Any]] = {}

    def __init__(self, params: dict[str, Any] | None = None):
        merged = {**self.defaults, **(params or {})}
        unknown = set(merged) - set(self.param_space)
        if unknown:
            raise ValueError(f"{self.name}: 알 수 없는 파라미터 {sorted(unknown)}")
        self.params = {k: self.param_space[k].validate(k, v) for k, v in merged.items()}
        self.check_params()

    def check_params(self) -> None:
        """파라미터 사이의 관계 검사 (필요한 전략만 재정의)."""

    @property
    def min_bars(self) -> int:
        raise NotImplementedError

    def generate(self, candles: pd.DataFrame) -> Signal:
        raise NotImplementedError
