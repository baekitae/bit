"""AI 제공자 공통 인터페이스 (DESIGN.md 8-6).

위원회 코드는 특정 회사 API 를 직접 부르지 않고 `LLMClient.complete()` 만 부른다.
제공자를 추가하려면 이 인터페이스를 구현한 어댑터를 만들고 `make_client()` 에 등록한다.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol


class LLMError(Exception):
    """AI 호출 실패 (네트워크, 시간 초과, 거부, 형식 오류 등). 위원회는 이 경우 skip 한다."""


@dataclass
class LLMResponse:
    data: dict[str, Any]          # JSON 스키마에 맞춘 응답
    text: str                     # 원시 응답 텍스트
    provider: str
    model: str                    # 실제로 응답한 모델
    input_tokens: int = 0
    output_tokens: int = 0
    latency_sec: float = 0.0
    used_fallback: bool = False   # 제공자 쪽 대체 모델이 응답했는지
    extra: dict[str, Any] = field(default_factory=dict)


class LLMClient(Protocol):
    provider: str

    def complete(self, *, model: str, system: str, user: str, schema: dict[str, Any],
                 effort: str, timeout: float, max_tokens: int) -> LLMResponse: ...


class ClaudeClient:
    """Anthropic Claude (기본 제공자).

    - 구조화 출력(output_config.format)으로 JSON 스키마를 강제한다.
    - 안전 분류기가 요청을 거부하면 서버가 다른 모델로 다시 실행한다 (fallbacks: "default").
    - 키는 환경변수 ANTHROPIC_API_KEY (.env) 에서 읽는다.
    """
    provider = "claude"
    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, server_fallback: bool = True, max_retries: int = 2):
        import anthropic  # 지연 import: A 트랙만 돌릴 때는 필요 없음
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(max_retries=max_retries)
        self.server_fallback = server_fallback

    def complete(self, *, model: str, system: str, user: str, schema: dict[str, Any],
                 effort: str, timeout: float, max_tokens: int) -> LLMResponse:
        a = self._anthropic
        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            timeout=timeout,
        )
        if self.server_fallback:
            kwargs.update(betas=[self.FALLBACK_BETA], fallbacks="default")
        t0 = time.monotonic()
        try:
            resp = self.client.beta.messages.create(**kwargs)
        except a.APITimeoutError as e:
            raise LLMError(f"시간 초과: {e}") from e
        except a.RateLimitError as e:
            raise LLMError(f"호출 한도 초과(429): {e.message}") from e
        except a.AuthenticationError as e:
            raise LLMError("API 키 오류: ANTHROPIC_API_KEY 를 확인하세요") from e
        except a.APIStatusError as e:
            raise LLMError(f"API 오류 {e.status_code}: {e.message}") from e
        except a.APIConnectionError as e:
            raise LLMError(f"연결 오류: {e}") from e
        latency = time.monotonic() - t0

        if resp.stop_reason == "refusal":
            cat = getattr(resp.stop_details, "category", None) if resp.stop_details else None
            raise LLMError(f"모델이 응답을 거부함 (category={cat})")
        if resp.stop_reason == "max_tokens":
            raise LLMError("응답이 max_tokens 에서 잘림")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"JSON 파싱 실패: {text[:200]!r}") from e
        used_fallback = any(getattr(b, "type", None) == "fallback" for b in resp.content)
        return LLMResponse(
            data=data, text=text, provider=self.provider, model=resp.model,
            input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens,
            latency_sec=latency, used_fallback=used_fallback,
            extra={"request_id": getattr(resp, "_request_id", None)},
        )


def make_client(provider: str, **kwargs: Any) -> LLMClient:
    if provider == "claude":
        return ClaudeClient(**kwargs)
    raise LLMError(f"아직 지원하지 않는 AI 제공자: {provider} (src/council/llm.py 에 어댑터를 추가하세요)")
