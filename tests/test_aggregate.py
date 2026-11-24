"""집계와 리스크 게이트 검증. **LLM도 네트워크도 타지 않는다.**

둘 다 결정론적이어야 한다. 집계를 LLM에 맡기면 그것이 네 번째 에이전트가 되고
"구조가 이겼나"와 "집계 모델이 좋았나"가 분리되지 않는다.
"""
from dataclasses import dataclass

import pytest

from src.agents.aggregate import (DEFAULT_RULE, MAX_BUYS_PER_SECTOR,
                                  MAX_DISPERSION, MAX_VOL_RATIO, Aggregate,
                                  RiskEngine, RiskInput, aggregate,
                                  apply_verdict, dispersion)
from src.agents.schema import Direction


@dataclass
class O:
    agent: str
    symbol: str
    direction: str
    confidence: float


def ops(symbol, *pairs):
    """(에이전트, 방향, 확신도) 묶음."""
    return [O(a, symbol, d, c) for a, d, c in pairs]


def three(symbol, *dirs, conf=0.6):
    return ops(symbol, *[(a, d, conf) for a, d in
                         zip(("technical", "news", "macro"), dirs)])


# ---- 기본 규칙 ----

def test_기본은_다수결이다():
    """**확신도 가중을 기본으로 쓰지 않는다** (2026-11-23 측정).
    확신도의 잡음/신호가 0.52라 그걸로 가중하면 잡음을 증폭한다.
    """
    assert DEFAULT_RULE == "majority"


def test_전원일치면_방향이_서고_분산이_0이다():
    a = aggregate(three("A", "sell", "sell", "sell"))
    assert a.direction is Direction.SELL and a.dispersion == 0.0


def test_동수면_hold다():
    """못 정한 것을 정한 척하지 않는다."""
    a = aggregate(ops("A", ("technical", "buy", 0.6), ("news", "sell", 0.6)))
    assert a.direction is Direction.HOLD


def test_삼분할은_어떤_규칙에서도_hold다():
    o = three("A", "buy", "hold", "sell")
    for rule in ("majority", "confidence_weighted", "unanimous"):
        assert aggregate(o, rule).direction is Direction.HOLD


def test_동의_비율이_확신도를_깎는다():
    """셋 중 둘이 매수면 만장일치보다 낮아야 한다."""
    full = aggregate(three("A", "buy", "buy", "buy")).confidence
    part = aggregate(three("A", "buy", "buy", "hold")).confidence
    assert part < full


def test_만장일치_규칙은_더_보수적이다():
    o = three("A", "sell", "sell", "hold")
    assert aggregate(o, "majority").direction is Direction.SELL
    assert aggregate(o, "unanimous").direction is Direction.HOLD


def test_의견이_없으면_시그널을_만들지_않는다():
    assert aggregate([]) is None


def test_모르는_규칙은_거부한다():
    with pytest.raises(ValueError):
        aggregate(three("A", "buy", "buy", "buy"), "없는규칙")


def test_에이전트별_표가_남는다():
    """사후에 어느 조합이 어떻게 갈렸는지 되짚을 수 있어야 한다 (무료 ablation)."""
    a = aggregate(three("A", "buy", "hold", "sell"))
    assert a.votes == {"technical": "buy", "news": "hold", "macro": "sell"}


def test_분산이_갈림의_정도를_잰다():
    assert dispersion(three("A", "buy", "buy", "buy")) == 0.0
    assert dispersion(three("A", "buy", "hold", "sell")) > 0.8


# ---- 리스크 게이트 ----

def _inputs(symbols, sector="반도체", vol=0.5, idx=0.45):
    return {s: RiskInput(s, sector, vol, idx) for s in symbols}


def test_전원일치는_통과한다():
    aggs = [aggregate(three("A", "buy", "buy", "buy"))]
    v = RiskEngine().evaluate(aggs, _inputs("A"))
    assert v["A"].approved and apply_verdict(aggs[0], v["A"]) is Direction.BUY


def test_의견이_갈리면_기각한다():
    """**갈린 판단을 밀어붙이는 것은 잡음을 거래하는 일이다** (11-23 노이즈 측정)."""
    aggs = [aggregate(three("A", "buy", "hold", "sell"))]
    v = RiskEngine().evaluate(aggs, _inputs("A"))
    assert not v["A"].approved and "dispersion" in v["A"].rule_ids


def test_변동성은_지수_대비로_판정한다():
    """절대값으로 못 건다 — KR 지수가 38~49%고 개별주가 42~83%다(실측)."""
    aggs = [aggregate(three("A", "buy", "buy", "buy"))]
    inp = {"A": RiskInput("A", "반도체", 0.45 * MAX_VOL_RATIO + 0.01, 0.45)}
    v = RiskEngine().evaluate(aggs, inp)
    assert not v["A"].approved and "volatility" in v["A"].rule_ids


