"""as-of 컨텍스트 조립 — 에이전트가 보는 세계 전부.

**에이전트는 이 모듈이 준 것만 본다.** 원본 소스를 직접 조회하는 코드를 에이전트에
넣지 않는다 (`CLAUDE.md` 3절). 그래야 룩어헤드를 한 곳에서 막을 수 있다.

여기서 지키는 것 셋.

1. **모든 조회가 `as_of` 타임스탬프를 통과한다.** 가격은 장마감 판정을 거치고
   (`PriceStore.close_at`), 뉴스는 `known_at` 기준으로 잘린다(`Store.news_for`).
   `forward_bar`는 이 모듈에서 부르지 않는다 — 그건 채점기 전용이다.
2. **수치는 코드가 계산해 넘긴다.** `src/compute/indicators.py`가 만든 숫자만 들어간다.
   프롬프트 안에서 산술을 시키지 않는다.
3. **기사 본문은 없다.** API가 준 제목·요약·발행시각까지다. 무료 티어는 대부분
   재배포 금지라 로그를 공개할 때 본문을 빼야 하므로, 처음부터 해시로 참조한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from src.compute import indicators
from src.data.price_sources import BENCHMARKS
from src.data.prices import PriceStore
from src.data.storage import Store
from src.data.universe import Holding, load_universe

# 지표 계산에 필요한 최소 구간을 덮는 달력일. 120거래일 ≈ 168일이므로 여유를 둔다.
PRICE_LOOKBACK_DAYS = 400

# 프롬프트에 넣는 기사 수 상한. 넘치면 최신순으로 자른다.
# 1M 컨텍스트가 있다고 원문을 통째로 밀어넣지 않는다 (기획서 4.2절).
MAX_NEWS = 25
NEWS_LOOKBACK_DAYS = 7
SUMMARY_CHARS = 200


@dataclass(frozen=True)
class NewsRef:
    """기사 1건. 본문은 없고 참조용 해시를 들고 다닌다."""
    content_hash: str
    title: str
    summary: str
    publisher: str
    known_at: str
    timestamp_suspect: bool

    def to_payload(self) -> dict:
        out = {"id": self.content_hash, "title": self.title,
               "summary": self.summary, "publisher": self.publisher,
               "known_at": self.known_at}
        if self.timestamp_suspect:
            # 벤더가 준 발행 시각이 수상해 collected_at으로 대체된 기사다.
            # 시점을 덜 믿으라고 표시해 둔다.
            out["timestamp_suspect"] = True
        return out


@dataclass(frozen=True)
class SymbolContext:
    symbol: str
    name: str
    sector: str
    market: str
    as_of: datetime
    base_date: date
    base_price: float
    indicators: dict
    news: tuple[NewsRef, ...] = ()
    universe_version: str = ""

    @property
    def data_refs(self) -> list[str]:
        """이 판단이 참조한 스냅샷 식별자. `AgentOpinion.data_refs`에 그대로 들어간다."""
        return [f"price:{self.symbol}:{self.base_date.isoformat()}"] + \
               [f"news:{n.content_hash}" for n in self.news]

    def to_payload(self) -> dict:
        """프롬프트의 **가변부**. 고정 프리픽스와 섞지 않는다 (캐시가 깨진다)."""
        return {
            "symbol": self.symbol,
            "name": self.name,
            "sector": self.sector,
            "market": self.market,
            "as_of": self.as_of.isoformat(),
            "reference": {"date": self.base_date.isoformat(),
                          "close": self.base_price},
            "indicators": self.indicators,
            "news": [n.to_payload() for n in self.news],
        }


class ContextBuilder:
    """as-of 컨텍스트를 만든다. 에이전트에는 이 객체만 주입한다."""

    def __init__(self, price_store: PriceStore | None = None,
                 news_store: Store | None = None) -> None:
        self.prices = price_store or PriceStore()
        self.news = news_store or Store()
        self._universe = load_universe()

    def build(self, holding: Holding, as_of: datetime,
              news_days: int = NEWS_LOOKBACK_DAYS,
              max_news: int = MAX_NEWS) -> SymbolContext | None:
        """기준봉이 없으면 None. 시그널을 만들 수 없는 상태를 조용히 넘기지 않는다."""
        market = holding.market
        index_symbol = BENCHMARKS[market][0]

        base = self.prices.close_at(holding.symbol, market, as_of)
        if base is None:
            return None

        bars = self.prices.series(holding.symbol, market, as_of,
                                  lookback_days=PRICE_LOOKBACK_DAYS)
        index_bars = self.prices.series(index_symbol, market, as_of,
                                        lookback_days=PRICE_LOOKBACK_DAYS)

        rows = self.news.news_for(holding.symbol, as_of, lookback_days=news_days,
                                  primary_only=True)
        news = tuple(
            NewsRef(
                content_hash=r["content_hash"],
                title=(r["title"] or "").strip(),
                summary=(r["summary"] or "").strip()[:SUMMARY_CHARS],
                publisher=r["publisher"] or "",
                known_at=r["known_at"],
                timestamp_suspect=bool(r["timestamp_suspect"]),
            )
            for r in rows[:max_news]
        )

        return SymbolContext(
            symbol=holding.symbol, name=holding.name, sector=holding.sector,
            market=market, as_of=as_of,
            base_date=base.date, base_price=base.close_px,
            indicators=indicators.compute_all(bars, index_bars),
            news=news, universe_version=self._universe.version,
        )

    def build_market(self, market: str, as_of: datetime, **kw
                     ) -> list[SymbolContext]:
        """유니버스 전 종목. 기준봉이 없는 종목은 빠진다."""
        out = []
        for holding in self._universe.market(market):
            ctx = self.build(holding, as_of, **kw)
            if ctx is not None:
                out.append(ctx)
        return out
