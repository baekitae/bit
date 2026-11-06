"""AI 위원회 역할별 프롬프트와 응답 스키마 (DESIGN.md 8-2, 8-4).

프롬프트를 바꾸면 PROMPT_VERSION 을 올린다 (회의록에 함께 저장되어 결과 해석에 쓰인다).
"""
from __future__ import annotations

import json
from typing import Any

PROMPT_VERSION = "v2"   # v2: 최근 12봉 변화율 12개, 90일 수익률 제공 (window 600)

COMMON = """당신은 업비트 KRW-BTC 현물 모의매매 시스템의 "AI 위원회" 위원입니다.

상황:
- 코드로 작성된 규칙 전략이 방금 마감된 봉에서 매수(진입) 신호를 냈습니다.
- 위원회는 이 신호를 따를지(follow), 건너뛸지(skip), 비중을 줄여 따를지(reduce)만 정할 수 있습니다.
- 새로운 매매를 만들거나 비중을 늘릴 수 없습니다. 손절은 코드가 항상 실행합니다.

규칙:
- 입력의 모든 숫자는 코드가 계산한 값입니다. 다시 계산하거나 고치지 말고 그대로 근거로 쓰세요.
- 입력에 없는 정보(뉴스, 온체인 데이터, 학습 데이터에서 기억하는 과거 가격 등)를 지어내거나 근거로 쓰지 마세요.
- 가격을 단정적으로 예측하지 말고, 근거와 불확실성을 함께 말하세요.
- 모든 문장은 한국어로, 짧고 구체적으로 쓰세요."""

ROLES: dict[str, dict[str, str]] = {
    "trend": {
        "title": "추세 분석가",
        "task": """역할: 추세 분석가.
추세의 방향과 강도를 평가하세요. 이동평균 이격, 기간별 수익률, 신호 강도, 최근 봉 흐름을 근거로
이 진입 신호가 추세에 순응하는지, 추세가 지속될 근거와 약해지는 징후가 무엇인지 판단하세요.""",
    },
    "regime": {
        "title": "국면·위험 분석가",
        "task": """역할: 국면·위험 분석가.
현재 시장이 추세장/횡보장/급변동장 중 어디에 가까운지 판단하고(코드의 국면 분류도 참고),
변동성, 손절까지 거리, 계좌 상태(당일 손익, 연속 손실, 최근 거래 성과)를 근거로 위험 요인을 평가하세요.""",
    },
    "red_team": {
        "title": "레드팀",
        "task": """역할: 레드팀.
이 신호를 따르면 안 되는 이유를 반드시, 가장 강한 것부터 제시하세요. 신호가 좋아 보여도 반론을 찾아야 합니다.
각 반론이 얼마나 심각한지 솔직하게 평가하세요. stance 는 반론이 충분히 강할 때만 oppose 로 하세요.""",
    },
}

CHAIR_TASK = """역할: 위원장.
세 위원(추세 분석가, 국면·위험 분석가, 레드팀)의 의견과 입력 데이터를 종합해 최종 결정을 내리세요.

결정 규칙:
- decision: follow(그대로 진입) / skip(진입 안 함) / reduce(비중을 줄여 진입)
- size_multiplier: follow=1.0, skip=0.0, reduce 는 0보다 크고 1보다 작은 값
- stop_loss: 규칙 손절가보다 높게(더 좁게) 잡고 싶을 때만 원화 가격을 적고, 아니면 0. 규칙 손절가보다 낮은 값은 무시됩니다.
- confidence: 0.0 ~ 1.0
- reasoning: 결정의 핵심 근거 요약
- dissent: 레드팀의 핵심 반론 요약 (결정과 별개로 반드시 기록)
- 근거가 엇갈리거나 불확실하면 무리하지 말고 reduce 또는 skip 을 고려하세요. 다만 이유 없는 skip 도 기회 손실입니다."""

MEMBER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "stance": {"type": "string", "enum": ["support", "neutral", "oppose"]},
        "summary": {"type": "string"},
        "points": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["stance", "summary", "points", "risks"],
    "additionalProperties": False,
}

CHAIR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["follow", "skip", "reduce"]},
        "size_multiplier": {"type": "number"},
        "stop_loss": {"type": "number"},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
        "dissent": {"type": "string"},
    },
    "required": ["decision", "size_multiplier", "stop_loss", "confidence", "reasoning", "dissent"],
    "additionalProperties": False,
}


def _dump(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False, indent=2)


def member_prompt(role: str, context: dict[str, Any]) -> tuple[str, str]:
    system = COMMON + "\n\n" + ROLES[role]["task"]
    user = "입력 데이터 (코드 계산):\n" + _dump(context) + "\n\n위 신호에 대한 당신의 의견을 JSON 으로 답하세요."
    return system, user


def chair_prompt(context: dict[str, Any], opinions: dict[str, Any]) -> tuple[str, str]:
    system = COMMON + "\n\n" + CHAIR_TASK
    named = {ROLES[k]["title"]: v for k, v in opinions.items()}
    user = ("입력 데이터 (코드 계산):\n" + _dump(context)
            + "\n\n위원 의견:\n" + _dump(named)
            + "\n\n최종 결정을 JSON 으로 답하세요.")
    return system, user
