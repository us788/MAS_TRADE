"""Finnhub company-news 어댑터 — US 뉴스 메인.

무료 분당 60콜, 개인·비상업 한정. 기사 본문은 저장하지 않는다 (재배포 금지).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from src import config
from src.data._http import RateLimiter, get_json
from src.data.news import NewsBatch, NewsItem, clean_text, filter_as_of, to_utc


class FinnhubNews:
    market = "US"
    source = "finnhub"

    def __init__(self, api_key: str | None = None, max_rps: float = 1.0) -> None:
        self._key = api_key or config.require("FINNHUB_API_KEY")
        self._limiter = RateLimiter(max_rps)   # 분당 60콜 = 초당 1
        self._session = requests.Session()

    def fetch_raw(self, symbol: str, start: datetime, end: datetime) -> list[dict]:
        return get_json(
            self._session,
            f"{config.FINNHUB_BASE_URL}/company-news",
            limiter=self._limiter,
            params={
                "symbol": symbol,
                "from": to_utc(start).strftime("%Y-%m-%d"),
                "to": to_utc(end).strftime("%Y-%m-%d"),
                "token": self._key,
            },
            auth_hint="FINNHUB_API_KEY를 확인하세요.",
        )

    def collect(
        self, symbol: str, as_of: datetime, lookback_days: float = 1.0, **_
    ) -> NewsBatch:
        """날짜 범위로 직접 조회한다.

        네이버와 달리 구간을 지정할 수 있어 **커버리지 미달이 구조적으로 없다**.
        그래서 reached_floor는 항상 True다 — 수집 주기도 하루 1회로 충분하다.
        """
        as_of = to_utc(as_of)
        payload = self.fetch_raw(
            symbol, as_of - timedelta(days=lookback_days), as_of + timedelta(days=1)
        ) or []
        collected_at = datetime.now(timezone.utc)
        parsed = [parse_item(row, symbol, collected_at) for row in payload]
        items = [i for i in parsed if i]
        oldest = min((i.published_at for i in items), default=None)
        return NewsBatch(
            items=filter_as_of(items, as_of, lookback_days),
            pages=1,
            oldest_seen=oldest,
            reached_floor=True,
            raw=[payload],
        )

    def get_news(
        self, symbol: str, as_of: datetime, lookback_days: int = 7, **kwargs
    ) -> list[NewsItem]:
        return self.collect(symbol, as_of, lookback_days, **kwargs).primary


def parse_item(row: dict, symbol: str, collected_at: datetime) -> NewsItem | None:
    """Finnhub 응답 1건 → NewsItem. `datetime`은 유닉스 초(UTC)다."""
    stamp = row.get("datetime")
    if not stamp:
        return None   # 발행 시각이 없으면 시점 정합을 보장할 수 없다. 버린다.
    return NewsItem(
        source="finnhub",
        market="US",
        symbol=symbol,
        title=clean_text(row.get("headline")),
        summary=clean_text(row.get("summary")),
        url=row.get("url", ""),
        original_url=row.get("url", ""),
        publisher=row.get("source", ""),
        published_at=datetime.fromtimestamp(int(stamp), tz=timezone.utc),
        collected_at=to_utc(collected_at),
        # Finnhub는 티커로 종목을 태깅해 돌려준다. 네이버 키워드 검색과 달리
        # 벤더가 관련성을 보증하므로 별도 제목 매칭 없이 1차 자료로 쓴다.
        vendor_tagged=True,
        extra={"category": row.get("category", ""), "vendor_id": row.get("id")},
    )
