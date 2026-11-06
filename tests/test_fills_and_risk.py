import pytest

from src.paper.fills import Costs, buy_fill, sell_fill, stop_trigger_price
from src.risk.gate import EntryRequest, RiskGate, check_bar
from tests.conftest import BASE_CFG, T0, UNIT

COSTS = Costs(0.0005, 0.0005)


def test_buy_fill_costs():
    f = buy_fill(100.0, 1000.0, COSTS)
    assert f.price == pytest.approx(100.05)
    assert -f.cash_delta == pytest.approx(1000.0)  # 수수료 포함 정확히 spend 만큼 사용
    assert f.fee == pytest.approx(f.price * f.qty * 0.0005)


def test_round_trip_loses_costs():
    b = buy_fill(100.0, 1000.0, COSTS)
    s = sell_fill(100.0, b.qty, COSTS)
    assert s.price == pytest.approx(99.95)
    loss = 1000.0 - s.cash_delta
    assert loss == pytest.approx(1000 * 0.002, rel=0.02)  # 수수료·슬리피지 왕복 약 0.2%


def test_stop_trigger():
    assert stop_trigger_price(100, 96, 95) is None
    assert stop_trigger_price(100, 94, 95) == 95          # 봉 중간에 손절가 도달
    assert stop_trigger_price(90, 85, 95) == 90           # 갭 하락: 시가로 체결


def req(**kw):
    base = dict(price=100.0, stop_loss=95.0, size_multiplier=1.0, has_position=False,
                has_pending_order=False, equity=1_000_000, day_start_equity=1_000_000,
                decision_ts=T0, cooldown_until=None, data_ok=True)
    base.update(kw)
    return EntryRequest(**base)


@pytest.fixture
def gate():
    return RiskGate(BASE_CFG["risk"])


def test_gate_approves_normal(gate):
    r = gate.check_entry(req())
    assert r.approved and r.approved_pct == pytest.approx(0.30)


@pytest.mark.parametrize("kw,needle", [
    ({"stop_loss": None}, "손절가"),
    ({"stop_loss": 101.0}, "손절가"),
    ({"has_position": True}, "포지션"),
    ({"has_pending_order": True}, "대기"),
    ({"equity": 969_000}, "일일 손실"),
    ({"cooldown_until": T0 + 1}, "연속 손실"),
    ({"data_ok": False, "data_reason": "x"}, "데이터"),
    ({"system_error": "timeout"}, "시스템"),
    ({"size_multiplier": 0.0}, "비중"),
])
def test_gate_rejects(gate, kw, needle):
    r = gate.check_entry(req(**kw))
    assert not r.approved and r.approved_pct == 0
    assert any(needle in x for x in r.reasons)


def test_gate_size_only_reduces(gate):
    assert gate.check_entry(req(size_multiplier=5.0)).approved_pct == pytest.approx(0.30)
    assert gate.check_entry(req(size_multiplier=0.5)).approved_pct == pytest.approx(0.15)


def test_daily_loss_below_limit_ok(gate):
    assert gate.check_entry(req(equity=971_000)).approved


def test_consecutive_losses(gate):
    streak, cd = 0, None
    for i in range(4):
        streak, cd = gate.after_trade(streak, -1.0, T0)
        assert cd is None
    streak, cd = gate.after_trade(streak, -1.0, T0)
    assert streak == 0 and cd == T0 + 24 * 3600
    assert gate.after_trade(3, +1.0, T0) == (0, None)


def test_check_bar():
    good = {"ts": T0, "open": 100, "high": 110, "low": 90, "close": 105}
    assert check_bar(good, T0 - UNIT * 60, UNIT) == (True, "")
    assert not check_bar(good, T0 - 2 * UNIT * 60, UNIT)[0]
    assert not check_bar({**good, "high": 101, "close": 105}, None, UNIT)[0]
    assert not check_bar({**good, "low": 0}, None, UNIT)[0]
    late_now = T0 + UNIT * 60 * 3
    assert not check_bar(good, None, UNIT, now_ts=late_now, max_delay_bars=1)[0]
    assert check_bar(good, None, UNIT, now_ts=T0 + UNIT * 60 + 30, max_delay_bars=1)[0]
