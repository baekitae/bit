import json

import pandas as pd
import pytest

from src.council.council import Council, validate_decision
from src.council.llm import LLMError, LLMResponse
from src.strategies.base import BUY, SELL
from src.tracks.tracks import CouncilTrack, RuleTrack
from tests.conftest import BAR, T0, Scripted, make_candles, random_walk, run_bars

COUNCIL_CFG = {
    "default": {"provider": "fake", "model": "claude-opus-5-5", "effort": "high"},
    "roles": {"red_team": {"model": "claude-sonnet-5-5"}},
    "fallback": {"enabled": False},
    "timeout_sec": 5, "max_tokens": 1000, "max_meetings_per_day": 100, "usd_krw": 1000,
    "pricing": {"claude-opus-5-5": [4.0, 20.0], "claude-sonnet-5-5": [2.0, 10.0]},
}
REGIME = {"ma_hours": 400, "slope_hours": 80, "slope_threshold": 0.01}


class FakeLLM:
    provider = "fake"

    def __init__(self, chair=None, fail_role=None, error=None):
        self.chair = chair or {"decision": "follow", "size_multiplier": 1.0, "stop_loss": 0,
                               "confidence": 0.7, "reasoning": "추세 양호", "dissent": "과열 가능성"}
        self.fail_role = fail_role
        self.error = error
        self.calls = []

    def complete(self, *, model, system, user, schema, effort, timeout, max_tokens):
        role = ("chair" if "역할: 위원장" in system else "red_team" if "역할: 레드팀" in system
                else "trend" if "역할: 추세" in system else "regime")
        self.calls.append((role, model, json.loads(user.split("\n", 1)[1].rsplit("\n\n", 1)[0])
                           if role != "chair" else None))
        if role == self.fail_role:
            raise LLMError(self.error or "시간 초과")
        data = self.chair if role == "chair" else {"stance": "support", "summary": role, "points": ["a"], "risks": []}
        return LLMResponse(data=data, text=json.dumps(data), provider="fake", model=model,
                           input_tokens=1000, output_tokens=500, latency_sec=0.1)


@pytest.fixture
def ccfg(cfg):
    cfg["council"] = json.loads(json.dumps(COUNCIL_CFG))
    cfg["regime"] = REGIME
    return cfg


def council(cfg, llm):
    return Council(cfg, client_factory=lambda p: llm)


# ---- 결정 검증 ----
def test_validate_follow_skip_reduce():
    assert validate_decision({"decision": "follow", "size_multiplier": 0.3}, 90, 100).size_multiplier == 1.0
    assert validate_decision({"decision": "skip", "size_multiplier": 0.8}, 90, 100).size_multiplier == 0.0
    d = validate_decision({"decision": "reduce", "size_multiplier": 1.7}, 90, 100)
    assert d.decision == "follow" and d.size_multiplier == 1.0    # 비중은 키울 수 없음
    assert validate_decision({"decision": "reduce", "size_multiplier": 0.5}, 90, 100).size_multiplier == 0.5
    assert validate_decision({"decision": "reduce", "size_multiplier": -1}, 90, 100).decision == "skip"
    with pytest.raises(LLMError):
        validate_decision({"decision": "buy_more"}, 90, 100)


def test_validate_stop_only_tightens():
    assert validate_decision({"decision": "follow", "stop_loss": 95}, 90, 100).stop_loss == 95    # 좁힘 OK
    assert validate_decision({"decision": "follow", "stop_loss": 80}, 90, 100).stop_loss == 90    # 넓힘 무시
    assert validate_decision({"decision": "follow", "stop_loss": 105}, 90, 100).stop_loss == 90   # 현재가 이상 무시
    assert validate_decision({"decision": "follow", "stop_loss": 0}, 90, 100).stop_loss == 90


# ---- 회의를 포함한 트랙 실행 ----
def run_pair(conn, cfg, llm, script=None):
    df = random_walk(300, seed=3)
    script = script or {250: BUY, 270: SELL}
    c = council(cfg, llm)
    a = RuleTrack(conn, "t", cfg, Scripted(script), "v1", name="A1")
    b = CouncilTrack(conn, "t", cfg=cfg, strategy=Scripted(script), strategy_version="v1",
                     name="B1", council=c, mirror="A1")
    for i in range(len(df)):
        w = df.iloc[max(0, i - 299): i + 1]
        prev = int(df["ts"].iloc[i - 1]) if i else None
        a.on_bar(w, prev)
        b.on_bar(w, prev)
    from src.live import update_signal_outcomes
    update_signal_outcomes(conn, "t")
    return df


def trades(conn, track):
    return pd.read_sql_query("SELECT * FROM trades WHERE run_id='t' AND track=?", conn, params=(track,))


