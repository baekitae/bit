from src.strategies.base import Strategy
from src.strategies.donchian import DonchianBreakout
from src.strategies.ma_cross import MACross
from src.strategies.mean_reversion import BollingerReversion, RSIReversion
from src.strategies.titan import TitanRest

REGISTRY: dict[str, type[Strategy]] = {
    cls.name: cls for cls in (MACross, DonchianBreakout, RSIReversion, BollingerReversion, TitanRest)
}


def make_strategy(name: str, params: dict | None = None) -> Strategy:
    if name not in REGISTRY:
        raise ValueError(f"알 수 없는 전략: {name} (사용 가능: {sorted(REGISTRY)})")
    return REGISTRY[name](params)
