"""층 2 — 가상 보유 포트폴리오. 기획서 9.2절.

**보조 지표다.** 주 평가는 층 1(시그널 채점)이다. 3개월간 주 1회면 판단 시점이
12번뿐이라 포트폴리오 수익률만으로는 어떤 결론도 낼 수 없다(9.3절). 이건 "그래서
얼마?"라는 직관적 질문에 답하기 위한 트랙이다.

기획서가 못박은 규칙 넷.

1. **종목당 동일 금액 가중.** 임의 수량은 비중이 우연히 정해져 실력과 운이 섞인다.
   동일 금액이면 **종목 선택 자체만** 평가된다
2. **US/KR 각각 독립.** 현지통화로 끝까지 따로 본다. 합산하면 환율이 섞여 오염된다
3. 배당은 재투자 가정 → `close_tr`로 평가한다
4. 거래비용은 6절 기준 (`src/eval/scoring.py`의 `COSTS`)

**체결가 정의가 이 모듈의 핵심 설계다.**

시그널은 수요일 07:00 KST에 나오고 기준가는 화요일 종가다. 그 가격으로는 살 수 없다 —
판단이 나오기 전 가격이다. 그래서 **체결은 시그널 당일 종가**로 본다(`bar_on_or_after`).
아침에 판단하고 그날 장 마감에 체결하는 셈이라 실행 가능하고 보수적이다.
KR은 수요일 15:30, US는 수요일 세션(KST 목요일 새벽) 종가다.

**소수점 주식을 허용한다.** 정수로 끊으면 반올림이 비중을 왜곡해 "동일 금액 가중이라
종목 선택만 평가된다"는 전제가 깨진다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable, Literal, Sequence

from src.compute import metrics
from src.data.price_sources import BENCHMARKS
from src.data.prices import PriceStore
from src.eval.scoring import COSTS, CostModel

#: 가상 원금. 현지통화 기준이고 **두 시장을 합산하지 않는다**(기획서 9.2절).
#: 소수점 주식을 허용하므로 절대 금액은 수익률에 영향을 주지 않는다.
INITIAL_CAPITAL = {"KR": 10_000_000.0, "US": 10_000.0}

#: 어떤 시그널을 보유로 볼 것인가.
#:   exclude_sell — 매도만 제외하고 나머지 동일가중 (기획서 9.2절 문구 그대로)
#:   buy_only     — 매수만 편입. 더 집중적이라 시그널의 방향성이 선명하게 드러난다
Rule = Literal["exclude_sell", "buy_only"]


@dataclass(frozen=True)
class Trade:
    symbol: str
    shares: float          # 양수 매수, 음수 매도
    price: float
    value: float           # 절대 거래대금
    cost: float


@dataclass(frozen=True)
class Rebalance:
    as_of: datetime
    executed_on: date
    nav_before: float
    nav_after: float       # 비용 차감 후
    cost: float
    turnover: float        # 회전율 (편도)
    holdings: dict[str, float]
    trades: list[Trade] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


@dataclass
class PortfolioResult:
    market: str
    rule: Rule
    initial_capital: float
    rebalances: list[Rebalance] = field(default_factory=list)
    nav: list[tuple[date, float]] = field(default_factory=list)

    @property
    def total_return(self) -> float | None:
        if len(self.nav) < 2:
            return None
        return metrics.pct_return(self.nav[0][1], self.nav[-1][1])

    def summary(self) -> dict:
        values = [v for _, v in self.nav]
        years = ((self.nav[-1][0] - self.nav[0][0]).days / 365.25) if len(self.nav) > 1 else 0
        # 리밸런싱 간 수익률로 샤프를 낸다. 주 1회이므로 연 52회.
        periodic = [r for a, b in zip(values, values[1:])
                    if (r := metrics.pct_return(a, b)) is not None]
        return {
            "market": self.market,
            "rule": self.rule,
            "rebalances": len(self.rebalances),
            "total_return": self.total_return,
            "cagr": metrics.cagr(values[0], values[-1], years) if years > 0 else None,
            "max_drawdown": metrics.max_drawdown(values),
            "sharpe": metrics.sharpe(periodic, periods_per_year=52),
            "sortino": metrics.sortino(periodic, periods_per_year=52),
            "total_cost": sum(r.cost for r in self.rebalances),
            "mean_turnover": metrics.mean([r.turnover for r in self.rebalances]),
            "mean_holdings": metrics.mean([float(len(r.holdings)) for r in self.rebalances]),
        }


def _targets(signals: Sequence, rule: Rule) -> list[str]:
    if rule == "buy_only":
        return sorted({s.symbol for s in signals if s.direction == "buy"})
    return sorted({s.symbol for s in signals if s.direction != "sell"})


class VirtualPortfolio:
    """동일 금액 가중 가상 포트폴리오. 시장 하나를 끝까지 현지통화로 본다."""

    def __init__(self, store: PriceStore | None = None, *, market: str,
                 rule: Rule = "exclude_sell",
                 initial_capital: float | None = None,
                 cost_model: CostModel | None = None) -> None:
        self.store = store or PriceStore()
        self.market = market
        self.rule = rule
        self.capital = initial_capital or INITIAL_CAPITAL[market]
        self.costs = cost_model or COSTS[market]

    # ---- 평가 ----

    def _price(self, symbol: str, on: date) -> tuple[date, float] | None:
        """체결·평가 가격. **배당 재투자 가정이므로 `close_tr`이다.**

        KR은 벤더가 배당 조정 계열을 주지 않아 `close_tr == close_px`다 —
        즉 KR 포트폴리오는 실질적으로 가격수익률이다. US와 나란히 놓고
        절대 수익률을 비교하지 않는다 (`docs/harness.md` 3.4.2).
        """
        bar = self.store.bar_on_or_after(symbol, on)
        return (bar.date, bar.close_tr) if bar else None

    def _value(self, holdings: dict[str, float], on: date) -> float:
        total = 0.0
        for symbol, shares in holdings.items():
            got = self._price(symbol, on)
            if got:
                total += shares * got[1]
        return total

    # ---- 실행 ----

    def run(self, signals_by_date: Iterable[tuple[datetime, Sequence]]) -> PortfolioResult:
        result = PortfolioResult(self.market, self.rule, self.capital)
        cash = self.capital
        holdings: dict[str, float] = {}

        for as_of, signals in sorted(signals_by_date, key=lambda x: x[0]):
            exec_day = as_of.date()
            targets = _targets(signals, self.rule)

            # 체결 가능한 종목만. 가격이 없으면 그 종목은 이번 회차에서 뺀다.
            priced: dict[str, tuple[date, float]] = {}
            skipped = []
            for symbol in targets:
                got = self._price(symbol, exec_day)
                if got is None:
                    skipped.append(symbol)
                else:
                    priced[symbol] = got
            if not priced:
                continue
            executed_on = max(d for d, _ in priced.values())

            nav_before = cash + self._value(holdings, exec_day)
            # 동일 금액 가중. 소수점 주식을 허용해 반올림이 비중을 흔들지 않게 한다.
            target_value = nav_before / len(priced)

            trades, total_cost, traded = [], 0.0, 0.0
            new_holdings: dict[str, float] = {}
            for symbol in set(holdings) | set(priced):
                got = priced.get(symbol) or self._price(symbol, exec_day)
                if got is None:                       # 평가도 못 하면 그대로 둔다
                    new_holdings[symbol] = holdings.get(symbol, 0.0)
                    continue
                _, price = got
                want = (target_value / price) if symbol in priced else 0.0
                have = holdings.get(symbol, 0.0)
                delta = want - have
                if abs(delta * price) > 1e-9:
                    value = abs(delta * price)
                    bps = self.costs.commission_bps + self.costs.slippage_bps
                    if delta < 0:                      # 매도에만 거래세가 붙는다
                        bps += self.costs.tax_bps
                    cost = value * bps / 10_000
                    trades.append(Trade(symbol, delta, price, value, cost))
                    total_cost += cost
                    traded += value
                if want > 0:
                    new_holdings[symbol] = want

            nav_after = nav_before - total_cost
            # 비용은 현금에서 나간다. 동일가중을 다시 맞추면 오차가 남으므로
            # 비용 차감 후 NAV로 비중을 재계산한다.
            if new_holdings:
                adj = nav_after / nav_before if nav_before else 1.0
                new_holdings = {k: v * adj for k, v in new_holdings.items()}
            holdings = new_holdings
            cash = nav_after - self._value(holdings, exec_day)

            result.rebalances.append(Rebalance(
                as_of=as_of, executed_on=executed_on, nav_before=nav_before,
                nav_after=nav_after, cost=total_cost,
                turnover=(traded / nav_before / 2) if nav_before else 0.0,
                holdings=dict(holdings), trades=trades, skipped=skipped))
            result.nav.append((executed_on, nav_after))

        return result

    # ---- 벤치마크 ----

    def index_buy_and_hold(self, dates: Sequence[date]) -> PortfolioResult:
        """같은 날짜에 지수를 사서 들고 있었다면. 비용은 진입 1회분만 든다."""
        symbol = BENCHMARKS[self.market][0]
        out = PortfolioResult(self.market, self.rule, self.capital)
        shares = None
        for d in sorted(dates):
            got = self._price(symbol, d)
            if got is None:
                continue
            day, price = got
            if shares is None:
                entry = self.capital * (self.costs.commission_bps
                                        + self.costs.slippage_bps) / 10_000
                shares = (self.capital - entry) / price
            out.nav.append((day, shares * price))
        return out
