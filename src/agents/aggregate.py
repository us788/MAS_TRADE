"""의견 집계와 리스크 게이트 — 기획서 8절.

**둘 다 LLM이 아니다.** 집계를 LLM에 맡기면 그것이 네 번째 에이전트가 되고,
"구조가 이겼나"와 "집계 모델이 좋았나"가 분리되지 않는다. 리스크는 기획서가
명시적으로 룰 엔진으로 못박았다 — **LLM이 제안하고 룰이 허가하는 비대칭 구조**다.

**집계 규칙을 바꿔도 LLM 호출이 0회다.** 의견이 `opinions`에 개별 저장되므로
`aggregate(opinions, rule=...)`만 다시 돌리면 된다. CLAUDE.md가 말한 무료 ablation이
여기서 쓰인다 — 노이즈 바닥이 높다는 걸 아는 상태(11-23)라 특히 값지다.
"""
from __future__ import annotations

import statistics as st
from dataclasses import dataclass, field
from typing import Literal, Sequence

from src.agents.schema import Direction, RiskVerdict

#: 방향을 수치로. 집계와 분산 계산이 같은 축을 쓴다.
SCORE = {Direction.BUY: 1.0, Direction.HOLD: 0.0, Direction.SELL: -1.0}

Rule = Literal["majority", "confidence_weighted", "unanimous"]

#: 확신도 가중을 **기본으로 쓰지 않는 이유** (2026-11-23 측정).
#:
#: 같은 구성을 4회 돌려 재니 확신도의 회차 간 표준편차가 0.043, 회차 내가 0.082로
#: **잡음/신호가 0.52**였다. 그 값으로 가중하면 잡음을 증폭한다. 다수결은 방향만
#: 쓰므로 그 오염을 타지 않는다. `confidence_weighted`는 비교군으로만 둔다.
DEFAULT_RULE: Rule = "majority"

#: 이 값 미만이면 방향을 못 정한 것으로 보고 `hold`.
#: 확신도 가중에서만 쓴다 — 다수결은 표 수로 가른다.
WEIGHTED_THRESHOLD = 0.2


@dataclass
class Aggregate:
    """의견들을 묶은 결과. **시그널의 직전 단계다.**"""
    symbol: str
    direction: Direction
    confidence: float
    dispersion: float
    """에이전트 간 의견 분산. 기획서가 '그 자체가 피처다'라고 한 값이다."""
    rule: str
    votes: dict[str, str] = field(default_factory=dict)
    """에이전트별 방향. 사후에 어느 조합이 어떻게 갈렸는지 되짚을 수 있어야 한다."""


def dispersion(opinions: Sequence) -> float:
    """의견 분산. 0이면 전원 일치, 1에 가까우면 정반대로 갈렸다."""
    scores = [SCORE[Direction(o.direction)] for o in opinions]
    return st.pstdev(scores) if len(scores) > 1 else 0.0


def aggregate(opinions: Sequence, rule: Rule = DEFAULT_RULE) -> Aggregate | None:
    """의견들 → 방향 하나. 의견이 없으면 None (시그널을 만들지 않는다)."""
    opinions = [o for o in opinions if o is not None]
    if not opinions:
        return None

    symbol = opinions[0].symbol
    votes = {o.agent: Direction(o.direction).value for o in opinions}
    disp = dispersion(opinions)

    if rule == "majority":
        direction, conf = _majority(opinions)
    elif rule == "confidence_weighted":
        direction, conf = _weighted(opinions)
    elif rule == "unanimous":
        direction, conf = _unanimous(opinions)
    else:
        raise ValueError(f"알 수 없는 집계 규칙: {rule!r}")

    return Aggregate(symbol=symbol, direction=direction, confidence=conf,
                     dispersion=disp, rule=rule, votes=votes)


def _majority(opinions: Sequence) -> tuple[Direction, float]:
    """표가 많은 쪽. **동수면 `hold`다** — 못 정한 것을 정한 척하지 않는다.

    확신도는 **이긴 쪽에 선 에이전트들의 평균에 동의 비율을 곱한다.**
    셋 중 둘이 매수면 만장일치보다 낮게 나와야 한다.
    """
    counts: dict[Direction, list[float]] = {}
    for o in opinions:
        counts.setdefault(Direction(o.direction), []).append(float(o.confidence))
    top = max(counts.values(), key=len)
    winners = [d for d, v in counts.items() if len(v) == len(top)]
    if len(winners) > 1:
        return Direction.HOLD, _hold_confidence(opinions)
    direction = winners[0]
    agree = len(counts[direction]) / len(opinions)
    return direction, round(st.mean(counts[direction]) * agree, 4)