def test_섹터_상한을_넘기면_확신도_낮은_것부터_기각한다():
    aggs = [aggregate(three(s, "buy", "buy", "buy", conf=c))
            for s, c in zip("ABCD", (0.9, 0.3, 0.8, 0.7))]
    v = RiskEngine().evaluate(aggs, _inputs("ABCD"))
    assert not v["B"].approved                       # 확신도 0.3이 잘린다
    assert all(v[s].approved for s in "ACD")
    assert "sector_concentration" in v["B"].rule_ids


def test_다른_섹터는_따로_센다():
    aggs = [aggregate(three(s, "buy", "buy", "buy")) for s in "ABCD"]
    inp = {**_inputs("ABC", sector="반도체"), **_inputs("D", sector="금융")}
    v = RiskEngine().evaluate(aggs, inp)
    assert all(v[s].approved for s in "ABCD")        # 반도체 3 + 금융 1


def test_매도는_섹터_상한을_타지_않는다():
    """상한은 **매수 집중**을 막는 것이다. 전원 매도는 현금으로 가는 길이다."""
    aggs = [aggregate(three(s, "sell", "sell", "sell")) for s in "ABCDE"]
    v = RiskEngine().evaluate(aggs, _inputs("ABCDE"))
    assert all(v[s].approved for s in "ABCDE")


def test_기각은_시그널을_지우지_않고_hold로_바꾼다():
    """지우면 그 종목이 표본에서 사라져 채점 분모가 회차마다 달라진다."""
    agg = aggregate(three("A", "buy", "hold", "sell"))
    v = RiskEngine().evaluate([agg], _inputs("A"))
    assert apply_verdict(agg, v["A"]) is Direction.HOLD
    assert agg.direction is Direction.HOLD or agg.symbol == "A"   # 원본은 남는다


def test_기각_사유가_전부_기록된다():
    """사후에 '왜 그 주에 안 샀나'를 되짚을 수 있어야 한다."""
    agg = aggregate(three("A", "buy", "hold", "sell"))
    inp = {"A": RiskInput("A", "반도체", 0.45 * MAX_VOL_RATIO + 0.01, 0.45)}
    v = RiskEngine().evaluate([agg], inp)
    assert len(v["A"].reasons) == 2                  # 분산 + 변동성
    assert set(v["A"].rule_ids) == {"dispersion", "volatility"}


def test_지표가_없으면_변동성으로_기각하지_않는다():
    """`null`은 '0'이 아니라 '알 수 없음'이다. 모르는 것으로 기각하지 않는다."""
    aggs = [aggregate(three("A", "buy", "buy", "buy"))]
    v = RiskEngine().evaluate(aggs, {"A": RiskInput("A", "반도체", None, None)})
    assert v["A"].approved


# ---- 짝수 구성: KR은 펀더멘털이 붙어 넷이다 (2026-11-25) ----

def four(symbol, *dirs, conf=0.6):
    return ops(symbol, *[(a, d, conf) for a, d in
                         zip(("technical", "news", "macro", "fundamental"), dirs)])


def test_넷이_둘씩_갈리면_hold다():
    """**KR은 에이전트가 넷이라 2-2가 실제로 나온다.** 홀수 전제로 짠 다수결이
    동수를 임의로 깨면 안 된다 — 못 정한 것은 못 정한 것이다.
    """
    assert aggregate(four("A", "buy", "buy", "sell", "sell")).direction is Direction.HOLD
    assert aggregate(four("A", "buy", "buy", "hold", "hold")).direction is Direction.HOLD


def test_넷_중_둘만_같아도_나머지가_갈리면_방향이_선다():
    """2 매수 / 1 보유 / 1 매도는 최다가 하나뿐이라 동수가 아니다."""
    a = aggregate(four("A", "buy", "buy", "hold", "sell"))
    assert a.direction is Direction.BUY
    assert a.confidence == pytest.approx(0.6 * 0.5, abs=1e-4)   # 동의 2/4


def test_펀더멘털은_KR에서만_표를_던진다():
    """US는 셋이다. 집계 자체는 에이전트 수를 모른다 — 들어온 의견만 센다."""
    us = three("A", "buy", "buy", "sell")
    kr = four("A", "buy", "buy", "sell", "sell")
    assert us.__len__() == 3 and kr.__len__() == 4
    assert aggregate(us).direction is Direction.BUY     # 2/3
    assert aggregate(kr).direction is Direction.HOLD    # 2-2
