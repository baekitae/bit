from src.strategies.base import Strategy
from src.strategies.ma_cross import MACross

REGISTRY: dict[str, type[Strategy]] = {
    MACross.name: MACross,
}


def make_strategy(name: str, params: dict | None = None) -> Strategy:
    if name not in REGISTRY:
        raise ValueError(f"알 수 없는 전략: {name} (사용 가능: {sorted(REGISTRY)})")
    return REGISTRY[name](params)