def _weighted(opinions: Sequence) -> tuple[Direction, float]:
    """확신도 가중 순점수. **비교군이다** — 기본으로 쓰지 않는 이유는 위 주석."""
    total = sum(float(o.confidence) for o in opinions)
    if total <= 0:
        return Direction.HOLD, 0.0
    score = sum(SCORE[Direction(o.direction)] * float(o.confidence)
                for o in opinions) / total
    if abs(score) < WEIGHTED_THRESHOLD:
        return Direction.HOLD, round(abs(score), 4)
    return (Direction.BUY if score > 0 else Direction.SELL), round(abs(score), 4)


def _unanimous(opinions: Sequence) -> tuple[Direction, float]:
    """전원이 같아야 방향이 선다. **가장 보수적인 규칙**이고 hold가 많아진다."""
    dirs = {Direction(o.direction) for o in opinions}
    if len(dirs) != 1:
        return Direction.HOLD, _hold_confidence(opinions)
    direction = dirs.pop()
    return direction, round(st.mean([float(o.confidence) for o in opinions]), 4)


def _hold_confidence(opinions: Sequence) -> float:
    """못 정했을 때의 확신도. **낮게 준다** — 갈렸다는 사실 자체가 약한 신호다."""
    return round(st.mean([float(o.confidence) for o in opinions]) * 0.5, 4)


# ---------------------------------------------------------------- 리스크 게이트

#: 지수 대비 변동성 한도. 이 배수를 넘으면 기각한다.
#:
#: 절대값으로 못 건다 — KR 지수 변동성이 38~49%고 개별주가 42~83%다(실측).
#: 시장마다 수준이 달라 **지수 대비 배수**가 유일하게 이식 가능한 기준이다.
MAX_VOL_RATIO = 2.0

#: 한 섹터에 몰릴 수 있는 매수 수. **상관관계의 대용이다.**
#:
#: 같은 섹터 종목은 같이 움직인다. 기획서 8절이 요구한 상관 판정을 제대로 하려면
#: 종목 간 상관행렬이 필요한데(미결), 섹터 집중이 그 싼 대용이다.
MAX_BUYS_PER_SECTOR = 3

#: 의견이 이보다 갈리면 기각한다.
#:
#: 에이전트 셋이 3분할이면 분산이 0.816이다. 그 상태는 "판단이 섰다"가 아니다.
#: 11-23 노이즈 측정에서 재실행만으로 방향이 23% 뒤집히는 것을 봤으므로,
#: **갈린 판단을 밀어붙이는 것은 잡음을 거래하는 일이다.**
MAX_DISPERSION = 0.7

#: **에이전트 수가 바뀌면 같은 숫자가 다른 뜻이 된다** (2026-11-25 실측).
#:
#: 0.7은 셋을 전제로 잡은 값인데, 넷이 되면 분산값의 분포 자체가 바뀐다.
#: 매수 1 · 매도 1 · 보유 2가 정확히 **0.707**이라 0.7이 그 덩어리 한가운데를
#: 가른다. 균등 난수로 전수 열거하면:
#:
#:     n=3  분산값 {0, 0.471, 0.816, 0.943}   기각 12/27 = 44.4%
#:     n=4  분산값 {0, 0.433, 0.5, 0.707, 0.829, 0.866, 1.0}   기각 50/81 = 61.7%
#:
#: 기각은 `hold`로 바뀌므로 그대로 두면 **KR의 실행 가능한 시그널이 29.6% → 12.4%**로
#: 줄어든다(US의 절반 이하). 시장 간 성적을 비교하지 않기로 한 것(11-25 결정)과는
#: **다른 문제다** — KR 안에서 쓸 수 있는 표본이 그만큼 줄어 검정력이 떨어진다.
#:
#: 그래서 **균등 난수 기준 기각률이 비슷해지도록** 의견 수별로 잡는다.
#: 실제 에이전트는 균등 난수가 아니지만, 한쪽 시장만 조용히 빡빡해지는 것보다는
#: 명시적인 기준이 낫다. 표본이 쌓이면 기각된 시그널의 사후 성적으로 보정한다.
#:
#:     n=3  0.70  → 기각 12/27 = 44.4%
#:     n=4  0.75  → 기각 38/81 = 46.9%
#:
#: **의견 수로 고른다** — 에이전트가 빠진 종목은 그 수에 맞는 값을 쓴다.
DISPERSION_LIMIT = {3: 0.70, 4: 0.75}


