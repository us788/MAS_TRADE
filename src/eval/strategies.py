"""룰 기반 비LLM 베이스라인 — 기획서 2절이 FinRL을 지목한 자리의 앞단.

기획서 12절은 "멀티에이전트가 단일 LLM 호출을 이기는가"를 묻는다. 그 앞에 더 기본적인
질문이 있다 — **LLM이 20줄짜리 룰을 이기는가.** 못 이기면 LLM 자체가 의미 없다.

이 비교군이 특히 값진 이유 셋.

1. **룩어헤드가 없다는 걸 우리가 보장한다.** 기획서 7절이 "프레임워크 데이터 계층에서
   미래 데이터 유입 사례가 흔하다"고 경고했는데, 여기서는 `PriceStore`의 as-of
   인터페이스만 쓴다. `forward_bar`는 부르지 않는다
2. **학습 데이터 룩어헤드도 없다.** 기획서 4.4절이 백테스트를 2026-04-24 이후로
   제한한 것은 LLM이 과거를 이미 알기 때문이다. 룰에는 그 문제가 없어 **3년 전체**를
   쓸 수 있다. 다만 LLM과 맞대결할 때는 같은 구간으로 자른다
3. **비용 0, 완전 결정론.**

**방향은 횡단면 순위로 정한다.** 채점이 지수 대비 초과수익이므로 "이 종목이 오를까"가
아니라 "이 종목이 다른 종목보다 나을까"를 맞혀야 한다. 상위 1/3 매수, 하위 1/3 매도,
가운데는 hold다.

확신도도 순위에서 나온다 — 극단에 가까울수록 높다. LLM 확신도(폭 0.42~0.62)와
**정의가 명확하고 폭이 넓은 확신도**를 나란히 놓고 볼 수 있다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from src.compute import indicators, metrics
from src.data.price_sources import BENCHMARKS
from src.data.prices import Bar, PriceStore
from src.data.universe import Holding, load_universe

# 순위 → 확신도. 가운데는 0.5, 양 극단은 0.95.
CONF_MIN, CONF_MAX = 0.5, 0.95
# 상·하위 몇 분의 일을 매수·매도로 볼 것인가.
TERCILE = 1 / 3


@dataclass(frozen=True)
class RuleSignal:
    """채점기가 읽는 형태. `Scorer`가 보는 필드는 앞의 다섯이다."""
    symbol: str
    market: str
    as_of: datetime
    direction: str
    confidence: float
    score: float | None = None
    strategy: str = ""


class Strategy(ABC):
    """as-of 봉만 보고 종목별 점수를 낸다. 높을수록 매수 쪽이다."""

    name: str = "strategy"
    #: 점수 계산에 필요한 최소 봉 수. 모자라면 그 종목은 빠진다.
    min_bars: int = 130

    @abstractmethod
    def score(self, bars: Sequence[Bar], index_bars: Sequence[Bar]) -> float | None:
        """점수. 계산할 수 없으면 None — 0으로 채우면 '중립'으로 오해된다."""

    def to_signals(self, scored: list[tuple[str, float]], market: str,
                   as_of: datetime) -> list[RuleSignal]:
        """횡단면 순위 → 방향·확신도.

        동점은 평균 순위로 처리한다. 전 종목이 동점이면 방향을 가를 수 없으므로
        전부 hold다 — 억지로 가르면 없는 신호를 만들어내는 것이다.
        """
        n = len(scored)
        if n < 3:
            return []
        values = [s for _, s in scored]
        if len(set(values)) == 1:
            return [RuleSignal(sym, market, as_of, "hold", CONF_MIN, val, self.name)
                    for sym, val in scored]

        order = sorted(range(n), key=lambda i: values[i])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and values[order[j + 1]] == values[order[i]]:
                j += 1
            avg = (i + j) / 2
            for k in range(i, j + 1):
                ranks[order[k]] = avg
            i = j + 1

        out = []
        for idx, (symbol, value) in enumerate(scored):
            p = ranks[idx] / (n - 1)            # 0(최하) ~ 1(최상)
            if p >= 1 - TERCILE:
                direction = "buy"
            elif p <= TERCILE:
                direction = "sell"
            else:
                direction = "hold"
            conf = CONF_MIN + (CONF_MAX - CONF_MIN) * abs(2 * p - 1)
            out.append(RuleSignal(symbol, market, as_of, direction,
                                  round(conf, 3), value, self.name))
        return out


class Momentum(Strategy):
    """최근 N거래일 지수 대비 초과수익. 이긴 종목이 계속 이긴다는 쪽에 건다."""

    def __init__(self, lookback: int = 60) -> None:
        self.lookback = lookback
        self.name = f"momentum_{lookback}d"
        self.min_bars = lookback + 10

    def score(self, bars, index_bars):
        return indicators.relative_strength(bars, index_bars, self.lookback)


class MeanReversion(Strategy):
    """단기 초과수익의 반대. 많이 빠진 종목이 되돌아온다는 쪽에 건다."""

    def __init__(self, lookback: int = 5) -> None:
        self.lookback = lookback
        self.name = f"reversal_{lookback}d"
        self.min_bars = lookback + 10

    def score(self, bars, index_bars):
        rs = indicators.relative_strength(bars, index_bars, self.lookback)
        return None if rs is None else -rs


class LowVolatility(Strategy):
    """변동성이 낮은 종목을 산다. 저변동성 이상현상(low-vol anomaly)."""

    name = "low_volatility"
    min_bars = 70

    def score(self, bars, index_bars):
        vol = indicators.annualized_volatility(bars)
        return None if vol is None else -vol


class TrendFollowing(Strategy):
    """120일 이동평균 위에 있으면 매수 쪽. 추세 추종의 가장 단순한 형태."""

    name = "ma_gap_120d"
    min_bars = 130

    def score(self, bars, index_bars):
        return indicators.moving_average_gap(bars, 120)


class BuyAndHold(Strategy):
    """유니버스 동일가중 전량 매수. **유니버스가 지수를 이기는가**를 묻는다.

    횡단면 순위를 쓰지 않는다 — 전 종목이 같은 방향이다. 확신도도 고정이므로
    확신도-수익 상관은 잴 수 없고(상수는 상관이 정의되지 않는다) 그게 맞다.
    """

    name = "buy_and_hold"
    min_bars = 2

    def score(self, bars, index_bars):
        return 0.0

    def to_signals(self, scored, market, as_of):
        return [RuleSignal(sym, market, as_of, "buy", CONF_MIN, val, self.name)
                for sym, val in scored]


ALL_STRATEGIES: tuple[Strategy, ...] = (
    Momentum(60),
    Momentum(20),
    MeanReversion(5),
    LowVolatility(),
    TrendFollowing(),
    BuyAndHold(),
)


class StrategyRunner:
    """as-of 봉을 읽어 룰 시그널을 만든다. **`forward_bar`를 부르지 않는다.**"""

    def __init__(self, store: PriceStore | None = None,
                 lookback_days: int = 400) -> None:
        self.store = store or PriceStore()
        self.lookback_days = lookback_days
        self._universe = load_universe()

    def generate(self, strategy: Strategy, market: str,
                 as_of: datetime) -> list[RuleSignal]:
        index_symbol = BENCHMARKS[market][0]
        index_bars = self.store.series(index_symbol, market, as_of,
                                       lookback_days=self.lookback_days)
        if len(index_bars) < strategy.min_bars:
            return []

        scored: list[tuple[str, float]] = []
        for holding in self._universe.market(market):
            bars = self.store.series(holding.symbol, market, as_of,
                                     lookback_days=self.lookback_days)
            if len(bars) < strategy.min_bars:
                continue
            value = strategy.score(bars, index_bars)
            if value is not None:
                scored.append((holding.symbol, value))
        return strategy.to_signals(scored, market, as_of)
