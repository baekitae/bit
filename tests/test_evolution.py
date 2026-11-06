import copy
import json

import pytest

from src.council.council import Council
from src.council.llm import LLMError, LLMResponse
from src.evolution.manager import DAY, Evolution, get_version, shadow_track_name
from src.tracks.registry import build_tracks
from tests.conftest import BAR, BASE_CFG, T0, random_walk, store_candles

EVO = {"fixed_days": 14, "exclude_recent_days": 14, "min_sharpe_gain": 0.2, "min_trades_ratio": 0.7,
       "shadow_days": 14, "max_shadow_days": 42, "min_shadow_signals": 4}
BACKTEST = {"train_ratio": 0.7, "walk_forward": {"train_months": 6, "test_months": 3}, "min_trades_train": 3,
            "reject": {"val_sharpe_ratio_min": 0.5, "neighbor_profitable_min": 0.6,
                       "neighbor_sharpe_gap_max": 0.5, "min_trades_total": 30, "cost_stress_mult": 2.0}}
BASE = {"fast": 20, "slow": 60, "atr_period": 14, "atr_stop_mult": 2.0}


class ProposerLLM:
    provider = "fake"

    def __init__(self, action="change", params=None, fail=False):
        self.action, self.params, self.fail = action, params, fail
        self.calls = 0

    def complete(self, *, model, system, user, schema, effort, timeout, max_tokens):
        self.calls += 1
        if self.fail:
            raise LLMError("시간 초과")
        data = {"action": self.action, "params": {**BASE, **(self.params or {})},
                "change_summary": "테스트", "reason": "테스트"}
        return LLMResponse(data=data, text=json.dumps(data), provider="fake", model="m",
                           input_tokens=100, output_tokens=50)


@pytest.fixture
def env(conn):
    cfg = copy.deepcopy(BASE_CFG)
    cfg.update(evolution=dict(EVO), backtest=BACKTEST, data={"history_years": 10},
               regime={"ma_hours": 400, "slope_hours": 80, "slope_threshold": 0.01},
               comparison={"min_signals": 30},
               council={"default": {"provider": "fake", "model": "m", "effort": "high"}, "timeout_sec": 5,
                        "pricing": {"m": [1.0, 1.0]}, "usd_krw": 1000})
    df = random_walk(3000, seed=21)
    store_candles(conn, df)
    return cfg, df


def make(conn, cfg, llm):
    c = Council(cfg, client_factory=lambda p: llm)
    return Evolution(conn, cfg, "live", "C", "ma_cross", BASE, c)


def now_at(df, i):
    return int(df["ts"].iloc[i]) + BAR


def test_initial_version_and_fixed_period(conn, env):
    cfg, df = env
    llm = ProposerLLM()
    ev = make(conn, cfg, llm)
    t0 = now_at(df, 2500)
    assert ev.tick(t0) is False
    assert ev.active()["version"] == "v1" and json.loads(ev.active()["params"]) == BASE
    assert ev.tick(t0 + 13 * DAY) is False and llm.calls == 0       # 14일 전에는 제안 없음
    log = conn.execute("SELECT event FROM run_log WHERE event='comparison_start'").fetchall()
    assert len(log) == 1


def test_keep_records_no_change(conn, env):
    cfg, df = env
    ev = make(conn, cfg, ProposerLLM(action="keep"))
    t0 = now_at(df, 2500)
    ev.tick(t0)
    assert ev.tick(t0 + 14 * DAY) is False
    assert get_version(conn, "live", "C", "no_change") is not None
    st = ev.state()
    assert st["phase"] == "fixed" and st["cycle_start_ts"] == t0 + 14 * DAY and st["attempts"] == 1


def test_out_of_range_is_rejected(conn, env):
    cfg, df = env
    ev = make(conn, cfg, ProposerLLM(params={"fast": 500}))
    t0 = now_at(df, 2500)
    ev.tick(t0)
    ev.tick(t0 + 14 * DAY)
    rej = get_version(conn, "live", "C", "rejected")
    assert rej is not None and "허용 범위" in rej["validation"]
    assert ev.active()["version"] == "v1"


def test_ai_failure_is_recorded_and_cycle_restarts(conn, env):
    cfg, df = env
    ev = make(conn, cfg, ProposerLLM(fail=True))
    t0 = now_at(df, 2500)
    ev.tick(t0)
    assert ev.tick(t0 + 14 * DAY) is False
    assert get_version(conn, "live", "C", "rejected")["change_summary"] == "AI 제안 실패"


def test_validation_gate(conn, env, monkeypatch):
    cfg, df = env
    import src.evolution.manager as m
    seen = {}

    def fake_validate(conn_, cfg_, strategy, current, proposed, now_ts):
        seen["end"] = now_ts
        good = proposed != current
        return {"passed": good, "reason": "가짜", "current": {"sharpe": 1.0}, "proposed": {"sharpe": 1.5}}
    monkeypatch.setattr(m, "validate_proposal", fake_validate)
    ev = make(conn, cfg, ProposerLLM(params={"fast": 15}))
    t0 = now_at(df, 2500)
    ev.tick(t0)
    assert ev.tick(t0 + 14 * DAY) is True
    sh = ev.shadow()
    assert sh["version"] == "v2" and json.loads(sh["params"])["fast"] == 15
    assert ev.state()["phase"] == "shadow"
    # 섀도 트랙이 레지스트리에 생긴다
    cfg2 = copy.deepcopy(cfg)
    cfg2["tracks"]["enabled"] = ["C"]
    cfg2["tracks"]["defs"] = {"A1": {"type": "rule", "strategy": "ma_cross", "params": BASE}, "C": {"type": "evolving", "base": "A1"}}
    names = [t.name for t in build_tracks(conn, cfg2, "live", council=ev.council)]
    assert names == ["C", shadow_track_name("C", "v2")]


