"""DeepSeek 단일 호출 베이스라인 — 기획서 4.5절.

**이것 없이는 어떤 멀티에이전트 구조도 평가할 수 없다.** 기획서 12절 성공 기준이
"멀티에이전트 구조가 같은 모델의 단일 호출을 유의미하게 이기는가"이고, 못 이기면
구조를 단순화하는 것이 정답이라고 못박아 뒀다. 그 비교 대상이 여기다.

구성은 최소다 — `deepseek-v4-pro`, thinking, **도구도 에이전트도 없다.**
다른 벤더 모델을 베이스라인에 두면 "구조가 이겼나"와 "모델이 더 좋았나"가 분리되지
않으므로 모델은 고정한다(4.5절).

실패를 예외로 다루지 않는다. 한 종목의 JSON 파싱이 깨졌다고 나머지 14종목을 버리면
그 주 시그널이 통째로 사라진다. 실패는 기록하고 넘어간다.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic import BaseModel, Field, ValidationError

from src.agents.context import SymbolContext
from src.agents.prompts import prompt_version, system_prompt
from src.agents.schema import Direction
from src.llm.client import LLMCall, LLMClient, build_messages

AGENT_NAME = "baseline"
TIER = "deep"
THINKING = True

# **max_tokens는 추론 토큰까지 포함한 총량이다** (2026-09-16 실측).
# 1600으로 뒀더니 1600 전부가 추론에 쓰이고 본문이 빈 문자열로 왔다.
# thinking 모드에서는 추론이 수천 토큰을 쓰므로 넉넉히 준다.
MAX_TOKENS = 8000


class BaselineResponse(BaseModel):
    """LLM이 돌려줘야 하는 형태. 스키마를 벗어나면 그 응답은 버린다."""
    direction: Direction
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = ""
    counter_rationale: str = ""
    key_evidence: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class BaselineResult:
    context: SymbolContext
    response: BaselineResponse | None
    call: LLMCall | None
    user_payload: str
    raw_response: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.response is not None


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def _parse(text: str) -> BaselineResponse:
    """응답에서 JSON 객체를 꺼낸다.

    `json_object` 모드를 쓰더라도 코드펜스나 앞뒤 설명이 섞여 오는 경우가 있어
    방어적으로 처리한다. **관대하게 읽되 스키마는 엄격하게** 검증한다.
    """
    cleaned = _FENCE.sub("", text).strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("응답에서 JSON 객체를 찾지 못했습니다.") from None
        payload = json.loads(cleaned[start:end + 1])
    if not isinstance(payload, dict):
        raise ValueError(f"JSON 객체가 아닙니다: {type(payload).__name__}")
    return BaselineResponse.model_validate(payload)


class BaselineAgent:
    """단일 호출 베이스라인. 컨텍스트 하나를 받아 의견 하나를 낸다."""

    def __init__(self, client: LLMClient | None = None) -> None:
        self._client = client or LLMClient()

    def analyze(self, context: SymbolContext) -> BaselineResult:
        prefix = system_prompt(context.market)
        payload = json.dumps(context.to_payload(), ensure_ascii=False,
                             sort_keys=True, default=float)
        messages = build_messages(prefix, payload)

        try:
            text, call = self._client.complete(
                messages, tier=TIER, thinking=THINKING, json_mode=True,
                max_tokens=MAX_TOKENS)
        except Exception as e:                      # 벤더 오류·타임아웃
            return BaselineResult(context, None, None, payload, "",
                                  error=f"{type(e).__name__}: {e}")

        if call.finish_reason == "length":
            # 잘렸다. 스키마 오류로 뭉뚱그리면 원인을 못 찾는다 — 따로 표시한다.
            return BaselineResult(
                context, None, call, payload, text,
                error=f"truncated: max_tokens={MAX_TOKENS} 소진 "
                      f"(추론 {call.reasoning_tokens} + 본문 "
                      f"{call.completion_tokens - call.reasoning_tokens})")

        try:
            return BaselineResult(context, _parse(text), call, payload, text)
        except (ValueError, ValidationError, json.JSONDecodeError) as e:
            # 응답은 왔는데 형태가 틀렸다. 호출 기록은 남긴다 — 비용은 이미 나갔다.
            return BaselineResult(context, None, call, payload, text,
                                  error=f"{type(e).__name__}: {str(e)[:200]}")


def run_market(contexts: list[SymbolContext], *, market: str, as_of: datetime,
               store, agent: BaselineAgent | None = None,
               universe_version: str = "") -> dict:
    """한 시장 한 시점을 통째로 돌리고 **전량 저장**한다.

    실패한 종목은 건너뛰되 호출 기록은 남는다. 어디서 몇 건이 깨졌는지를 나중에
    셀 수 있어야 한다.
    """
    agent = agent or BaselineAgent()
    prefix = system_prompt(market)
    prompt_hash = store.register_prompt(market, prefix)
    run = store.start_run(AGENT_NAME, market, as_of, prompt_version(market),
                          universe_version)

    results, ok = [], 0
    for ctx in contexts:
        result = agent.analyze(ctx)
        results.append(result)

        signal_id = None
        if result.ok:
            signal_id = store.add_signal(
                run, symbol=ctx.symbol, market=market, as_of=as_of,
                reference_date=ctx.base_date, reference_price=ctx.base_price,
                direction=result.response.direction.value,
                confidence=result.response.confidence,
                model_versions=[result.call.reported_model] if result.call else [],
                estimated_cost_usd=result.call.estimated_cost_usd if result.call else None,
            )
            store.add_opinion(
                signal_id, agent=AGENT_NAME,
                direction=result.response.direction.value,
                confidence=result.response.confidence,
                rationale=result.response.rationale,
                counter_rationale=result.response.counter_rationale,
                data_refs=ctx.data_refs,
                llm_call_ids=[result.call.call_id] if result.call else [],
            )
            ok += 1

        if result.call is not None:
            store.add_call(result.call, run_id=run.run_id, signal_id=signal_id,
                           agent=AGENT_NAME, prompt_hash=prompt_hash,
                           user_payload=result.user_payload,
                           response=result.raw_response)

    failures = [f"{r.context.symbol}: {r.error}" for r in results if not r.ok]
    store.finish_run(run, attempted=len(contexts), succeeded=ok,
                     notes="; ".join(failures)[:2000] or None)
    return {"run_id": run.run_id, "attempted": len(contexts), "succeeded": ok,
            "results": results, "failures": failures}
