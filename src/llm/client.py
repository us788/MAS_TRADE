"""LLM 호출 레이어. 기획서 4절.

에이전트 코드는 벤더 SDK를 직접 부르지 않는다. 이 모듈만 통한다.

지키는 것:
- **벤더 교체 가능**(4.7절). DeepSeek는 OpenAI ChatCompletions 호환이라
  base_url·모델명 교체만으로 이전할 수 있어야 한다.
- **전량 로깅**(4.3절·8절). 호출마다 응답이 보고한 모델 버전·토큰·캐시 적중·
  추정 비용·타임스탬프를 LLMCall로 남긴다. 이게 없는 결과는 재현 불가로 취급한다.
- **프리픽스 캐싱**(4.2절). 캐시 히트 단가가 미스보다 모델에 따라 30~50배 싸다.
  고정 프리픽스(시스템 프롬프트·용어·스키마)를 앞에, 종목별 가변부를 뒤에 둔다.
  호출마다 앞부분을 흔들면 캐시가 전부 깨진다. build_messages()를 쓸 것.

하지 않는 것:
- **산술.** 수치는 src/compute/의 결정론적 코드가 계산해 넘긴다. 프롬프트 안에서
  계산을 시키지 않는다 (CLAUDE.md 3절).
"""
from __future__ import annotations

import time as _time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from openai import OpenAI

from src import config
from src.llm.pricing import estimate_cost_usd, is_off_peak

Tier = Literal["fast", "deep"]

_TIER_TO_MODEL: dict[Tier, str] = {
    "fast": config.LLM_MODEL_FAST,
    "deep": config.LLM_MODEL_DEEP,
}


@dataclass(frozen=True)
class LLMCall:
    """호출 1건의 감사 기록. 시그널 로그에 그대로 붙는다."""

    call_id: str
    tier: Tier
    requested_model: str
    reported_model: str
    """응답이 실제로 보고한 모델 버전. 별칭 뒤에서 모델이 갱신되면 여기서 드러난다(4.3절)."""
    thinking: bool
    called_at: datetime
    latency_ms: int
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    off_peak: bool
    estimated_cost_usd: float | None
    """단가표에 없는 모델이면 None. 모르면 0이 아니라 모른다고 기록한다."""
    finish_reason: str | None
    reasoning_tokens: int = 0
    """실제로 쓰인 추론 토큰. 요청한 모드와 실제 동작이 어긋나면 여기서 드러난다."""


def build_messages(
    stable_prefix: str, variable_part: str, *, history: list[dict[str, str]] | None = None
) -> list[dict[str, str]]:
    """캐시 친화적 메시지 구성.

    stable_prefix는 호출 간 **바이트 단위로 동일**해야 한다. 날짜·종목명·랜덤 ID를
    여기 넣으면 캐시가 매번 깨진다. 그런 것은 variable_part로.
    """
    messages: list[dict[str, str]] = [{"role": "system", "content": stable_prefix}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": variable_part})
    return messages


class LLMClient:
    """DeepSeek(OpenAI 호환) 래퍼."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self._client = OpenAI(
            api_key=api_key or config.require("DEEPSEEK_API_KEY"),
            base_url=base_url or config.LLM_BASE_URL,
            timeout=config.LLM_TIMEOUT_SEC,
        )

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        tier: Tier = "fast",
        thinking: bool = False,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> tuple[str, LLMCall]:
        """텍스트 1건을 받고 감사 기록을 함께 반환한다."""
        model = _TIER_TO_MODEL[tier]
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": config.LLM_TEMPERATURE,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        kwargs.update(_thinking_kwargs(thinking))

        called_at = datetime.now(timezone.utc)
        started = _time.perf_counter()
        response = self._client.chat.completions.create(**kwargs)
        latency_ms = int((_time.perf_counter() - started) * 1000)

        usage = response.usage
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage, "completion_tokens", 0) or 0
        cached_tokens = _cached_tokens(usage)
        reasoning_tokens = _reasoning_tokens(usage, response)

        record = LLMCall(
            call_id=str(uuid.uuid4()),
            tier=tier,
            requested_model=model,
            reported_model=getattr(response, "model", "") or "",
            thinking=thinking,
            called_at=called_at,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            cached_tokens=cached_tokens,
            completion_tokens=completion_tokens,
            off_peak=is_off_peak(called_at),
            estimated_cost_usd=estimate_cost_usd(
                model,
                cached_input_tokens=cached_tokens,
                uncached_input_tokens=max(prompt_tokens - cached_tokens, 0),
                output_tokens=completion_tokens,
                at=called_at,
            ),
            finish_reason=response.choices[0].finish_reason,
            reasoning_tokens=reasoning_tokens,
        )
        return response.choices[0].message.content or "", record


def _thinking_kwargs(thinking: bool) -> dict[str, Any]:
    """thinking / non-thinking 모드 전환 지점.

    벤더가 이 스위치의 파라미터명을 바꿀 수 있으므로 **한 곳에만** 둔다.

    2026-09-16 실제 호출로 확인한 것:

    - OpenAI SDK에서는 `extra_body`로 넘긴다 — `{"thinking": {"type": "enabled"}}`
    - **아무것도 보내지 않으면 thinking이 켜진 상태가 기본이다.** 그래서 반드시
      명시적으로 disabled를 보내야 한다. 이전 구현은 빈 dict를 돌려줬고,
      `thinking=False` 호출이 조용히 thinking 모드로 돌고 있었다 —
      기획서 4.1절(스크리닝·포맷 정리는 non-thinking)과 어긋나고 비용도 더 든다
    - `reasoning_effort="none"`도 같은 효과지만, 두 스위치를 섞지 않는다
    """
    return {"extra_body": {"thinking": {"type": "enabled" if thinking else "disabled"}}}


def _reasoning_tokens(usage: Any, response: Any) -> int:
    """추론 토큰 수. 필드가 없으면 reasoning_content 존재 여부로 대체 추정한다.

    "non-thinking으로 요청했는데 추론이 돌았다"를 사후에 알아채기 위한 장치다.
    """
    details = getattr(usage, "completion_tokens_details", None)
    value = getattr(details, "reasoning_tokens", None)
    if isinstance(value, int):
        return value
    try:
        content = response.choices[0].message.reasoning_content
    except (AttributeError, IndexError):
        return 0
    return len(content) if content else 0


def _cached_tokens(usage: Any) -> int:
    """프리픽스 캐시 적중 토큰 수. 필드명이 벤더마다 달라 방어적으로 읽는다."""
    for attr in ("prompt_cache_hit_tokens", "cached_tokens"):
        value = getattr(usage, attr, None)
        if isinstance(value, int):
            return value
    details = getattr(usage, "prompt_tokens_details", None)
    value = getattr(details, "cached_tokens", None)
    return value if isinstance(value, int) else 0
