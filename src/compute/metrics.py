"""결정론 지표 계산.

**모든 수치 계산은 여기서 한다. LLM 프롬프트 안에서 산술을 시키지 않는다**
(`CLAUDE.md` 3절). LLM이 만든 틀린 숫자는 근거로 로그에 남아 사후 검증까지 오염시킨다.

이 모듈은 순수 함수만 둔다 — DB도 네트워크도 타지 않는다. 그래야 테스트가
입력·출력만으로 끝나고, 채점 결과를 의심할 때 여기부터 좁혀 들어올 수 있다.

**모르면 0이 아니라 None을 돌려준다.** 표본이 없는데 0을 주면 "성적이 0"과
"잴 수 없음"이 섞인다.
"""
from __future__ import annotations

import itertools
import math
from statistics import NormalDist
from typing import Literal, Sequence

# 레짐 분류 임계값. 구간 지수 수익률이 이보다 크면 상승, 작으면 하락, 사이면 횡보.
# 기획서 6절이 "전체 평균은 거짓말을 한다"며 레짐 분리를 요구하지만 경계값은 정하지
# 않았다. ±5%는 선택이다 — 바꾸면 성적 해석이 달라지므로 일지에 남기고 바꾼다.
REGIME_THRESHOLD = 0.05

# **표준편차가 "0"인지 판정하는 문턱.**
# `stdev([0.1, 0.1, 0.1])`은 0이 아니라 1.7e-17을 준다 — 부동소수점 잡음이다.
# `sd == 0`으로만 거르면 변동이 전혀 없는 계열에서 샤프가 무한대로 튀고,
# DSR이 "엣지가 있을 확률 0.9998"을 돌려준다(2026-09-28 테스트가 잡았다).
# 09-18에 가격 수정 이력에서 배운 것과 같은 교훈이다: **등호로 0을 재지 않는다.**
# 수익률은 1e-4 수준이므로 1e-12는 진짜 변동을 절대 삼키지 않는다.
ZERO_VARIANCE_TOL = 1e-12

Regime = Literal["up", "down", "flat"]


def pct_return(start: float | None, end: float | None) -> float | None:
    """단순 수익률. 기준가가 0이거나 없으면 None."""
    if start is None or end is None or start == 0:
        return None
    return end / start - 1.0


def excess_return(symbol_ret: float | None, index_ret: float | None) -> float | None:
    """지수 대비 초과수익.

    **두 값은 반드시 같은 기준으로 계산돼야 한다**(`docs/harness.md` 3.4.1).
    종목만 배당 조정(`close_tr`)하고 지수는 가격지수를 쓰면 고배당주가 공짜 점수를
    얻는다 — XOM 3년에 14.9%p였다. 채점기는 양쪽 다 `close_px`로 넘긴다.
    """
    if symbol_ret is None or index_ret is None:
        return None
    return symbol_ret - index_ret


def directional_excess(excess: float | None, direction: str) -> float | None:
    """시그널대로 했을 때의 초과수익.

    `buy`는 초과수익 그대로, `sell`은 부호를 뒤집는다(덜 오르면 맞힌 것).
    `hold`는 None — 적중 판정에서 빼고 비율만 따로 센다. `hold`를 어느 쪽으로든
    세면 `hold`가 많은 전략이 적중률을 인위적으로 움직인다.
    """
    if excess is None:
        return None
    if direction == "buy":
        return excess
    if direction == "sell":
        return -excess
    if direction == "hold":
        return None
    raise ValueError(f"알 수 없는 방향: {direction!r}")


def hit_rate(directional: Sequence[float | None]) -> float | None:
    """적중률. `None`(hold·미채점)은 분모에서 뺀다."""
    scored = [d for d in directional if d is not None]
    if not scored:
        return None
    return sum(1 for d in scored if d > 0) / len(scored)