def test_real_validation_rejects_identical_strategy(conn, env):
    """실제 검증: 같은 성과면 샤프 +0.2 를 못 넘어 탈락해야 한다."""
    cfg, df = env
    from src.evolution.validate import validate_proposal
    now = now_at(df, 2999)
    r = validate_proposal(conn, cfg, "ma_cross", BASE, dict(BASE), now)
    assert r["passed"] is False and "미달" in r["reason"]
    assert r["data_range"][1] <= now - 14 * DAY                       # 최근 14일 제외


def _start_shadow(conn, cfg, df, monkeypatch, t0):
    import src.evolution.manager as m
    monkeypatch.setattr(m, "validate_proposal", lambda *a, **k: {"passed": True, "reason": "가짜"})
    ev = make(conn, cfg, ProposerLLM(params={"fast": 15}))
    ev.tick(t0)
    ev.tick(t0 + 14 * DAY)
    return ev


def _fake_track_data(conn, start, cur_ret, sh_ret, signals):
    for name, ret in (("C", cur_ret), ("C_v2", sh_ret)):
        conn.execute("INSERT INTO equity VALUES ('live', ?, ?, 1, 0, 1000000)", (name, start - BAR))
        conn.execute("INSERT INTO equity VALUES ('live', ?, ?, 1, 0, ?)", (name, start + 10 * DAY, 1e6 * (1 + ret)))
    for k in range(signals):
        conn.execute("""INSERT INTO signals (run_id, track, strategy, strategy_version, bar_ts, action, price)
                        VALUES ('live', 'C', 'ma_cross', 'v1', ?, 'buy', 1)""", (start + k * BAR,))
    conn.execute("""INSERT INTO tracks (run_id, track, strategy, strategy_version, initial_cash, cash)
                    VALUES ('live', 'C', 'ma_cross', 'v1', 1000000, 1000000)""")
    conn.commit()


def test_shadow_better_is_promoted(conn, env, monkeypatch):
    cfg, df = env
    t0 = now_at(df, 2500)
    ev = _start_shadow(conn, cfg, df, monkeypatch, t0)
    start = ev.state()["shadow_start_ts"]
    _fake_track_data(conn, start, cur_ret=0.01, sh_ret=0.03, signals=5)
    assert ev.tick(start + 13 * DAY) is False
    assert ev.tick(start + 14 * DAY) is True
    assert ev.active()["version"] == "v2" and ev.shadow() is None
    assert get_version(conn, "live", "C", "retired")["version"] == "v1"
    assert conn.execute("SELECT strategy_version FROM tracks WHERE track='C'").fetchone()[0] == "v2"


def test_shadow_worse_is_retired(conn, env, monkeypatch):
    cfg, df = env
    t0 = now_at(df, 2500)
    ev = _start_shadow(conn, cfg, df, monkeypatch, t0)
    start = ev.state()["shadow_start_ts"]
    _fake_track_data(conn, start, cur_ret=0.03, sh_ret=0.01, signals=5)
    assert ev.tick(start + 14 * DAY) is True
    assert ev.active()["version"] == "v1"
    v2 = conn.execute("SELECT status, validation FROM strategy_versions WHERE version='v2'").fetchone()
    assert v2["status"] == "retired" and "폐기" in v2["validation"]


def test_shadow_extends_then_gives_up(conn, env, monkeypatch):
    cfg, df = env
    t0 = now_at(df, 2500)
    ev = _start_shadow(conn, cfg, df, monkeypatch, t0)
    start = ev.state()["shadow_start_ts"]
    _fake_track_data(conn, start, cur_ret=0.0, sh_ret=0.05, signals=2)   # 신호 부족
    assert ev.tick(start + 14 * DAY) is False                            # 연장
    assert ev.tick(start + 41 * DAY) is False
    assert ev.tick(start + 42 * DAY) is True                             # 판정 불가 폐기
    assert ev.active()["version"] == "v1"
    assert "판정 불가" in conn.execute("SELECT validation FROM strategy_versions WHERE version='v2'").fetchone()[0]


def test_rollback(conn, env, monkeypatch):
    cfg, df = env
    t0 = now_at(df, 2500)
    ev = _start_shadow(conn, cfg, df, monkeypatch, t0)
    start = ev.state()["shadow_start_ts"]
    _fake_track_data(conn, start, cur_ret=0.0, sh_ret=0.05, signals=5)
    ev.tick(start + 14 * DAY)
    assert ev.active()["version"] == "v2"
    ev.rollback("v1", start + 15 * DAY)
    act = ev.active()
    assert act["version"] == "v3" and json.loads(act["params"]) == BASE
