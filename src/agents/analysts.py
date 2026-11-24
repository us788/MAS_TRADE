"""분석 에이전트 — 기획서 8절의 병렬 레이어.

**각 에이전트는 컨텍스트의 일부만 본다.** 그게 베이스라인(전부 한 번에)과의
유일한 구조적 차이고, 기획서 12절이 묻는 "구조가 단일 호출을 이기는가"의 대상이다.

의견은 **개별 저장**한다(`opinions` 테이블). 그래야 CLAUDE.md가 말한
**"에이전트 부분집합 사후 재채점(무료 ablation)"**이 가능하다 — 집계 규칙을 바꾸거나
"거시만 뺀" 조합을 다시 채점하는 데 LLM 호출이 0회다. 노이즈 바닥이 높다는 걸
아는 상태(11-23)라 공짜로 여러 조합을 볼 수 있는 구조가 특히 값지다.

**지금은 거시 하나다.** 기술적·뉴스는 베이스라인이 이미 쓰는 재료라 나중에 붙이고,
펀더멘털·수급은 어댑터가 없다(`docs/HANDOFF.md` 미결 9번).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import ValidationError

from src.agents.analyst_prompts import analyst_prompt_version, analyst_system_prompt
from src.agents.baseline import MAX_TOKENS, THINKING, TIER, BaselineResponse, _parse
from src.agents.context import SymbolContext
from src.llm.client import LLMCall, LLMClient, build_messages


@dataclass
class AnalystResult:
    """에이전트 한 번의 결과. 실패해도 호출 기록은 남는다 — 비용은 이미 나갔다."""
    agent: str
    context: SymbolContext
    response: BaselineResponse | None
    call: LLMCall | None
    payload: str
    raw_text: str
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.response is not None


class MacroAnalyst:
    """거시 레짐 ↔ 종목 민감도. **뉴스도 가격 흐름도 보지 않는다.**"""

    name = "macro"

    def __init__(self, client: LLMClient | None = None) -> None:
        self._client = client or LLMClient()

    def slice(self, context: SymbolContext, macro: dict) -> dict:
        """이 에이전트가 보는 입력.

        **의도적으로 좁다.** 매크로 레짐과, 그 레짐이 이 종목에 얼마나 전달되는지를
        재는 민감도 지표만 준다. 뉴스·이동평균·거래량은 다른 축의 몫이다.
        """
        ind = context.indicators or {}
        return {
            "symbol": context.symbol,
            "name": context.name,
            "sector": context.sector,
            "market": context.market,
            "as_of": context.as_of.isoformat(),
            "macro": macro,
            "sensitivity": {
                "beta_120d": ind.get("beta_120d"),
                "annualized_volatility_60d": ind.get("annualized_volatility_60d"),
                "index_annualized_volatility_60d":
                    ind.get("index_annualized_volatility_60d"),
                "relative_strength": ind.get("relative_strength"),
            },
        }

    def analyze(self, context: SymbolContext, macro: dict) -> AnalystResult:
        prefix = analyst_system_prompt(self.name)
        payload = json.dumps(self.slice(context, macro), ensure_ascii=False,
                             sort_keys=True, default=float)
        messages = build_messages(prefix, payload)

        try:
            text, call = self._client.complete(
                messages, tier=TIER, thinking=THINKING, json_mode=True,
                max_tokens=MAX_TOKENS)
        except Exception as e:                      # noqa: BLE001  벤더 오류·타임아웃
            return AnalystResult(self.name, context, None, None, payload, "",
                                 error=f"{type(e).__name__}: {e}")

        if call.finish_reason == "length":
            return AnalystResult(
                self.name, context, None, call, payload, text,
                error=f"truncated: max_tokens={MAX_TOKENS} 소진")

        try:
            return AnalystResult(self.name, context, _parse(text), call, payload, text)
        except (ValueError, ValidationError, json.JSONDecodeError) as e:
            return AnalystResult(self.name, context, None, call, payload, text,
                                 error=f"{type(e).__name__}: {str(e)[:200]}")


#: 지금 돌릴 수 있는 에이전트. 어댑터가 생기면 여기에 붙인다.
ANALYSTS = {MacroAnalyst.name: MacroAnalyst}


def prompt_versions() -> dict[str, str]:
    """에이전트별 프롬프트 해시. 시그널 로그에 남겨 경계를 긋는다."""
    return {name: analyst_prompt_version(name) for name in ANALYSTS}
