"""결정론 지표 검증. 순수 함수라 입력·출력만으로 끝난다.

**모르는 것을 0으로 돌려주지 않는지**가 여기서 지킬 핵심이다. 표본이 없는데 0을 주면
"성적이 0"과 "잴 수 없음"이 섞여 집계가 조용히 거짓말을 한다.
"""
import math

from src.compute import metrics as m


def test_기준가가_0이거나_없으면_수익률은_None이다():
    assert m.pct_return(0, 100) is None
    assert m.pct_return(None, 100) is None
    assert m.pct_return(100, None) is None
    assert math.isclose(m.pct_return(100, 110), 0.1)


def test_sell은_부호를_뒤집는다():
    assert m.directional_excess(0.03, "buy") == 0.03
    assert math.isclose(m.directional_excess(0.03, "sell"), -0.03)
    assert math.isclose(m.directional_excess(-0.03, "sell"), 0.03)


def test_hold은_적중_판정에서_빠진다():
    """hold를 어느 쪽으로든 세면 hold가 많은 전략이 적중률을 인위적으로 움직인다."""
    assert m.directional_excess(0.03, "hold") is None
    assert m.hit_rate([0.01, -0.01, None, None]) == 0.5   # 분모는 2


def test_잴_것이_없으면_적중률은_0이_아니라_None이다():
    assert m.hit_rate([]) is None
    assert m.hit_rate([None, None]) is None


def test_상관은_표본_3건_미만이면_None이다():
    assert m.correlation([1, 2], [1, 2])["spearman"] is None
    assert m.correlation([1, 2], [1, 2])["n"] == 2


def test_한쪽이_상수면_상관은_0이_아니라_None이다():
    """확신도를 전부 0.5로 준 시스템은 '상관 0'이 아니라 '잴 수 없음'이다."""
    r = m.correlation([0.5, 0.5, 0.5, 0.5], [0.1, -0.2, 0.3, 0.0])
    assert r["pearson"] is None and r["spearman"] is None and r["n"] == 4


def test_확신도가_수익을_따라가면_상관이_1이다():
    r = m.correlation([0.1, 0.4, 0.7, 0.9], [-0.05, 0.0, 0.03, 0.10])
    assert math.isclose(r["spearman"], 1.0)
    assert r["pearson"] > 0.9


def test_스피어만은_순위만_본다():
    """이상치 하나가 피어슨을 끌고 가도 순위 상관은 버틴다."""
    conf = [0.1, 0.2, 0.3, 0.4]
    ret = [0.01, 0.02, 0.03, 50.0]        # 마지막이 이상치
    r = m.correlation(conf, ret)
    assert math.isclose(r["spearman"], 1.0)


def test_동점은_평균_순위로_센다():
    r = m.correlation([1, 1, 2, 3], [1, 1, 2, 3])
    assert math.isclose(r["spearman"], 1.0)


def test_레짐은_임계값으로_나뉜다():
    assert m.classify_regime(0.10) == "up"
    assert m.classify_regime(-0.10) == "down"
    assert m.classify_regime(0.01) == "flat"
    assert m.classify_regime(None) is None


def test_최대낙폭():
    assert math.isclose(m.max_drawdown([100, 120, 90, 110]), -0.25)  # 120 -> 90
    assert m.max_drawdown([100]) is None


def test_샤프는_변동이_0이면_None이다():
    assert m.sharpe([0.01, 0.01, 0.01]) is None
    assert m.sharpe([0.01, -0.01, 0.02, 0.0]) is not None


def test_소르티노는_하방만_센다():
    """상승 변동을 위험으로 세면 좋은 전략이 벌을 받는다."""
    up_only = [0.01, 0.02, 0.03, 0.05]
    mixed = [0.01, -0.02, 0.03, -0.05]
    assert m.sortino(up_only) is None           # 하방이 없다 -> 잴 수 없다
    assert m.sortino(mixed) is not None


def test_회전율은_편도_기준이다():
    prev = {"A": 0.5, "B": 0.5}
    new = {"A": 0.5, "C": 0.5}
    assert math.isclose(m.turnover(prev, new), 0.5)  # B 전량 매도 -> C 전량 매수
    assert m.turnover(prev, prev) == 0.0