def test_follow_enters_like_a(conn, ccfg):
    llm = FakeLLM()
    run_pair(conn, ccfg, llm)
    assert len(trades(conn, "A1")) == len(trades(conn, "B1")) == 1
    m = conn.execute("SELECT * FROM council_meetings").fetchone()
    assert m["error"] is None and m["prompt_version"] == "v1"
    assert m["input_tokens"] == 4000 and m["output_tokens"] == 2000
    # 비용: opus 3회 (1000*4 + 500*20)/1e6 + sonnet 1회 (1000*2 + 500*10)/1e6, 달러→원 1000
    assert m["cost_krw"] == pytest.approx((3 * 0.014 + 0.007) * 1000)
    providers = json.loads(m["providers"])
    assert providers["red_team"]["model"] == "claude-sonnet-5-5" and providers["chair"]["model"] == "claude-opus-5-5"
    o = conn.execute("SELECT * FROM signal_outcomes").fetchone()
    assert o["b_decision"] == "follow" and o["a_signal_id"] is not None
    assert o["a_trade_id"] is not None and o["b_trade_id"] is not None
    assert o["a_pnl_pct"] == pytest.approx(o["b_pnl_pct"])


def test_members_get_code_computed_context(conn, ccfg):
    llm = FakeLLM()
    run_pair(conn, ccfg, llm)
    ctx = next(c[2] for c in llm.calls if c[0] == "trend")
    assert ctx["규칙전략_신호"]["행동"] == "매수(진입)"
    assert ctx["계좌"]["트랙"] == "B1" and "국면_코드분류" in ctx["지표"]
    assert {c[0] for c in llm.calls} == {"trend", "regime", "red_team", "chair"}


def test_skip_blocks_entry_and_is_recorded(conn, ccfg):
    llm = FakeLLM(chair={"decision": "skip", "size_multiplier": 0, "stop_loss": 0, "confidence": 0.6,
                         "reasoning": "횡보", "dissent": "-"})
    run_pair(conn, ccfg, llm)
    assert len(trades(conn, "A1")) == 1 and len(trades(conn, "B1")) == 0
    rc = conn.execute("SELECT * FROM risk_checks WHERE track='B1'").fetchone()
    assert rc["approved"] == 0
    o = conn.execute("SELECT * FROM signal_outcomes").fetchone()
    assert o["b_decision"] == "skip" and o["a_pnl_pct"] is not None and o["b_trade_id"] is None


def test_reduce_halves_size(conn, ccfg):
    llm = FakeLLM(chair={"decision": "reduce", "size_multiplier": 0.5, "stop_loss": 0, "confidence": 0.5,
                         "reasoning": "-", "dissent": "-"})
    run_pair(conn, ccfg, llm)
    a = conn.execute("SELECT target_notional FROM paper_orders WHERE track='A1' AND side='buy'").fetchone()[0]
    b = conn.execute("SELECT target_notional FROM paper_orders WHERE track='B1' AND side='buy'").fetchone()[0]
    assert b == pytest.approx(a * 0.5)


@pytest.mark.parametrize("role", ["trend", "red_team", "chair"])
def test_any_failure_means_skip(conn, ccfg, role):
    llm = FakeLLM(fail_role=role, error="시간 초과")
    run_pair(conn, ccfg, llm)
    assert len(trades(conn, "B1")) == 0 and len(trades(conn, "A1")) == 1
    m = conn.execute("SELECT * FROM council_meetings").fetchone()
    assert "시간 초과" in m["error"]
    rc = conn.execute("SELECT reasons FROM risk_checks WHERE track='B1'").fetchone()[0]
    assert "시스템 오류" in rc
    o = conn.execute("SELECT b_decision FROM signal_outcomes").fetchone()[0]
    assert o == "error"


def test_stop_loss_still_runs_for_b(conn, ccfg):
    """AI 와 무관하게 손절은 실행된다."""
    rows = [(100.0, 101.0, 99.0, 100.0)] * 250 + [(100.0, 101.0, 80.0, 82.0)] + [(82.0, 83.0, 81.0, 82.0)] * 5
    df = make_candles(rows)
    c = council(ccfg, FakeLLM())
    b = CouncilTrack(conn, "t", cfg=ccfg, strategy=Scripted({249: BUY}), strategy_version="v1",
                     name="B1", council=c, mirror=None)
    run_bars(b, df, window=300)
    t = trades(conn, "B1")
    assert len(t) == 1 and t.iloc[0]["exit_kind"] == "stop"


def test_daily_limit(conn, ccfg):
    ccfg["council"]["max_meetings_per_day"] = 0
    llm = FakeLLM()
    run_pair(conn, ccfg, llm)
    assert llm.calls == []
    m = conn.execute("SELECT error FROM council_meetings").fetchone()
    assert m["error"] == "daily_limit"
    assert len(trades(conn, "B1")) == 0


def test_fallback_provider(conn, ccfg):
    ccfg["council"]["fallback"] = {"enabled": True, "provider": "backup", "model": "backup-model"}
    primary = FakeLLM(fail_role="trend")
    backup = FakeLLM()
    c = Council(ccfg, client_factory=lambda p: backup if p == "backup" else primary)
    from src.council.context import build_context  # noqa: F401
    r = c._call("trend", "역할: 추세 분석가", "입력 데이터 (코드 계산):\n{}\n\nx", {})
    assert r.used_fallback and r.model == "backup-model"
