"""Finnhub company-news 어댑터 — US 뉴스 메인.

무료 분당 60콜, 개인·비상업 한정. 기사 본문은 저장하지 않는다 (재배포 금지).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from src import config
from src.data._http import RateLimiter, get_json
from src.data.news import NewsItem, clean_text, filter_as_of, to_utc


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

    def get_news(
        self, symbol: str, as_of: datetime, lookback_days: int = 7
    ) -> list[NewsItem]:
        as_of = to_utc(as_of)
        # to는 하루 넉넉히 잡고, 경계는 known_at으로 정확히 자른다.
        payload = self.fetch_raw(
            symbol, as_of - timedelta(days=lookback_days), as_of + timedelta(days=1)
        )
        collected_at = datetime.now(timezone.utc)
        items = [parse_item(row, symbol, collected_at) for row in payload or []]
        return filter_as_of([i for i in items if i], as_of, lookback_days)


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
        extra={"category": row.get("category", ""), "vendor_id": row.get("id")},
    )
