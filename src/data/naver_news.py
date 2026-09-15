"""네이버 검색 API(뉴스) 어댑터 — KR 뉴스 메인.

이 어댑터가 까다로운 이유는 세 가지다.

1. **날짜 범위 파라미터가 없다.** sort=date로 최신순을 받아 pubDate로 직접 자른다.
   과거 구간 조회는 불가능하다 — 그래서 매일 수집해 쌓는 것이 전제다.
2. **종목코드로 검색되지 않는다.** 회사명으로 쿼리해야 해서 동명이의 잡음이 크다.
   유니버스의 news_require / news_exclude로 거른다.
3. **pubDate는 KST**이고 title·description에 <b> 태그와 HTML 엔티티가 섞여 온다.

플랫폼(apihub/legacy)에 따라 도메인·경로·헤더가 전부 다르다. config에서 고른다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests

from src import config
from src.data.news import NewsItem, clean_text, filter_as_of, matches_symbol, to_utc

MAX_DISPLAY = 100
MAX_START = 1000


class NaverNews:
    market = "KR"
    source = "naver"

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        max_rps: float = 5.0,
    ) -> None:
        endpoint = config.naver_endpoint()
        self._base = endpoint["base_url"] + endpoint["news_path"]
        self._session = requests.Session()
        self._session.headers.update({
            endpoint["id_header"]: client_id or config.require("NAVER_CLIENT_ID"),
            endpoint["secret_header"]: client_secret or config.require("NAVER_CLIENT_SECRET"),
        })
        from src.data._http import RateLimiter
        self._limiter = RateLimiter(max_rps)

    def fetch_raw(self, query: str, display: int = MAX_DISPLAY, start: int = 1) -> dict:
        from src.data._http import get_json
        return get_json(
            self._session,
            self._base,
            limiter=self._limiter,
            params={
                "query": query,
                "display": min(display, MAX_DISPLAY),
                "start": min(start, MAX_START),
                "sort": "date",
            },
            auth_hint=(
                f"NAVER_API_FLAVOR={config.NAVER_API_FLAVOR}입니다. "
                "발급처(개발자센터/API HUB)와 맞는지 확인하세요 — 헤더 이름이 다릅니다."
            ),
        )

    def get_news(
        self,
        symbol: str,
        as_of: datetime,
        lookback_days: int = 7,
        query: str | None = None,
        require: list[str] | None = None,
        exclude: list[str] | None = None,
    ) -> list[NewsItem]:
        as_of = to_utc(as_of)
        payload = self.fetch_raw(query or symbol)
        collected_at = datetime.now(timezone.utc)

        items = []
        for row in payload.get("items", []):
            item = parse_item(row, symbol, collected_at)
            if item is None:
                continue
            if not matches_symbol(item, list(require or []), list(exclude or [])):
                continue
            items.append(item)
        return filter_as_of(items, as_of, lookback_days)


def parse_item(row: dict, symbol: str, collected_at: datetime) -> NewsItem | None:
    """네이버 응답 1건 → NewsItem. pubDate는 RFC 2822 + KST 오프셋이다."""
    published = parse_pub_date(row.get("pubDate"))
    if published is None:
        return None   # 발행 시각이 없으면 시점 정합을 보장할 수 없다.
    original = row.get("originallink") or ""
    return NewsItem(
        source="naver",
        market="KR",
        symbol=symbol,
        title=clean_text(row.get("title")),
        summary=clean_text(row.get("description")),
        url=row.get("link", ""),
        original_url=original,
        publisher=publisher_from(original or row.get("link", "")),
        published_at=published,
        collected_at=to_utc(collected_at),
    )


def parse_pub_date(value: str | None) -> datetime | None:
    """'Tue, 15 Sep 2026 01:38:00 +0900' → UTC datetime."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    return to_utc(parsed)


def publisher_from(url: str) -> str:
    """원문 링크 도메인을 매체명 대용으로 쓴다. 화이트리스트 필터의 기준이 된다."""
    if not url:
        return ""
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host
