"""결정론 지표 계산.

**모든 수치 계산은 여기서 한다. LLM 프롬프트 안에서 산술을 시키지 않는다**
(`CLAUDE.md` 3절). LLM이 만든 틀린 숫자는 근거로 로그에 남아 사후 검증까지 오염시킨다.

이 모듈은 순수 함수만 둔다 — DB도 네트워크도 타지 않는다. 그래야 테스트가
입력·출력만으로 끝나고, 채점 결과를 의심할 때 여기부터 좁혀 들어올 수 있다.

**모르면 0이 아니라 None을 돌려준다.** 표본이 없는데 0을 주면 "성적이 0"과
"잴 수 없음"이 섞인다.
"""
from __future__ import annotations

import math
from typing import Literal, Sequence

# 레짐 분류 임계값. 구간 지수 수익률이 이보다 크면 상승, 작으면 하락, 사이면 횡보.
# 기획서 6절이 "전체 평균은 거짓말을 한다"며 레짐 분리를 요구하지만 경계값은 정하지
# 않았다. ±5%는 선택이다 — 바꾸면 성적 해석이 달라지므로 일지에 남기고 바꾼다.
REGIME_THRESHOLD = 0.05

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
    if m is None or sd is None or sd == 0:
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
    if dd == 0:
        return None
    return m / dd * math.sqrt(periods_per_year)


def turnover(prev_weights: dict[str, float], new_weights: dict[str, float]) -> float:
    """리밸런싱 회전율. 비중 변화 절대값 합의 절반 (편도 기준)."""
    keys = set(prev_weights) | set(new_weights)
    return sum(abs(new_weights.get(k, 0.0) - prev_weights.get(k, 0.0))
               for k in keys) / 2.0
