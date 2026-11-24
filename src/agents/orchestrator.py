"""멀티 에이전트 한 회차 — 에이전트 → 집계 → 리스크 게이트 → 시그널.

기획서 8절의 호출 구조를 묶는 자리다. 부품은 전부 따로 있었고 **이을 경로가
없어서** 회차를 못 돌렸다.

**베이스라인과 같은 저장소에 다른 `kind`로 쌓인다.** 읽는 쪽이 기본으로
`baseline`만 보므로 섞이지 않는다. 그래야 기획서 12절의 "구조가 단일 호출을
이기는가"를 **같은 시점·같은 유니버스·같은 기준가**로 비교할 수 있다.

## 순서가 두 단계인 이유

종목 하나씩 끝까지 처리할 수 없다. **섹터 집중은 종목 하나만 봐서는 못 판정한다** —
"이 섹터에 이미 매수가 셋"인지는 그 시장 전체의 집계가 나와야 안다. 그래서

    1단계  전 종목 × 전 에이전트 호출 → 의견 → 종목별 집계
    2단계  시장 전체 집계를 리스크 게이트에 통째로 넣고 판정 → 저장

이다. 1단계에서 돈이 다 나가므로 **호출 기록은 2단계 실패와 무관하게 남긴다.**

## 데이터 소스를 들고 들어가지 않는다

매크로와 재무는 **호출부가 조립해서 넘긴다**(CLAUDE.md 3절: "원본 소스를 직접
조회하는 코드를 에이전트에 넣지 않는다"). 이 모듈은 `MacroStore`도 `DartStore`도
모른다. as-of 판정은 넘기기 전에 이미 끝나 있어야 한다.

## 순차 호출이다

KR은 종목 15 × 에이전트 4 = **60회**, US는 45회다. 베이스라인(15회)보다 오래 걸린다.
병렬로 접으면 빨라지지만 `LLMClient`의 세션 공유가 스레드 안전인지 확인되지 않았고,
**돈이 나가는 경로에 미검증 동시성을 넣지 않는다**(11-22에 한 번 데였다).
주 1회 배치라 벽시계 시간이 병목이 아니다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

from src.agents.aggregate import (Aggregate, DEFAULT_RULE, RiskEngine, RiskInput,
                                  Rule, aggregate, apply_verdict)
from src.agents.analyst_prompts import analyst_system_prompt
from src.agents.analysts import ANALYSTS, Analyst, AnalystResult, analysts_for
from src.agents.context import SymbolContext
from src.agents.schema import RiskVerdict

#: 이 트랙의 이름. **베이스라인과 섞이지 않게 하는 유일한 장치다.**
#: 프롬프트나 에이전트 구성을 바꾸면 `-v2`로 올린다 — 그 전후 성적을 섞으면 안 된다.
AGENTS_KIND = "agents-v1"


@dataclass
class Opinion:
    """집계가 보는 최소 형태. `AnalystResult`에서 성공한 것만 추린 것이다."""
    agent: str
    symbol: str
    direction: str
    confidence: float
    rationale: str = ""
    counter_rationale: str = ""
    call_id: str | None = None


@dataclass
class SymbolOutcome:
    """종목 하나의 전말. **실패한 에이전트도 들고 있다** — 전량 로깅."""
    context: SymbolContext
    results: list[AnalystResult] = field(default_factory=list)
    opinions: list[Opinion] = field(default_factory=list)
    agg: Aggregate | None = None
    verdict: RiskVerdict | None = None
    signal_id: str | None = None

    @property
    def failures(self) -> list[str]:
        return [f"{self.context.symbol}/{r.agent}: {r.error}"
                for r in self.results if not r.ok]

    @property
    def ok(self) -> bool:
        """**의견이 하나라도 서면 시그널을 만든다.**

        전원 실패만 실패로 센다. 일부가 빠져도 종목을 버리지 않는 이유는 리스크
        게이트와 같다 — 버리면 그 종목이 표본에서 사라져 **채점 분모가 회차마다
        달라진다.** 누가 빠졌는지는 `opinions` 테이블에 남으므로 사후에 "넷 다
        산 회차만" 같은 재채점이 가능하다(LLM 호출 0회).
        """
        return bool(self.opinions)


def prompt_version_label(market: str) -> str:
    """회차에 남길 프롬프트 버전. **에이전트별 해시를 한 줄로 묶는다.**

    `runs.prompt_version`이 칼럼 하나라 그렇다. 하나라도 바뀌면 문자열이 바뀌므로
    경계를 긋는 목적에는 충분하다.
    """
    from src.agents.analysts import prompt_versions
    return ",".join(f"{k}={v}" for k, v in sorted(prompt_versions(market).items()))


def collect_opinions(context: SymbolContext, agents: Sequence[Analyst], *,
                     macro: dict | None = None,
                     fundamentals: dict | None = None) -> SymbolOutcome:
    """종목 하나에 에이전트들을 돌린다. **실패해도 계속 간다.**"""
    out = SymbolOutcome(context=context)
    for agent in agents:
        result = agent.analyze(context, macro, fundamentals=fundamentals)
        out.results.append(result)
        if result.ok:
            out.opinions.append(Opinion(
                agent=result.agent, symbol=context.symbol,
                direction=result.response.direction.value,
                confidence=result.response.confidence,
                rationale=result.response.rationale,
                counter_rationale=result.response.counter_rationale,
                call_id=result.call.call_id if result.call else None,
            ))
    return out


def risk_inputs(contexts: Sequence[SymbolContext]) -> dict[str, RiskInput]:
    """게이트가 보는 지표. **가격 시계열이 아니라 이미 계산된 값만** 넘긴다."""
    out = {}
    for c in contexts:
        ind = c.indicators or {}
        out[c.symbol] = RiskInput(
            symbol=c.symbol, sector=c.sector,
            volatility=ind.get("annualized_volatility_60d"),
            index_volatility=ind.get("index_annualized_volatility_60d"))
    return out


def run_market_agents(contexts: list[SymbolContext], *, market: str,
                      as_of: datetime, store, macro: dict | None = None,
                      fundamentals: dict[str, dict] | None = None,
                      client=None, rule: Rule = DEFAULT_RULE,
                      engine: RiskEngine | None = None,
                      kind: str = AGENTS_KIND,
                      universe_version: str = "") -> dict:
    """한 시장 한 시점을 통째로 돌리고 전량 저장한다.

    `fundamentals`는 종목코드 → 재무 묶음. 없는 종목은 빈 채로 간다(US 전부).
    """
    fundamentals = fundamentals or {}
    engine = engine or RiskEngine()
    classes = analysts_for(market)
    agents = [cls(client) for cls in classes.values()]

    # 프리픽스를 먼저 등록한다. 호출 기록이 어떤 프롬프트로 났는지 되짚을 수 있어야 한다.
    hashes = {
        name: store.register_prompt(
            market, analyst_system_prompt(name, market if cls.market_specific else None))
        for name, cls in classes.items()
    }

    run = store.start_run(kind, market, as_of, prompt_version_label(market),
                          universe_version)

    # ---- 1단계: 호출과 집계 ----
    outcomes: list[SymbolOutcome] = []
    for ctx in contexts:
        out = collect_opinions(ctx, agents, macro=macro,
                               fundamentals=fundamentals.get(ctx.symbol))
        out.agg = aggregate(out.opinions, rule) if out.opinions else None
        outcomes.append(out)

    # ---- 2단계: 게이트는 시장 전체를 본다 ----
    aggregates = [o.agg for o in outcomes if o.agg is not None]
    verdicts = engine.evaluate(aggregates, risk_inputs(contexts))

    # ---- 저장 ----
    ok = 0
    for out in outcomes:
        signal_id = None
        if out.agg is not None:
            verdict = verdicts.get(out.agg.symbol) or RiskVerdict(approved=True)
            out.verdict = verdict
            direction = apply_verdict(out.agg, verdict)
            calls = [r.call for r in out.results if r.call is not None]
            signal_id = store.add_signal(
                run, symbol=out.context.symbol, market=market, as_of=as_of,
                reference_date=out.context.base_date,
                reference_price=out.context.base_price,
                direction=direction.value,
                confidence=out.agg.confidence,
                dispersion=out.agg.dispersion,
                risk_approved=verdict.approved,
                risk_reasons=verdict.reasons,
                model_versions=sorted({c.reported_model for c in calls}),
                estimated_cost_usd=sum(c.estimated_cost_usd or 0.0 for c in calls),
            )
            out.signal_id = signal_id
            for op in out.opinions:
                store.add_opinion(
                    signal_id, agent=op.agent, direction=op.direction,
                    confidence=op.confidence, rationale=op.rationale,
                    counter_rationale=op.counter_rationale,
                    data_refs=out.context.data_refs,
                    llm_call_ids=[op.call_id] if op.call_id else [])
            ok += 1

        # **호출 기록은 시그널이 섰든 안 섰든 남긴다.** 비용은 이미 나갔다.
        for r in out.results:
            if r.call is not None:
                store.add_call(r.call, run_id=run.run_id, signal_id=signal_id,
                               agent=r.agent, prompt_hash=hashes.get(r.agent),
                               user_payload=r.payload, response=r.raw_text)

    failures = [f for out in outcomes for f in out.failures]
    store.finish_run(run, attempted=len(contexts), succeeded=ok,
                     notes="; ".join(failures)[:2000] or None)
    return {"run_id": run.run_id, "attempted": len(contexts), "succeeded": ok,
            "outcomes": outcomes, "failures": failures,
            "agents": sorted(classes)}
