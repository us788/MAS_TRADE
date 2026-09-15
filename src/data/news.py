"""뉴스 공통 타입. 벤더가 무엇이든 여기까지 오면 같은 모양이 된다.

기획서 8절. 상위 에이전트는 Finnhub를 쓰는지 네이버를 쓰는지 몰라야 한다.

이 파일의 존재 이유는 타임스탬프 규칙 하나다. 벤더가 주는 발행 시각에는
재발행·수정 시각이 섞여 들어온다. 그래서 `published_at`과 `collected_at`을
둘 다 들고 다니고, 어긋나면 보수적인 쪽을 쓴다 (`known_at`).
"""
from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

_BLOCK_TAG = re.compile(r"</?(?:br|p|div|li|tr|h[1-6])\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class NewsItem:
    """기사 1건. 본문은 담지 않는다 — 무료 티어는 대부분 재배포 금지다."""

    source: str
    """수집한 벤더. "finnhub" | "naver" """
    market: str
    symbol: str
    title: str
    summary: str
    url: str
    publisher: str
    published_at: datetime
    """벤더가 말하는 발행 시각 (UTC)."""
    collected_at: datetime
    """우리가 실제로 받아온 시각 (UTC). 이게 있어야 벤더 시각을 검증할 수 있다."""
    original_url: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def timestamp_suspect(self) -> bool:
        """발행 시각이 수집 시각보다 미래다. 있을 수 없는 일이므로 값을 믿지 않는다."""
        return self.published_at > self.collected_at

    @property
    def known_at(self) -> datetime:
        """이 기사를 '알 수 있게 된 시각'. as_of 필터는 이 값으로 건다.

        평소에는 발행 시각이지만, 그게 수집 시각보다 미래면 벤더 값이 깨진 것이므로
        보수적으로 수집 시각을 쓴다. 늦게 알았다고 보는 쪽이 룩어헤드에 안전하다.
        """
        return self.collected_at if self.timestamp_suspect else self.published_at

    @property
    def content_hash(self) -> str:
        """중복 제거 키이자, 데이터셋 공개 시 본문 대신 남길 지문."""
        basis = (self.original_url or self.url or f"{self.title}|{self.published_at}")
        return hashlib.sha256(basis.strip().lower().encode("utf-8")).hexdigest()[:16]

    def to_record(self) -> dict:
        """JSONL 한 줄. 공개 가능한 필드만 담는다."""
        return {
            "source": self.source,
            "market": self.market,
            "symbol": self.symbol,
            "title": self.title,
            "summary": self.summary,
            "url": self.url,
            "original_url": self.original_url,
            "publisher": self.publisher,
            "published_at": self.published_at.isoformat(),
            "collected_at": self.collected_at.isoformat(),
            "known_at": self.known_at.isoformat(),
            "timestamp_suspect": self.timestamp_suspect,
            "content_hash": self.content_hash,
            **({"extra": self.extra} if self.extra else {}),
        }


class NewsSource(Protocol):
    """시장별 어댑터가 구현한다. 상위 코드는 이것만 본다."""

    market: str

    def get_news(
        self, symbol: str, as_of: datetime, lookback_days: int = 7
    ) -> list[NewsItem]:
        """as_of 시점에 알 수 있었던 기사만 돌려준다."""
        ...


# ---- 정규화 헬퍼 ----


def clean_text(value: str | None) -> str:
    """네이버는 <b> 태그와 HTML 엔티티를 섞어 보낸다. 그대로 프롬프트에 넣지 않는다."""
    if not value:
        return ""
    # 블록 태그는 단어 경계이므로 공백으로, 인라인 태그(<b> 등)는 지운다.
    # 인라인까지 공백으로 바꾸면 "<b>삼성전자</b>," 가 "삼성전자 ," 가 된다.
    text = _BLOCK_TAG.sub(" ", value)
    text = _TAG.sub("", text)
    # 태그를 먼저 걷어낸 뒤에 엔티티를 푼다. 순서를 바꾸면 &lt;b&gt; 같은
    # 리터럴 텍스트가 진짜 태그가 되어 지워진다.
    text = html.unescape(text)
    return _WS.sub(" ", text).strip()


def to_utc(value: datetime) -> datetime:
    """저장은 전부 UTC. 표시할 때만 현지 시간으로 바꾼다."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def filter_as_of(
    items: list[NewsItem], as_of: datetime, lookback_days: int | None = None
) -> list[NewsItem]:
    """as_of 이후에 알려진 기사를 잘라낸다. 어댑터가 아니라 여기서 한 번에 건다."""
    cutoff = to_utc(as_of)
    kept = [item for item in items if item.known_at <= cutoff]
    if lookback_days is not None:
        floor = cutoff.timestamp() - lookback_days * 86400
        kept = [item for item in kept if item.known_at.timestamp() >= floor]
    return sorted(kept, key=lambda i: i.known_at, reverse=True)


def matches_symbol(item: NewsItem, require: list[str], exclude: list[str]) -> bool:
    """동명이의 필터. 한국 뉴스는 회사명으로 검색해야 해서 잡음이 크다.

    require가 비어 있으면 통과시킨다 (미국은 티커 태깅이 벤더 쪽에서 된다).
    """
    haystack = f"{item.title} {item.summary}"
    if any(term and term in haystack for term in exclude):
        return False
    if not require:
        return True
    return any(term and term in haystack for term in require)
