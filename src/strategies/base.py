"""규칙 전략 공통 인터페이스 (DESIGN.md 6-1).

전략은 `compute()` 하나만 구현한다. 모든 봉에 대한 신호를 벡터로 계산하며,
각 봉의 신호는 그 봉까지의 데이터만 사용한다 (미래 참조 없음).
- 백테스트: 전체 데이터에 `compute()` 를 한 번 호출해 신호 배열을 얻는다.
- 실시간/재생: `generate()` 가 최근 봉 묶음에 `compute()` 를 호출해 마지막 봉 신호만 쓴다.
두 경로가 같은 계산을 쓰므로 백테스트와 실시간 동작이 일치한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

import numpy as np
import pandas as pd

BUY, SELL, HOLD = "buy", "sell", "hold"


@dataclass(frozen=True)
class ParamRange:
    """C 트랙이 수정할 수 있는 허용 범위."""
    low: float
    high: float
    kind: type = float  # int 또는 float

    def validate(self, name: str, value: Any) -> Any:
        if self.kind is int and (not isinstance(value, (int, np.integer)) or isinstance(value, bool)):
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
    """입력: 마감된 캔들 DataFrame + 파라미터 / 출력: 신호

    - 현물 전용: sell 은 보유 포지션 청산만 뜻한다.
    - 전략은 포지션 상태를 모른다. 포지션에 따른 처리는 트랙(엔진)이 한다.
    - 신호는 "상태"가 아니라 "사건"(교차·돌파가 일어난 봉)에만 낸다.
    """

    name: ClassVar[str] = "base"
    label: ClassVar[str] = ""
    param_space: ClassVar[dict[str, ParamRange]] = {}
    defaults: ClassVar[dict[str, Any]] = {}
    grid: ClassVar[dict[str, list[Any]]] = {}       # 2단계 파라미터 탐색 격자
    reasons: ClassVar[dict[str, str]] = {}          # 신호 설명 (파라미터로 format)

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

    def compute(self, candles: pd.DataFrame) -> pd.DataFrame:
        """columns: action(buy/sell/hold), stop_loss, strength, 그리고 지표 열들."""
        raise NotImplementedError

    @staticmethod
    def _actions(buy: pd.Series, sell: pd.Series) -> np.ndarray:
        # 같은 봉에 둘 다 나오면 청산 우선 (보수적)
        return np.select([sell.fillna(False).to_numpy(bool), buy.fillna(False).to_numpy(bool)],
                         [SELL, BUY], default=HOLD)

    def generate(self, candles: pd.DataFrame) -> Signal:
        price = float(candles["close"].iloc[-1])
        if len(candles) < self.min_bars:
            return Signal(HOLD, price, reason="데이터 부족")
        df = self.compute(candles)
        r = df.iloc[-1]
        action = str(r["action"])
        ind = {k: float(r[k]) for k in df.columns if k not in ("action", "stop_loss", "strength")}
        stop = float(r["stop_loss"]) if action == BUY else None
        reason = self.reasons.get(action, "").format(**self.params)
        return Signal(action, price, float(r["strength"]), stop, reason, ind)
