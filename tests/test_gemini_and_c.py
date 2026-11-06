import copy
import json

import pytest

from src.council.council import Council
from src.council.llm import GeminiClient, LLMError
from src.council.prompts import CHAIR_SCHEMA
from tests.test_council import COUNCIL_CFG, FakeLLM
from tests.test_live import MarketSession  # noqa: F401  (fixture 재사용 위해)


class Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._p, self.text = status, payload, text

    def json(self):
        return self._p


class Sess:
    def __init__(self, resp):
        self.resp = resp
        self.sent = None

    def post(self, url, json, timeout, headers):
        self.sent = (url, json, headers)
        return self.resp


CHAIR = {"decision": "skip", "size_multiplier": 0, "stop_loss": 0, "confidence": 0.5, "reasoning": "r", "dissent": "d"}


def ok_payload(data, model="gemini-x-001"):
    return {"candidates": [{"content": {"parts": [{"text": "생각중", "thought": True}, {"text": json.dumps(data)}]},
                            "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20, "thoughtsTokenCount": 30},
            "modelVersion": model}


def call(sess, key="k"):
    return GeminiClient(session=sess, api_key=key).complete(
        model="gemini-x", system="s", user="u", schema=CHAIR_SCHEMA, effort="high", timeout=5, max_tokens=100)


def test_gemini_ok():
    sess = Sess(Resp(200, ok_payload(CHAIR)))
    r = call(sess)
    assert r.data == CHAIR and r.provider == "gemini" and r.model == "gemini-x-001"
    assert r.input_tokens == 100 and r.output_tokens == 50          # 생각 토큰 포함
    url, body, headers = sess.sent
    assert "gemini-x:generateContent" in url and headers["x-goog-api-key"] == "k"
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert "decision" in body["contents"][0]["parts"][0]["text"]       # 스키마를 프롬프트에 포함


@pytest.mark.parametrize("resp,needle", [
    (Resp(429, {}, "quota"), "한도"),
    (Resp(500, {}, "boom"), "500"),
    (Resp(200, {"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}), "SAFETY"),
    (Resp(200, {"candidates": [{"content": {"parts": [{"text": "not json"}]}, "finishReason": "STOP"}]}), "파싱"),
    (Resp(200, {"candidates": [{"content": {"parts": [{"text": "{}"}]}, "finishReason": "MAX_TOKENS"}]}), "MAX_TOKENS"),
    (Resp(200, ok_payload({"decision": "follow"})), "필수"),
])
def test_gemini_errors(resp, needle):
    with pytest.raises(LLMError, match=needle):
        call(Sess(resp))


def test_gemini_needs_key():
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        call(Sess(Resp(200, {})), key="")


def test_daily_limit_is_per_council(conn, cfg):
    cfg["council"] = copy.deepcopy(COUNCIL_CFG)
    g_settings = {**copy.deepcopy(COUNCIL_CFG), "max_meetings_per_day": 1, "free": True, "pricing": {}}
    claude = Council(cfg, client_factory=lambda p: FakeLLM())
    gemini = Council(cfg, client_factory=lambda p: FakeLLM(), settings=g_settings, name="gemini")
    ctx = {"x": 1}
    assert gemini.decide(conn, "t", "G1", 1, 0, ctx, 90, 100).error is None
    assert gemini.decide(conn, "t", "G1", 2, 0, ctx, 90, 100).error == "하루 최대 소집 횟수 초과"
    assert claude.decide(conn, "t", "B1", 3, 0, ctx, 90, 100).error is None   # Claude 는 별도 한도
    costs = dict(conn.execute("SELECT council, SUM(cost_krw) FROM council_meetings GROUP BY council").fetchall())
    assert costs["gemini"] == 0 and costs["default"] > 0


def test_live_with_b_g_c_tracks(conn, setup_full):
    runner, set_now, df = setup_full
    from tests.conftest import BAR, T0
    for k in range(600, 700):
        set_now(T0 + (k + 1) * BAR + 20)
        runner.step()
    names = {r[0] for r in conn.execute("SELECT track FROM tracks WHERE run_id='live'")}
    assert {"A1", "B1", "G1", "C"} <= names
    st = conn.execute("SELECT phase FROM evolution_state WHERE track='C'").fetchone()
    assert st is not None
    m = dict(conn.execute("SELECT council, COUNT(*) FROM council_meetings GROUP BY council").fetchall())
    assert m.get("default", 0) > 0 and m.get("gemini", 0) > 0
    so = {r[0] for r in conn.execute("SELECT b_track FROM signal_outcomes")}
    assert so <= {"B1", "G1"}                                            # C 는 짝비교하지 않음


@pytest.fixture
def setup_full(conn):
    from src.data.upbit import UpbitClient
    from src.live import LiveRunner
    from tests.conftest import BAR, BASE_CFG, T0, random_walk
    cfg = copy.deepcopy(BASE_CFG)
    cfg.update(data={"history_years": 1, "request_interval_sec": 0, "max_candle_delay_bars": 1},
               live={"delay_after_close_sec": 15, "max_late_sec": 900},
               regime={"ma_hours": 400, "slope_hours": 80, "slope_threshold": 0.01},
               council=copy.deepcopy(COUNCIL_CFG),
               evolution={"fixed_days": 14, "exclude_recent_days": 14, "min_sharpe_gain": 0.2,
                          "min_trades_ratio": 0.7, "shadow_days": 14, "max_shadow_days": 42, "min_shadow_signals": 4},
               backtest={"train_ratio": 0.7, "walk_forward": {"train_months": 3, "test_months": 1},
                         "min_trades_train": 3, "reject": {}})
    cfg["strategy"]["window_bars"] = 200
    p = {"fast": 5, "slow": 20, "atr_period": 14, "atr_stop_mult": 2.0}
    cfg["tracks"]["enabled"] = ["A1", "B1", "G1", "C"]
    cfg["tracks"]["defs"] = {"A1": {"type": "rule", "strategy": "ma_cross", "timeframe": 240, "params": p},
                             "B1": {"type": "council", "mirror": "A1"},
                             "G1": {"type": "council", "mirror": "A1", "council": "gemini"},
                             "C": {"type": "evolving", "base": "A1"}}
    df = random_walk(2400, seed=12)
    clock = [T0 + 600 * BAR]
    sess = MarketSession(df, clock)
    client = UpbitClient(session=sess, interval_sec=0, sleep=lambda s: None)
    now = {"t": 0}

    def set_now(t):
        now["t"] = t
        clock[0] = (t // BAR) * BAR

    set_now(T0 + 601 * BAR + 20)
    llm = FakeLLM()
    councils = {"default": Council(cfg, client_factory=lambda p: llm, now=lambda: now["t"]),
                "gemini": Council(cfg, client_factory=lambda p: FakeLLM(), settings=copy.deepcopy(COUNCIL_CFG),
                                  name="gemini", now=lambda: now["t"])}
    runner = LiveRunner(conn, cfg, client, council=councils, run_id="live", now=lambda: now["t"], log=lambda s: None)
    return runner, set_now, df
