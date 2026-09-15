"""네이버 검색 API(뉴스) 어댑터 — KR 뉴스 메인.

이 어댑터가 까다로운 이유는 실측으로 드러났다 (2026-09-15, 종목당 1,000건 표본).

1. **키워드 검색이지 종목 피드가 아니다.** 본문까지 매칭하므로 "종목명이 언급됐는가"로
   거를 수 없다 — 검색어가 스니펫에 거의 항상 들어 있어 100% 통과한다.
   삼성전자 1,000건 중 제목 매칭은 18%뿐이었다. 그래서 버리는 대신 **등급**을 매긴다.
2. **최신순 1,000건이 덮는 구간이 종목마다 다르다.** 삼성전자 7시간, 한화에어로 8일.
   start 상한이 1,000이라 언급 많은 종목은 하루치도 한 번에 못 받는다.
   → 종목별 수집 주기를 다르게 간다 (universe.json의 collect_every_hours).
3. 날짜 범위 파라미터가 없고, pubDate는 KST, 제목·요약에 태그와 엔티티가 섞여 온다.

플랫폼(apihub/legacy)에 따라 도메인·경로·헤더가 다르다. config에서 고른다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests

from src import config
from src.data._http import RateLimiter, get_json
from src.data.news import (
    NewsItem,
    Relevance,
    classify_relevance,
    clean_text,
    filter_as_of,
    is_excluded,
    to_utc,
)

MAX_DISPLAY = 100
MAX_START = 1000
MAX_PAGES = MAX_START // MAX_DISPLAY


@dataclass
class NewsBatch:
    """수집 1회의 결과와 **커버리지**.

    reached_floor가 False면 lookback 구간을 다 못 받았다는 뜻이다.
    조용히 넘어가면 그만큼이 영구 손실이므로 호출부가 gap으로 기록해야 한다.
    """

    items: list[NewsItem]
    pages: int
    oldest_seen: datetime | None
    reached_floor: bool

    @property
    def primary(self) -> list[NewsItem]:
        return [i for i in self.items if i.is_primary]


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
        self._limiter = RateLimiter(max_rps)

    def fetch_raw(self, query: str, display: int = MAX_DISPLAY, start: int = 1) -> dict:
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

    def collect(
        self,
        symbol: str,
        as_of: datetime,
        lookback_days: float = 1.0,
        *,
        query: str | None = None,
        names: list[str] | None = None,
        exclude: list[str] | None = None,
        max_pages: int = MAX_PAGES,
    ) -> NewsBatch:
        """lookback 경계에 닿을 때까지 페이징하며 긁는다.

        경계에 못 닿고 페이지 상한에 걸리면 reached_floor=False로 알린다.
        """
        as_of = to_utc(as_of)
        floor = as_of - timedelta(days=lookback_days)
        collected_at = datetime.now(timezone.utc)

        items: list[NewsItem] = []
        oldest: datetime | None = None
        pages = 0
        reached = False

        for start in range(1, MAX_START + 1, MAX_DISPLAY):
            if pages >= max_pages:
                break
            payload = self.fetch_raw(query or symbol, MAX_DISPLAY, start)
            rows = payload.get("items", [])
            if not rows:
                reached = True   # 더 줄 게 없으면 그 구간은 다 받은 것이다
                break
            pages += 1

            for row in rows:
                item = parse_item(row, symbol, collected_at)
                if item is None:
                    continue
                if oldest is None or item.published_at < oldest:
                    oldest = item.published_at
                if is_excluded(item, list(exclude or [])):
                    continue
                items.append(
                    replace_relevance(item, classify_relevance(item, list(names or [])))
                )

            if oldest is not None and oldest <= floor:
                reached = True
                break

        return NewsBatch(
            items=filter_as_of(items, as_of, lookback_days),
            pages=pages,
            oldest_seen=oldest,
            reached_floor=reached,
        )

    def get_news(
        self, symbol: str, as_of: datetime, lookback_days: int = 7, **kwargs
    ) -> list[NewsItem]:
        """NewsSource 프로토콜용. 기본 파이프라인은 제목 매칭 기사만 쓴다."""
        return self.collect(symbol, as_of, lookback_days, **kwargs).primary


def replace_relevance(item: NewsItem, relevance: Relevance) -> NewsItem:
    from dataclasses import replace
    return replace(item, relevance=relevance.value)


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
        vendor_tagged=False,   # 키워드 검색이다. 벤더가 종목을 보증하지 않는다.
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
    """원문 링크 도메인을 매체명 대용으로 쓴다."""
    if not url:
        return ""
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host
