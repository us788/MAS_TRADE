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


# ── 신뢰구간 ────────────────────────────────────────────────────────────

def test_t분위수가_표값과_맞는다():
    """작은 표본에서 정규 근사를 쓰면 구간을 좁게 잡는다 — 그때가 필요한 때다."""
    assert math.isclose(m._t_quantile(0.975, 14), 2.1448, abs_tol=5e-4)
    assert math.isclose(m._t_quantile(0.975, 29), 2.0452, abs_tol=5e-4)
    assert m._t_quantile(0.975, 10_000) < 1.97      # 표본이 크면 z로 수렴


def test_적중률_신뢰구간이_표본을_드러낸다():
    """같은 0.4라도 n=15와 n=300은 다른 말이다."""
    small = m.proportion_ci(6, 15)
    large = m.proportion_ci(120, 300)
    assert small[0] < 0.5 < small[1]                # 동전과 구분되지 않는다
    assert large[1] < 0.5                           # n=300이면 구분된다
    assert (small[1] - small[0]) > (large[1] - large[0])


def test_비율_신뢰구간은_0과_1을_넘지_않는다():
    lo, hi = m.proportion_ci(0, 10)
    assert lo == 0.0 and 0 < hi < 1
    lo, hi = m.proportion_ci(10, 10)
    assert hi == 1.0 and 0 < lo < 1
    assert m.proportion_ci(0, 0) is None
    assert m.proportion_ci(11, 10) is None


def test_평균_신뢰구간은_표본이_1이면_None이다():
    assert m.mean_ci([0.01]) is None
    assert m.mean_ci([]) is None
    lo, hi = m.mean_ci([0.01, -0.02, 0.03, -0.01, 0.02])
    assert lo < 0 < hi
    assert m.mean_ci([0.01, None, -0.02, 0.03, None, -0.01, 0.02]) is not None


# ── 다중검정 보정 ───────────────────────────────────────────────────────

def test_DSR은_시행을_늘릴수록_같은_샤프를_깎는다():
    """1,116가지를 다 계산하고 최고를 고르면 그 최고는 최댓값 통계일 수 있다."""
    import random
    rnd = random.Random(3)
    few = [rnd.gauss(0, 0.12) for _ in range(20)]
    many = [rnd.gauss(0, 0.12) for _ in range(1116)]
    assert m.deflated_sharpe(0.45, many, 60) < m.deflated_sharpe(0.45, few, 60)


def test_DSR은_잴_수_없으면_None이다():
    assert m.deflated_sharpe(0.5, [0.1], 60) is None          # 시행이 하나
    assert m.deflated_sharpe(0.5, [0.1, 0.1, 0.1], 60) is None  # 분산이 0
    assert m.deflated_sharpe(0.5, [0.1, 0.2], 1) is None      # 관측이 하나


def test_PBO는_진짜_엣지가_있으면_낮아진다():
    """엣지 없는 시행들 사이에서 고르면 IS 1등이 OOS 중앙값 아래로 자주 떨어진다."""
    import random
    def make(seed, edge):
        rnd = random.Random(seed)
        return [[rnd.gauss(0.010 if k == 0 and edge else 0.0, 0.02)
                 for _ in range(64)] for k in range(40)]
    noise = [m.pbo(make(s, False))["pbo"] for s in range(6)]
    real = [m.pbo(make(s, True))["pbo"] for s in range(6)]
    assert sum(real) / len(real) < sum(noise) / len(noise)


def test_PBO는_입력이_어긋나면_None이다():
    series = [[0.01] * 8, [0.02] * 8]
    assert m.pbo(series, splits=7) is None      # 홀수는 반으로 못 가른다
    assert m.pbo(series, splits=16) is None     # 관측보다 조각이 많다
    assert m.pbo([[0.01] * 8]) is None          # 시행이 하나면 순위가 없다
    assert m.pbo([[0.01] * 8, [0.02] * 4]) is None   # 길이가 다르다
