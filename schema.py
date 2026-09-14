"""에이전트 출력 스키마. 모든 에이전트는 이 형태로만 응답한다."""
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Direction(str, Enum):
    BUY = "buy"
    HOLD = "hold"
    SELL = "sell"


class AgentOpinion(BaseModel):
    """개별 분석 에이전트의 의견 하나. 시그널 로그의 기본 단위."""

    agent: str
    symbol: str
    as_of: datetime
    direction: Direction
    confidence: float = Field(ge=0.0, le=1.0, description="캘리브레이션 검증 대상. 그대로 믿지 않는다")
    rationale: str
    counter_rationale: str = Field(description="자기 의견에 대한 반대 논거. 비워두지 않는다")
    data_refs: list[str] = Field(default_factory=list, description="참조한 스냅샷 식별자")


class RiskVerdict(BaseModel):
    """리스크 엔진 판정. 조언이 아니라 게이트다."""

    approved: bool
    reasons: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)


class Signal(BaseModel):
    """한 종목에 대한 최종 시그널. 이것이 평가의 샘플 1건이다."""

    symbol: str
    market: Literal["US", "KR"]
    as_of: datetime
    reference_price: float
    direction: Direction
    confidence: float = Field(ge=0.0, le=1.0)
    opinions: list[AgentOpinion]
    dispersion: float = Field(ge=0.0, description="에이전트 간 의견 분산. 그 자체가 피처다")
    risk: RiskVerdict
    proposed_weight: float | None = Field(
        default=None,
        description="LLM 제시 비중. 기본 운용은 동일가중이고 이것은 비교군으로만 추적한다",
    )