def mean(values: Sequence[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def stdev(values: Sequence[float | None], sample: bool = True) -> float | None:
    vals = [v for v in values if v is not None]
    n = len(vals)
    if n < (2 if sample else 1):
        return None
    m = sum(vals) / n
    var = sum((v - m) ** 2 for v in vals) / ((n - 1) if sample else n)
    return math.sqrt(var)


def _ranks(values: Sequence[float]) -> list[float]:
    """동점은 평균 순위로. 스피어만 상관에 쓴다."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(x: Sequence[float], y: Sequence[float]) -> float | None:
    n = len(x)
    if n < 3:
        return None
    mx, my = sum(x) / n, sum(y) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx == 0 or syy == 0:
        return None          # 한쪽이 상수다. 상관이 0인 게 아니라 잴 수 없다.
    return sxy / math.sqrt(sxx * syy)


def correlation(x: Sequence[float | None], y: Sequence[float | None]) -> dict:
    """확신도-수익 상관. 기획서 9.1절이 **가장 중요한 지표**로 꼽은 것.

    스피어만을 주로 본다 — 확신도는 서열 정보에 가깝고 피어슨은 이상치에 끌려간다.
    둘 다 계산해 같이 보고한다. 표본이 3건 미만이거나 한쪽이 상수면 None이다.
    """
    pairs = [(a, b) for a, b in zip(x, y) if a is not None and b is not None]
    if len(pairs) < 3:
        return {"n": len(pairs), "pearson": None, "spearman": None}
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    return {
        "n": len(pairs),
        "pearson": _pearson(xs, ys),
        "spearman": _pearson(_ranks(xs), _ranks(ys)),
    }


def classify_regime(index_return: float | None,
                    threshold: float = REGIME_THRESHOLD) -> Regime | None:
    """구간 지수 수익률로 레짐을 나눈다. 전체 평균은 거짓말을 한다(기획서 6절)."""
    if index_return is None:
        return None
    if index_return > threshold:
        return "up"
    if index_return < -threshold:
        return "down"
    return "flat"


# ---- 시계열 지표 (층 2 포트폴리오용) ----

def max_drawdown(series: Sequence[float]) -> float | None:
    """최대 낙폭. 음수로 돌려준다 (-0.2 = -20%)."""
    if len(series) < 2:
        return None
    peak, worst = series[0], 0.0
    for v in series[1:]:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, v / peak - 1.0)
    return worst


def cagr(start: float, end: float, years: float) -> float | None:
    if start <= 0 or years <= 0:
        return None
    return (end / start) ** (1.0 / years) - 1.0


def sharpe(returns: Sequence[float], periods_per_year: int = 252,
           risk_free: float = 0.0) -> float | None:
    """기간 수익률 계열의 샤프. `risk_free`는 같은 주기 단위로 넣는다."""
    excess = [r - risk_free for r in returns]
    m, sd = mean(excess), stdev(excess)
    if m is None or sd is None or sd <= ZERO_VARIANCE_TOL:
        return None
    return m / sd * math.sqrt(periods_per_year)


def sortino(returns: Sequence[float], periods_per_year: int = 252,
            risk_free: float = 0.0) -> float | None:
    """하방 변동만으로 나눈다. 상승 변동을 위험으로 세지 않는다."""
    excess = [r - risk_free for r in returns]
    m = mean(excess)
    downside = [e for e in excess if e < 0]
    if m is None or len(downside) < 1:
        return None
    dd = math.sqrt(sum(e ** 2 for e in downside) / len(excess))
    if dd <= ZERO_VARIANCE_TOL:
        return None
    return m / dd * math.sqrt(periods_per_year)


def turnover(prev_weights: dict[str, float], new_weights: dict[str, float]) -> float:
    """리밸런싱 회전율. 비중 변화 절대값 합의 절반 (편도 기준)."""
    keys = set(prev_weights) | set(new_weights)
    return sum(abs(new_weights.get(k, 0.0) - prev_weights.get(k, 0.0))
               for k in keys) / 2.0


# ─────────────────────────────────────────────────────────────────────────
# 신뢰구간 — 점추정만 보고하지 않는다
#
# 2026-09-28에 US 5거래일 적중률 0.444를 보고하면서 "n=15라 해석하지 않는다"를
# **말로** 덧붙였다. 말은 다음 사람에게 전달되지 않는다. 숫자 옆에 구간을 같이
# 찍으면 해석의 한계가 보고서 자체에 박힌다.
# ─────────────────────────────────────────────────────────────────────────

def _t_quantile(p: float, df: int) -> float:
    """Student t 분위수. scipy 없이 Cornish-Fisher 전개로 구한다.

    표본이 작을 때가 정확히 이 구간이 필요한 때인데, 정규 근사(z=1.96)를 쓰면
    그때 구간을 좁게 잡는다. n=15에서 z는 1.96, t는 2.145로 10% 차이다.
    df=14에서 이 전개는 2.1439를 주고 참값은 2.1448이다.
    """
    z = NormalDist().inv_cdf(p)
    if df <= 0:
        return z
    g1 = (z ** 3 + z) / 4
    g2 = (5 * z ** 5 + 16 * z ** 3 + 3 * z) / 96
    g3 = (3 * z ** 7 + 19 * z ** 5 + 17 * z ** 3 - 15 * z) / 384
    return z + g1 / df + g2 / df ** 2 + g3 / df ** 3


def proportion_ci(successes: int, n: int, confidence: float = 0.95
                  ) -> tuple[float, float] | None:
    """비율의 신뢰구간 (Wilson score).

    적중률처럼 0/1을 세는 값에 쓴다. **정규 근사(Wald)를 쓰지 않는다** — 표본이
    작거나 비율이 0·1에 가까울 때 구간이 [0,1] 밖으로 나가거나 터무니없이 좁아진다.
    """
    if n <= 0 or successes < 0 or successes > n:
        return None
    z = NormalDist().inv_cdf(1 - (1 - confidence) / 2)
    p = successes / n
    denom = 1 + z ** 2 / n
    center = (p + z ** 2 / (2 * n)) / denom
    half = z / denom * math.sqrt(p * (1 - p) / n + z ** 2 / (4 * n ** 2))
    return (max(0.0, center - half), min(1.0, center + half))


def mean_ci(values: Sequence[float | None], confidence: float = 0.95
            ) -> tuple[float, float] | None:
    """평균의 신뢰구간. 표본이 작으므로 t 분포를 쓴다 (`_t_quantile`)."""
    xs = [v for v in values if v is not None]
    if len(xs) < 2:
        return None
    m, sd = mean(xs), stdev(xs)
    if m is None or sd is None:
        return None
    t = _t_quantile(1 - (1 - confidence) / 2, len(xs) - 1)
    half = t * sd / math.sqrt(len(xs))
    return (m - half, m + half)


# ─────────────────────────────────────────────────────────────────────────
# 다중검정 보정 — 공짜 사후 탐색의 대가
#
# `docs/validation.md` 5.3절은 저장된 의견·확신도를 재조합해 **조합 1,116가지를
# LLM 호출 0회로** 탐색한다고 적었다. 공짜라서 매력적이지만, 그게 정확히
# **백테스트 과최적화의 교과서적 상황**이다 — 1,116개를 다 계산하고 최고를 고르면
# 그 최고는 실력이 아니라 최댓값 통계일 수 있다.
#
# 새 호출이 드는 8아암에는 Bonferroni를 걸어뒀는데, **공짜인 쪽이 위험은 더 크다.**
# 아래 둘이 그 구멍을 메운다 (Bailey & López de Prado).
# ─────────────────────────────────────────────────────────────────────────

def deflated_sharpe(observed_sharpe: float, trial_sharpes: Sequence[float],
                    n_obs: int, skew: float = 0.0, kurtosis: float = 3.0
                    ) -> float | None:
    """Deflated Sharpe Ratio — "진짜 샤프 > 0"일 확률.

    관측 샤프를 **시행 횟수 · 시행들의 샤프 분산 · 수익률의 왜도/첨도**로 깎는다.
    시행을 많이 할수록 우연히 높은 샤프가 나오므로, 그 기대 최댓값을 넘어야 의미가 있다.

    `observed_sharpe`와 `trial_sharpes`는 **연율화하지 않은 주기당 샤프**여야 한다.
    연율화한 값을 넣으면 `n_obs` 보정과 단위가 어긋난다.
    `kurtosis`는 초과첨도가 아니라 원첨도다 — 정규분포가 3이다.

    0.95를 넘으면 시행 횟수를 감안해도 엣지가 남아 있다고 본다.
    """
    n_trials = len(trial_sharpes)
    if n_trials < 2 or n_obs < 2:
        return None
    sd = stdev(trial_sharpes)
    if sd is None or sd <= ZERO_VARIANCE_TOL:
        return None

    # 귀무가설(진짜 엣지 없음) 아래에서 N번 시행했을 때 기대되는 **최대** 샤프.
    euler = 0.5772156649015329
    nd = NormalDist()
    expected_max = sd * ((1 - euler) * nd.inv_cdf(1 - 1 / n_trials)
                         + euler * nd.inv_cdf(1 - 1 / (n_trials * math.e)))

    denom = 1 - skew * observed_sharpe + (kurtosis - 1) / 4 * observed_sharpe ** 2
    if denom <= 0:
        return None
    return nd.cdf((observed_sharpe - expected_max) * math.sqrt(n_obs - 1)
                  / math.sqrt(denom))


def _sharpe_raw(returns: Sequence[float]) -> float | None:
    """주기당(연율화 없음) 샤프. PBO 내부 순위용."""
    m, sd = mean(returns), stdev(returns)
    if m is None or sd is None or sd <= ZERO_VARIANCE_TOL:
        return None
    return m / sd


def pbo(trials: Sequence[Sequence[float]], splits: int = 8) -> dict | None:
    """Probability of Backtest Overfitting — CSCV 방식.

    시행들의 수익률 계열을 시간축으로 `splits`개 조각으로 나누고, 절반을 IS(탐색),
    절반을 OOS(검증)로 쓰는 **모든 조합**에 대해:

        IS에서 1등인 구성이 OOS에서 중앙값 아래로 떨어지는가?

    그 비율이 PBO다. **0.5를 넘으면 과최적합**이다 — 동전 던지기보다 못하게
    고르고 있다는 뜻이다.

    **한 번 잰 PBO를 정밀한 값으로 읽지 않는다.** 조합들이 같은 데이터를 공유해
    서로 독립이 아니다. 합성 데이터 30셋으로 재보니(2026-09-28) 엣지가 없을 때
    평균 0.500 · **표준편차 0.165**(범위 0.27~0.86), 엣지가 있을 때 평균 0.142였다.
    0.4와 0.5의 차이는 잡음이고, 0.5와 0.8의 차이는 신호다.

    `trials`는 구성마다 하나씩, **같은 기간·같은 순서**의 수익률 계열이다.
    아암마다 같은 `as_of`를 겪는 우리 설계에서는 이 정렬이 자동으로 맞는다.
    """
    n_trials = len(trials)
    if n_trials < 2 or splits < 2 or splits % 2 != 0:
        return None
    n_obs = len(trials[0])
    if any(len(t) != n_obs for t in trials) or n_obs < splits:
        return None

    bounds = [round(i * n_obs / splits) for i in range(splits + 1)]
    blocks = [list(range(bounds[i], bounds[i + 1])) for i in range(splits)]
    if any(not b for b in blocks):
        return None

    logits: list[float] = []
    for combo in itertools.combinations(range(splits), splits // 2):
        is_idx = [i for c in combo for i in blocks[c]]
        oos_idx = [i for c in range(splits) if c not in combo for i in blocks[c]]

        is_perf = [_sharpe_raw([t[i] for i in is_idx]) for t in trials]
        if all(p is None for p in is_perf):
            continue
        best = max(range(n_trials), key=lambda k: (is_perf[k] is not None, is_perf[k]))

        oos_perf = [_sharpe_raw([t[i] for i in oos_idx]) for t in trials]
        if oos_perf[best] is None:
            continue
        # 최악 1등 ~ 최고 n등. 상대순위 ω를 로짓으로 옮긴다.
        worse = sum(1 for p in oos_perf if p is not None and p < oos_perf[best])
        omega = (worse + 1) / (n_trials + 1)
        omega = min(max(omega, 1e-9), 1 - 1e-9)
        logits.append(math.log(omega / (1 - omega)))

    if not logits:
        return None
    return {
        "pbo": sum(1 for l in logits if l <= 0) / len(logits),
        "combinations": len(logits),
        "splits": splits,
        "median_logit": sorted(logits)[len(logits) // 2],
    }
