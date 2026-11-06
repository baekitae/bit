"""C 트랙 수정안 제안 (DESIGN.md 9-2). AI 는 파라미터를 허용 범위 안에서만 바꿀 수 있다."""
from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

from src.council.llm import LLMError, LLMResponse
from src.strategies import REGISTRY

PROMPT_VERSION = "evo-v1"

SYSTEM = """당신은 업비트 KRW-BTC 현물 모의매매 시스템의 "전략 검토 위원회"입니다.
C 트랙은 규칙 전략 + AI 필터로 운영되며, 정해진 주기마다 전략 파라미터 수정을 한 번 제안할 수 있습니다.

규칙:
- 바꿀 수 있는 것은 전략 파라미터뿐이고, 각 파라미터의 허용 범위 안이어야 합니다. 코드나 리스크 규칙은 바꿀 수 없습니다.
- 제안은 코드가 과거 데이터로 다시 검증합니다(워크포워드 표본 외 샤프 +0.2 이상, MDD 악화 금지, 거래 수 70% 이상).
  검증을 통과해야 섀도 운영에 들어가고, 섀도에서 더 나아야 교체됩니다.
- 입력의 숫자는 코드가 계산한 값입니다. 다시 계산하지 말고 근거로 쓰세요. 입력에 없는 정보를 지어내지 마세요.
- 최근 몇 주 성과에 과하게 맞추는 것은 과최적화입니다. 근거가 약하면 keep(유지)을 고르세요.
- 한국어로 짧고 구체적으로 쓰세요."""


def proposal_schema(strategy: str) -> dict[str, Any]:
    cls = REGISTRY[strategy]
    props = {k: {"type": "integer" if r.kind is int else "number"} for k, r in cls.param_space.items()}
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["change", "keep"]},
            "params": {"type": "object", "properties": props, "required": list(props),
                       "additionalProperties": False},
            "change_summary": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["action", "params", "change_summary", "reason"],
        "additionalProperties": False,
    }


def _r(x: Any, nd: int = 4) -> Any:
    if isinstance(x, float):
        return None if math.isnan(x) else round(x, nd)
    return x


def build_review_input(conn: sqlite3.Connection, run_id: str, track: str, strategy: str,
                       params: dict[str, Any], since_ts: int, backtest: dict[str, Any],
                       history: list[dict[str, Any]]) -> dict[str, Any]:
    cls = REGISTRY[strategy]
    trades = conn.execute(
        "SELECT pnl_pct, exit_kind FROM trades WHERE run_id=? AND track=? AND exit_ts >= ?",
        (run_id, track, since_ts)).fetchall()
    eq = conn.execute(
        "SELECT equity FROM equity WHERE run_id=? AND track=? AND bar_ts >= ? ORDER BY bar_ts",
        (run_id, track, since_ts)).fetchall()
    meetings = conn.execute(
        """SELECT m.bar_ts, m.decision, m.error FROM council_meetings m
           WHERE m.run_id=? AND m.track=? AND m.bar_ts >= ? ORDER BY m.id""",
        (run_id, track, since_ts)).fetchall()
    mt = []
    for m in meetings[-20:]:
        d = json.loads(m["decision"] or "{}")
        raw = d.get("raw") or {}
        mt.append({"결정": d.get("applied", {}).get("decision"), "확신": _r(raw.get("confidence")),
                   "근거": (raw.get("reasoning") or "")[:200], "오류": m["error"]})
    return {
        "전략": f"{cls.label} ({strategy})",
        "현재_파라미터": params,
        "허용_범위": {k: [r.low, r.high, "정수" if r.kind is int else "실수"] for k, r in cls.param_space.items()},
        "이번_주기_실적": {
            "수익률": _r(eq[-1][0] / eq[0][0] - 1) if len(eq) >= 2 else None,
            "거래_수": len(trades),
            "거래별_수익률": [_r(t["pnl_pct"]) for t in trades],
            "청산_종류": [t["exit_kind"] for t in trades],
        },
        "이번_주기_위원회_결정": mt,
        "현재_파라미터_과거검증_코드계산": backtest,
        "이전_수정_이력": history[-10:],
    }


def propose(client, model: str, effort: str, timeout: float, strategy: str,
            review: dict[str, Any]) -> tuple[dict[str, Any], LLMResponse]:
    user = ("입력 데이터 (코드 계산):\n" + json.dumps(review, ensure_ascii=False, indent=2)
            + "\n\n파라미터를 바꿀지(change) 유지할지(keep) JSON 으로 답하세요. keep 이면 params 에 현재 값을 그대로 적으세요.")
    r = client.complete(model=model, system=SYSTEM, user=user, schema=proposal_schema(strategy),
                        effort=effort, timeout=timeout, max_tokens=16000)
    data = r.data
    if data.get("action") not in ("change", "keep"):
        raise LLMError(f"알 수 없는 action: {data.get('action')!r}")
    return data, r
