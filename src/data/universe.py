"""유니버스 로더. config/universe.json 하나가 수집·평가의 기준이다.

유니버스를 조용히 바꾸면 성적 비교가 깨진다. 종목 교체는 반드시
config/universe.json의 changelog에 날짜와 이유를 남긴다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from src import config

UNIVERSE_PATH = config.PROJECT_ROOT / "config" / "universe.json"


@dataclass(frozen=True)
class Holding:
    symbol: str
    name: str
    sector: str
    market: str
    basis: str
    news_query: str
    news_require: tuple[str, ...] = ()
    news_exclude: tuple[str, ...] = ()
    collect_every_hours: int = 24
    """수집 주기. KR은 실측으로 배정한다 (scripts/measure_news_rate.py).
    US(Finnhub)는 날짜 범위 조회가 되어 커버리지 문제가 없으므로 24시간 고정."""

    @property
    def names(self) -> list[str]:
        """관련성 판정에 쓸 표기들.

        US는 티커도 후보에 넣는다 — 영문 기사 제목은 'NVDA'처럼 티커로 쓰는 일이
        흔하다. KR 종목코드(6자리 숫자)는 제목에 나오지 않으므로 넣지 않는다.
        """
        terms = [self.name, *self.news_require]
        if self.market == "US":
            terms.append(self.symbol)
        return terms


@dataclass(frozen=True)
class Universe:
    version: str
    holdings: dict[str, tuple[Holding, ...]] = field(default_factory=dict)

    def market(self, market: str) -> tuple[Holding, ...]:
        return self.holdings.get(market, ())

    def symbols(self, market: str) -> list[str]:
        return [h.symbol for h in self.market(market)]

    def get(self, market: str, symbol: str) -> Holding:
        for holding in self.market(market):
            if holding.symbol == symbol:
                return holding
        raise KeyError(f"{market} 유니버스에 {symbol!r}가 없습니다.")


@lru_cache(maxsize=1)
def load_universe(path: Path | None = None) -> Universe:
    payload = json.loads((path or UNIVERSE_PATH).read_text(encoding="utf-8"))
    holdings = {}
    for market in ("US", "KR"):
        holdings[market] = tuple(
            Holding(
                symbol=row["symbol"],
                name=row["name"],
                sector=row["sector"],
                market=market,
                basis=row.get("basis", ""),
                news_query=row.get("news_query") or row["name"],
                news_require=tuple(row.get("news_require", ())),
                news_exclude=tuple(row.get("news_exclude", ())),
                collect_every_hours=int(row.get("collect_every_hours", 24)),
            )
            for row in payload.get(market, [])
        )
    return Universe(version=payload.get("version", ""), holdings=holdings)
