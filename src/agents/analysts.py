"""분석 에이전트 — 기획서 8절의 병렬 레이어.

**각 에이전트는 컨텍스트의 일부만 본다.** 그게 베이스라인(전부 한 번에)과의 유일한
구조적 차이고, 기획서 12절이 묻는 "구조가 단일 호출을 이기는가"의 대상이다.
슬라이스가 넓어지면 "구조가 이겼나 정보가 이겼나"가 분리되지 않는다.

**세 에이전트가 베이스라인의 입력을 나눠 갖는다.**

    기술적   지표 (가격 흐름)        ← 뉴스·거시 안 봄
    뉴스     기사 + 기준가·최근수익률  ← 지표 흐름 안 봄
    거시     매크로 레짐 + 민감도      ← 뉴스·가격 흐름 안 봄

겹치는 부분이 조금 있다. 뉴스 에이전트가 `returns`를 받는 것은 **그 뉴스가 이미
주가에 반영됐는지** 가늠하라는 것이고, 거시가 `relative_strength`를 받는 것은
레짐 전달 여부를 보라는 것이다. **각 겹침은 그 축의 판단에 필요해서 넣은 것이고,
그 외는 주지 않는다.**

의견은 **개별 저장**한다(`opinions` 테이블). 그래야 CLAUDE.md가 말한
**"에이전트 부분집합 사후 재채점(무료 ablation)"**이 가능하다 — 집계 규칙을 바꾸거나
"거시만 뺀" 조합을 다시 채점하는 데 LLM 호출이 0회다.

**펀더멘털은 KR만 있다.** EDGAR가 IP 차단으로 막혀 US 재무를 못 받는다.
그래서 KR은 네 관점, US는 세 관점으로 돈다. **이 비대칭은 고정하기로 했다**
(2026-11-25) — 두 시장 성적은 원래 같은 기준으로 비교하지 않기 때문이다
(CLAUDE.md 4절: "US와 KR은 각자 현지통화로 끝까지 따로 본다").
실험은 시장 **안에서** 멀티 에이전트 대 베이스라인이고 거기엔 영향이 없다.

수급은 아직 어댑터가 없다(`docs/HANDOFF.md` 미결 9번).
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


class Analyst:
    """에이전트 공통. **차이는 프롬프트와 슬라이스 둘뿐이다.**"""

    name: str = ""
    market_specific: bool = False

    #: 이 에이전트를 돌릴 시장. None이면 전 시장.
    markets: tuple[str, ...] | None = None

    def __init__(self, client: LLMClient | None = None) -> None:
        self._client = client or LLMClient()

    # ---- 하위 클래스가 채운다 ----

    def slice(self, context: SymbolContext, macro: dict | None = None, *,
              fundamentals: dict | None = None) -> dict:
        raise NotImplementedError

    # ---- 공통 ----

    def _head(self, context: SymbolContext) -> dict:
        """모든 슬라이스가 공유하는 식별부. 종목이 뭔지는 알아야 한다."""
        return {"symbol": context.symbol, "name": context.name,
                "sector": context.sector, "market": context.market,
                "as_of": context.as_of.isoformat()}

    def prompt_version(self, market: str) -> str:
        return analyst_prompt_version(self.name, market if self.market_specific else None)

    def analyze(self, context: SymbolContext, macro: dict | None = None, *,
                fundamentals: dict | None = None) -> AnalystResult:
        prefix = analyst_system_prompt(
            self.name, context.market if self.market_specific else None)
        payload = json.dumps(self.slice(context, macro, fundamentals=fundamentals),
                             ensure_ascii=False,
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
            return AnalystResult(self.name, context, None, call, payload, text,
                                 error=f"truncated: max_tokens={MAX_TOKENS} 소진")

        try:
            return AnalystResult(self.name, context, _parse(text), call, payload, text)
        except (ValueError, ValidationError, json.JSONDecodeError) as e:
            return AnalystResult(self.name, context, None, call, payload, text,
                                 error=f"{type(e).__name__}: {str(e)[:200]}")


class TechnicalAnalyst(Analyst):
    """가격 흐름. **뉴스도 거시도 보지 않는다.**"""

    name = "technical"

    #: 이 에이전트의 축. `beta_120d`와 지수 변동성은 **거시의 민감도 축**이라 뺀다.
    KEYS = ("bars", "returns", "relative_strength", "ma_gap", "volume_ratio_20d",
            "drawdown_from_1y_peak", "annualized_volatility_60d", "index_returns")

    def slice(self, context: SymbolContext, macro: dict | None = None, *,
              fundamentals: dict | None = None) -> dict:
        ind = context.indicators or {}
        return {
            **self._head(context),
            "reference": {"date": context.base_date.isoformat(),
                          "close": context.base_price},
            "indicators": {k: ind.get(k) for k in self.KEYS},
        }


class NewsAnalyst(Analyst):
    """기사. **지표 흐름을 보지 않는다.**

    `returns`만 예외로 받는다 — **그 뉴스가 이미 반영됐는지** 가늠하는 데 필요하다.
    이동평균·거래량·낙폭은 주지 않는다.
    """

    name = "news"
    market_specific = True

    #: 반영 여부 판단에 필요한 최소 창.
    RETURN_KEYS = ("5d", "20d")

    def slice(self, context: SymbolContext, macro: dict | None = None, *,
              fundamentals: dict | None = None) -> dict:
        returns = (context.indicators or {}).get("returns") or {}
        return {
            **self._head(context),
            "reference": {"date": context.base_date.isoformat(),
                          "close": context.base_price},
            "returns": {k: returns.get(k) for k in self.RETURN_KEYS},
            "news": [n.to_payload() for n in context.news],
        }


class MacroAnalyst(Analyst):
    """거시 레짐 ↔ 종목 민감도. **뉴스도 가격 흐름도 보지 않는다.**"""

    name = "macro"

    #: 레짐이 이 종목에 얼마나 전달되는지를 재는 축.
    KEYS = ("beta_120d", "annualized_volatility_60d",
            "index_annualized_volatility_60d", "relative_strength")

    def slice(self, context: SymbolContext, macro: dict | None = None, *,
              fundamentals: dict | None = None) -> dict:
        ind = context.indicators or {}
        return {
            **self._head(context),
            "macro": macro or {},
            "sensitivity": {k: ind.get(k) for k in self.KEYS},
        }


class FundamentalAnalyst(Analyst):
    """재무제표. **뉴스도 가격 흐름도 거시도 보지 않는다.**

    **가격을 주지 않는다.** 밸류에이션을 보려면 주식 수가 있어야 PER·PBR이 나오는데
    DART 재무 응답에는 없다. 기준가만 주면 LLM이 시가총액을 **기억에서** 끌어와
    채우고, 그건 시점 정합성 위반이면서 검증도 불가능하다. 그래서 이 에이전트의
    축은 "싸다/비싸다"가 아니라 **"좋아지는 중인가"**다.

    이 때문에 다른 셋과 달리 **베이스라인이 안 보는 재료**를 본다. 베이스라인은
    재무를 받지 않는다. 그래서 KR에서 멀티 에이전트가 이기면 "구조가 이긴 것"과
    "재무라는 정보가 더해진 것"이 섞인다 — **의견을 개별 저장하므로 사후에
    펀더멘털만 빼고 재채점하면 분리된다**(LLM 호출 0회).
    """

    name = "fundamental"
    market_specific = True
    markets = ("KR",)

    def slice(self, context: SymbolContext, macro: dict | None = None, *,
              fundamentals: dict | None = None) -> dict:
        return {**self._head(context), "financials": fundamentals or {}}


#: 지금 돌릴 수 있는 에이전트. 어댑터가 생기면 여기에 붙인다.
ANALYSTS: dict[str, type[Analyst]] = {
    TechnicalAnalyst.name: TechnicalAnalyst,
    NewsAnalyst.name: NewsAnalyst,
    MacroAnalyst.name: MacroAnalyst,
    FundamentalAnalyst.name: FundamentalAnalyst,
}


def analysts_for(market: str) -> dict[str, type[Analyst]]:
    """그 시장에서 돌릴 에이전트. 펀더멘털은 KR만이다."""
    return {name: cls for name, cls in ANALYSTS.items()
            if cls.markets is None or market in cls.markets}


def prompt_versions(market: str) -> dict[str, str]:
    """에이전트별 프롬프트 해시. 시그널 로그에 남겨 경계를 긋는다.

    **시장마다 키 집합이 다르다** — US에는 펀더멘털이 없다.
    """
    return {
        name: analyst_prompt_version(name, market if cls.market_specific else None)
        for name, cls in analysts_for(market).items()
    }
