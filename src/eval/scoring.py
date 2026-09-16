"""시그널 채점 — 기획서 9.1절 "층 1 시그널 평가"의 구현.

이게 **본 평가**다. 포트폴리오 수익률이 아니라 **의견 1건이 샘플 1건**이다
(기획서 9.3절 — 주 1회 3개월이면 판단 시점이 12번뿐이라 포트폴리오로는 아무 말도
할 수 없다).

**이 모듈이 `PriceStore.forward_bar`를 부르는 유일한 곳이다.** 에이전트에는
as_of 전용 인터페이스만 주입한다 (`docs/harness.md` 3.1절).

지켜야 할 것 셋.

1. **적중은 절대수익이 아니라 지수 대비 초과로 판정한다** (9.1절). 그냥 올랐는지가
   아니다.
2. **종목과 지수를 같은 기준(`close_px`)으로 잰다** (harness.md 3.4.1절).
   종목만 배당 조정하면 고배당주가 공짜 점수를 얻는다 — XOM 3년에 14.9%p였다.
3. **못 잰 것은 실패가 아니라 미채점이다.** 5거래일이 아직 안 지난 시그널은
   `pending`이고, 적재가 밀린 것은 `stale`이다. 둘을 섞으면 성적이 조용히 왜곡된다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Iterable, Sequence

from src.compute import metrics
from src.data.price_sources import BENCHMARKS
from src.data.prices import Bar, PriceStore

# 기획서 9.1절이 정한 평가 호라이즌.
HORIZONS = (5, 20)

# 레짐을 판정할 때 되돌아보는 거래일 수. 시그널이 **어떤 장세에서 나왔는지**를
# 보는 것이므로 호라이즌이 아니라 직전 구간을 본다. 60거래일 ≈ 3개월.
REGIME_LOOKBACK = 60

# 적재가 이만큼 밀렸으면 "아직 안 왔다"가 아니라 "안 받았다"로 본다.
# 달력 기준이라 연휴 뭉치(추석·설)를 넘기려면 여유가 필요하다. 다만 주 판정은
# 지수와의 비교다 — 그쪽이 그 시장의 실제 거래일을 알고 있다.
STALE_DAYS = 7


@dataclass(frozen=True)
class CostModel:
    """기획서 6절 상수. **세율은 바뀐다 — 확인일을 함께 남긴다.**"""
    commission_bps: float    # 편도 수수료
    tax_bps: float           # 거래세. 매도 시에만 붙는다
    slippage_bps: float
    checked: str             # 확인일

    @property
    def roundtrip(self) -> float:
        """진입 + 청산 1회분. 수수료·슬리피지는 양방향, 거래세는 매도 한 번."""
        return (2 * self.commission_bps + 2 * self.slippage_bps + self.tax_bps) / 10_000


# KR 거래세: 2026년 환원으로 코스피 0.05% + 농특세 0.15% = 0.20%, 코스닥도 0.20%.
# 기획서 6절의 0.18%는 낡은 값이다 (docs/harness.md 4.2절).
COSTS = {
    "US": CostModel(commission_bps=5.0, tax_bps=0.0, slippage_bps=5.0,
                    checked="2026-09-16"),
    "KR": CostModel(commission_bps=1.5, tax_bps=20.0, slippage_bps=5.0,
                    checked="2026-09-16"),
}


@dataclass(frozen=True)
class HorizonScore:
    """호라이즌 하나의 채점 결과.

    `status`가 `scored`가 아니면 나머지 값은 전부 None이다. 부분적으로 채운 값을
    집계에 섞지 않기 위해서다.
    """
    trading_days: int
    status: str              # scored | pending | stale | no_base | misaligned
    end_date: date | None = None
    symbol_return: float | None = None
    index_return: float | None = None
    excess: float | None = None
    directional: float | None = None          # 시그널대로 했을 때의 초과수익
    directional_after_cost: float | None = None
    hit: bool | None = None                   # hold는 None
    regime: str | None = None


@dataclass(frozen=True)
class ScoredSignal:
    symbol: str
    market: str
    as_of: datetime
    direction: str
    confidence: float
    base_date: date | None
    base_price: float | None
    horizons: dict[int, HorizonScore] = field(default_factory=dict)


def _direction_of(signal) -> str:
    d = getattr(signal, "direction", None)
    return str(getattr(d, "value", d))


class Scorer:
    """시그널을 실제 등락에 맞대어 채점한다.

    `PriceStore`를 직접 들고 있는 유일한 평가 객체다. 에이전트 쪽에 넘기지 않는다.
    """

    def __init__(self, store: PriceStore | None = None,
                 horizons: Sequence[int] = HORIZONS,
                 today: date | None = None) -> None:
        self.store = store or PriceStore()
        self.horizons = tuple(horizons)
        self._today = today or datetime.now(timezone.utc).date()

    # ---- 단건 ----

    def score(self, signal) -> ScoredSignal:
        market = signal.market
        direction = _direction_of(signal)
        index_symbol = BENCHMARKS[market][0]

        base = self.store.close_at(signal.symbol, market, signal.as_of)
        index_base = self.store.close_at(index_symbol, market, signal.as_of)

        if base is None or index_base is None:
            return ScoredSignal(
                symbol=signal.symbol, market=market, as_of=signal.as_of,
                direction=direction, confidence=signal.confidence,
                base_date=base.date if base else None,
                base_price=base.close_px if base else None,
                horizons={n: HorizonScore(n, "no_base") for n in self.horizons},
            )

        if base.date != index_base.date:
            # 종목과 지수의 기준일이 다르면 초과수익이 하루치 어긋난다.
            # 조용히 계산하지 않고 드러낸다.
            return ScoredSignal(
                symbol=signal.symbol, market=market, as_of=signal.as_of,
                direction=direction, confidence=signal.confidence,
                base_date=base.date, base_price=base.close_px,
                horizons={n: HorizonScore(n, "misaligned") for n in self.horizons},
            )

        regime = self._regime(index_symbol, base.date)
        scores = {n: self._horizon(signal.symbol, index_symbol, market, base,
                                   index_base, direction, n, regime)
                  for n in self.horizons}
        return ScoredSignal(
            symbol=signal.symbol, market=market, as_of=signal.as_of,
            direction=direction, confidence=signal.confidence,
            base_date=base.date, base_price=base.close_px, horizons=scores,
        )

    def score_many(self, signals: Iterable) -> list[ScoredSignal]:
        return [self.score(s) for s in signals]

    # ---- 내부 ----

    def _regime(self, index_symbol: str, base: date) -> str | None:
        past = self.store.trailing_bar(index_symbol, base, REGIME_LOOKBACK)
        cur = self.store.close_at_date(index_symbol, base)
        if past is None or cur is None:
            return None
        return metrics.classify_regime(
            metrics.pct_return(past.close_px, cur.close_px))

    def _horizon(self, symbol: str, index_symbol: str, market: str, base: Bar,
                 index_base: Bar, direction: str, n: int,
                 regime: str | None) -> HorizonScore:
        fwd = self.store.forward_bar(symbol, base.date, n)
        index_fwd = self.store.forward_bar(index_symbol, base.date, n)

        if fwd is None or index_fwd is None:
            return HorizonScore(n, self._missing_status(symbol, index_symbol),
                                regime=regime)
        if fwd.date != index_fwd.date:
            return HorizonScore(n, "misaligned", regime=regime)

        # 종목과 지수 모두 close_px. 기준을 섞으면 고배당주가 유리해진다.
        sym_ret = metrics.pct_return(base.close_px, fwd.close_px)
        idx_ret = metrics.pct_return(index_base.close_px, index_fwd.close_px)
        excess = metrics.excess_return(sym_ret, idx_ret)
        directional = metrics.directional_excess(excess, direction)

        after_cost = None
        if directional is not None:
            after_cost = directional - COSTS[market].roundtrip

        return HorizonScore(
            trading_days=n, status="scored", end_date=fwd.date,
            symbol_return=sym_ret, index_return=idx_ret, excess=excess,
            directional=directional, directional_after_cost=after_cost,
            hit=None if directional is None else directional > 0, regime=regime,
        )

    def _missing_status(self, symbol: str, index_symbol: str) -> str:
        """아직 안 온 것(pending)과 안 받은 것(stale)을 구분한다.

        달력만 보면 연휴 뭉치에서 오탐한다. **지수와 비교하는 것이 주 판정**이다 —
        지수는 그 시장이 실제로 언제 열렸는지를 알고 있다. 종목이 지수보다 뒤처져
        있으면 그 종목만 적재가 밀린 것이다. 둘 다 오래됐으면 전체가 밀린 것이다.
        """
        last = self.store.latest_date(symbol)
        if last is None:
            return "no_base"
        index_last = self.store.latest_date(index_symbol)
        if index_last is not None and last < index_last:
            return "stale"                                   # 이 종목만 밀렸다
        anchor = index_last or last
        if (self._today - anchor).days > STALE_DAYS:
            return "stale"                                   # 전체가 밀렸다
        return "pending"


# ---- 집계 ----

def summarize(scored: Sequence[ScoredSignal], trading_days: int) -> dict:
    """호라이즌 하나에 대한 집계. 기획서 9.1절 측정 항목.

    **채점된 것만 분모에 넣는다.** pending·stale을 0으로 세면 성적이 왜곡된다.
    """
    rows = [(s, s.horizons.get(trading_days)) for s in scored]
    rows = [(s, h) for s, h in rows if h is not None]
    done = [(s, h) for s, h in rows if h.status == "scored"]

    directional = [h.directional for _, h in done]
    after_cost = [h.directional_after_cost for _, h in done]
    conf = [s.confidence for s, h in done if h.directional is not None]
    ret = [h.directional for _, h in done if h.directional is not None]

    by_status: dict[str, int] = {}
    for _, h in rows:
        by_status[h.status] = by_status.get(h.status, 0) + 1

    by_regime: dict[str, dict] = {}
    for regime in ("up", "down", "flat"):
        sub = [h.directional for _, h in done if h.regime == regime]
        if sub:
            by_regime[regime] = {"n": len([d for d in sub if d is not None]),
                                 "hit_rate": metrics.hit_rate(sub),
                                 "mean_excess": metrics.mean(sub)}

    holds = sum(1 for s, h in done if s.direction == "hold")
    return {
        "trading_days": trading_days,
        "signals": len(rows),
        "scored": len(done),
        "by_status": by_status,
        "hold_ratio": (holds / len(done)) if done else None,
        "hit_rate": metrics.hit_rate(directional),
        "mean_excess": metrics.mean(directional),
        "mean_excess_after_cost": metrics.mean(after_cost),
        "confidence_correlation": metrics.correlation(conf, ret),
        "by_regime": by_regime,
    }