@dataclass
class RiskInput:
    """게이트가 보는 것. 가격 시계열이 아니라 **이미 계산된 지표**만 받는다."""
    symbol: str
    sector: str
    volatility: float | None
    index_volatility: float | None


class RiskEngine:
    """승인/기각 게이트. **조언이 아니다** (기획서 8절).

    기각 사유는 **코드로 판정하고 전부 기록한다.** 사후에 "왜 그 주에 안 샀나"를
    되짚을 수 있어야 한다.

    기각은 방향을 `hold`로 바꾼다 — 시그널을 지우지 않는다. 지우면 그 종목이
    표본에서 사라져 채점 분모가 회차마다 달라진다.
    """

    def __init__(self, *, max_vol_ratio: float = MAX_VOL_RATIO,
                 max_buys_per_sector: int = MAX_BUYS_PER_SECTOR,
                 max_dispersion: float | None = None) -> None:
        self.max_vol_ratio = max_vol_ratio
        self.max_buys_per_sector = max_buys_per_sector
        #: None이면 의견 수에 맞춰 고른다. 값을 주면 그 값으로 고정한다(테스트·비교군).
        self.max_dispersion = max_dispersion

    def dispersion_limit(self, n_opinions: int) -> float:
        """의견 수에 맞는 분산 상한. 표에 없는 수는 셋 기준으로 떨어진다."""
        if self.max_dispersion is not None:
            return self.max_dispersion
        return DISPERSION_LIMIT.get(n_opinions, MAX_DISPERSION)

    def evaluate(self, aggregates: Sequence[Aggregate],
                 inputs: dict[str, RiskInput]) -> dict[str, RiskVerdict]:
        """시장 하나를 통째로 본다. **섹터 집중은 종목 하나만 봐서는 못 판정한다.**"""
        out: dict[str, RiskVerdict] = {}
        for agg in aggregates:
            reasons, rules = [], []
            ri = inputs.get(agg.symbol)

            limit = self.dispersion_limit(len(agg.votes))
            if agg.dispersion > limit:
                reasons.append(f"의견 분산 {agg.dispersion:.2f} > {limit}")
                rules.append("dispersion")

            if ri and ri.volatility and ri.index_volatility:
                ratio = ri.volatility / ri.index_volatility
                if ratio > self.max_vol_ratio:
                    reasons.append(f"변동성이 지수의 {ratio:.1f}배 > {self.max_vol_ratio}")
                    rules.append("volatility")

            out[agg.symbol] = RiskVerdict(approved=not reasons, reasons=reasons,
                                          rule_ids=rules)

        # 섹터 집중은 **승인된 매수만 놓고** 다시 본다. 이미 기각된 것은 세지 않는다.
        self._apply_sector_cap(aggregates, inputs, out)
        return out

    def _apply_sector_cap(self, aggregates, inputs, verdicts) -> None:
        """섹터당 매수 상한. **확신도가 낮은 쪽부터 기각한다.**"""
        by_sector: dict[str, list[Aggregate]] = {}
        for agg in aggregates:
            if agg.direction is not Direction.BUY or not verdicts[agg.symbol].approved:
                continue
            ri = inputs.get(agg.symbol)
            by_sector.setdefault(ri.sector if ri else "", []).append(agg)

        for sector, group in by_sector.items():
            if len(group) <= self.max_buys_per_sector:
                continue
            group.sort(key=lambda a: a.confidence)          # 낮은 확신도부터
            for agg in group[:len(group) - self.max_buys_per_sector]:
                v = verdicts[agg.symbol]
                v.reasons.append(
                    f"섹터 '{sector}' 매수 {len(group)}건 > {self.max_buys_per_sector}")
                v.rule_ids.append("sector_concentration")
                v.approved = False


def apply_verdict(agg: Aggregate, verdict: RiskVerdict) -> Direction:
    """게이트를 통과한 최종 방향. 기각이면 `hold`다."""
    return agg.direction if verdict.approved else Direction.HOLD
