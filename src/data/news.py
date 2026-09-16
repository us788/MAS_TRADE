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
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol

class Relevance(str, Enum):
    """검색 결과가 실제로 그 종목 기사인지의 등급.

    네이버 검색은 본문까지 매칭하므로 "종목명이 언급됐는가"로는 거를 수 없다.
    검색어가 스니펫에 거의 항상 들어 있어 그 필터는 100% 통과한다.
    실측(2026-09-15): 삼성전자 1,000건 중 제목 매칭 18%, 요약에만 82%.

    구분은 **제목에 있는가**로 한다. 제목에 회사명이 있으면 그 기사는 그 회사에 관한
    것일 가능성이 높고, 요약에만 있으면 대개 스쳐 지나가는 언급이다.
    """

    TITLE = "title"
    """제목에 종목명. 기본 파이프라인은 이것만 쓴다."""
    SUMMARY = "summary"
    """요약에만. 버리지 않고 저장해 사후 재채점에 쓴다."""
    NONE = "none"
    """제목·요약 어디에도 없음. 본문에만 걸린 경우."""


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
    relevance: str = Relevance.NONE.value
    vendor_tagged: bool = False
    """벤더가 종목을 직접 태깅했는가. Finnhub는 티커로 태깅하므로 True,
    네이버는 키워드 검색이라 False — 이 차이가 신뢰도를 가른다."""
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

    @property
    def is_primary(self) -> bool:
        """기본 파이프라인이 에이전트에 넘길 기사인가."""
        return self.vendor_tagged or self.relevance == Relevance.TITLE.value

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
            "relevance": self.relevance,
            "vendor_tagged": self.vendor_tagged,
            "content_hash": self.content_hash,
            **({"extra": self.extra} if self.extra else {}),
        }


@dataclass
class NewsBatch:
    """수집 1회의 결과와 **커버리지**.

    reached_floor가 False면 lookback 구간을 다 못 받았다는 뜻이다. 조용히 넘어가면
    그만큼이 영구 손실이므로 호출부가 gap으로 기록해야 한다.
    """

    items: list["NewsItem"]
    pages: int = 0
    oldest_seen: datetime | None = None
    reached_floor: bool = True
    raw: list = field(default_factory=list)
    """원본 응답 페이지들. 재현성을 위해 그대로 저장한다."""

    @property
    def primary(self) -> list["NewsItem"]:
        return [i for i in self.items if i.is_primary]


class NewsSource(Protocol):
    """시장별 어댑터가 구현한다. 상위 코드는 이것만 본다."""

    market: str

    def get_news(
        self, symbol: str, as_of: datetime, lookback_days: int = 7
    ) -> list[NewsItem]:
        """as_of 시점에 알 수 있었던 기사만 돌려준다."""
        ...


# ---- 정규화 헬퍼 ----


# 공백으로 취급할 제어문자. 나머지 Cc/Cf는 지운다.
_KEEP_AS_SPACE = "\t\n\r\f\v"


# C1 제어영역(U+0080~U+009F). 정상 텍스트에는 절대 나오지 않으므로 모지바케 신호다.
_C1 = re.compile(r"[\u0080-\u009f]")


def fix_mojibake(text: str) -> str:
    """UTF-8 바이트가 latin-1로 디코딩돼 온 것을 되돌린다.

    **2026-09-16에 Finnhub 응답에서 발견했다.** `â\x80\x94`가 들어오는데,
    이건 `E2 80 94`(U+2014 EM DASH)의 UTF-8 바이트가 latin-1로 읽힌 결과다.

        'growth stay strongâ\x80\x94see why'  ->  'growth stay strong—see why'

    제어문자만 지우면 `â`가 남아 단어가 깨진다. 되돌리는 것이 맞다.

    **C1 제어문자가 있을 때만** 시도한다. 정상 텍스트에 U+0080~U+009F가 나올 일이
    없으므로 오탐이 없는 신호다. 한국어가 섞여 있으면 latin-1 인코딩이 실패하는데,
    그건 그대로 두면 된다 — 이 증상은 영문 소스에서만 나온다.
    """
    if not _C1.search(text):
        return text
    try:
        repaired = text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    # 복구 결과에 C1이 남아 있으면 가정이 틀린 것이다. 원문을 지킨다.
    return text if _C1.search(repaired) else repaired


def strip_invisible(text: str) -> str:
    """보이지 않는 서식·제어 문자를 지운다.

    **2026-09-16에 실제로 발견한 문제다.** 네이버 기사에 소프트 하이픈(U+00AD)이
    섞여 들어온다 (`'박민정 기\xad자'`). 화면에는 안 보이는데 두 가지로 샌다.

    1. 토큰 낭비 — 의미 없는 문자가 프롬프트에 실린다
    2. **관련성 판정이 조용히 실패한다.** 한국어는 부분 문자열 매칭이라
       `삼성전자` 사이에 이게 끼면 매칭이 안 되고, 그 기사는 `title` 등급을
       못 받아 에이전트에 넘어가지 않는다. 기사를 잃는 것과 같다.

    유니코드 카테고리로 거른다 — 목록을 하드코딩하면 새 문자가 나올 때마다 샌다.
    `Cf`(서식)는 전부, `Cc`(제어)는 공백류만 남기고 지운다. 공백류는 뒤에서
    `_WS`가 하나로 접는다.
    """
    return "".join(
        ch for ch in text
        if not (unicodedata.category(ch) == "Cf"
                or (unicodedata.category(ch) == "Cc" and ch not in _KEEP_AS_SPACE))
    )


def clean_text(value: str | None) -> str:
    """벤더 원문을 씻는다. **이미 씻은 텍스트에 다시 돌리면 안 된다 - 멱등이 아니다.**

    태그를 먼저 지우고 엔티티를 나중에 푸는 순서 때문에 HTML 엔티티로 인코딩된
    꺾쇠는 리터럴 `<...>`로 남는다. 저장된 그 값에 이 함수를 또 돌리면 이번에는
    진짜 태그로 보여 지워진다. 요약이 통째로 사라지는 경우가 실제로 있었다.

    저장된 기사에 **새 정규화 규칙만** 다시 입히려면 `renormalize()`를 쓴다.
    """
    if not value:
        return ""
    # 블록 태그는 단어 경계이므로 공백으로, 인라인 태그(<b> 등)는 지운다.
    # 인라인까지 공백으로 바꾸면 "<b>삼성전자</b>," 가 "삼성전자 ," 가 된다.
    text = _BLOCK_TAG.sub(" ", value)
    text = _TAG.sub("", text)
    # 태그를 먼저 걷어낸 뒤에 엔티티를 푼다. 순서를 바꾸면 &lt;b&gt; 같은
    # 리터럴 텍스트가 진짜 태그가 되어 지워진다.
    text = html.unescape(text)
    # 모지바케를 먼저 되돌린다. 순서가 반대면 `â\x80\x94`에서 제어문자만 지워져
    # `â`가 남고, 그때는 되돌릴 근거(C1 신호)조차 사라진다.
    text = fix_mojibake(text)
    # **엔티티를 푼 뒤**에 지운다. `&shy;`가 바로 U+00AD로 풀리기 때문이다.
    text = strip_invisible(text)
    return _WS.sub(" ", text).strip()


def renormalize(value: str | None) -> str:
    """이미 `clean_text`를 거쳐 저장된 텍스트에 **나중에 생긴 규칙만** 다시 입힌다.

    태그 제거와 엔티티 해제는 다시 하지 않는다 - 수집 시점에 이미 끝났고,
    다시 하면 리터럴 꺾쇠가 태그로 오인돼 본문이 사라진다(`clean_text` 설명 참고).

    `scripts/clean_news_text.py`가 쓴다. 과거 구간은 다시 받을 수 없으므로
    이미 받은 것을 새 규칙으로 다시 읽을 수 있어야 한다.
    """
    if not value:
        return ""
    return _WS.sub(" ", strip_invisible(fix_mojibake(value))).strip()


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


def term_matches(term: str, text: str) -> bool:
    """종목 표기가 텍스트에 나타나는가.

    영문과 한국어를 다르게 다뤄야 한다.

    - **영문·티커는 경계를 보고 대소문자를 무시한다.** 부분 문자열로 보면
      'NEE'가 'engineer'에, 'CAT'이 'category'에 걸린다. 대소문자를 구분하면
      제목의 'Nvidia'가 'NVIDIA'와 매칭되지 않는다.
    - 경계는 `\b`가 아니라 **"앞뒤가 ASCII 영숫자가 아닐 것"** 이다. `\b`는 한글도
      단어 문자로 보기 때문에, 한국어 기사에서 조사가 영문에 바로 붙는 경우
      ('SKT가', 'NAVER의') 경계가 성립하지 않아 놓친다.
    - **한국어는 부분 문자열로 본다.** 조사가 바로 붙어('삼성전자는', '기아가')
      경계 규칙 자체를 쓸 수 없다.
    """
    if not term:
        return False
    if term.isascii():
        pattern = rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])"
        return re.search(pattern, text, re.IGNORECASE) is not None
    return term in text


def is_excluded(item: NewsItem, exclude: list[str]) -> bool:
    """동명이의 배제. 걸리면 등급을 매기지 않고 버린다 ('한화' -> 야구단)."""
    haystack = f"{item.title} {item.summary}"
    return any(term_matches(term, haystack) for term in exclude)


def classify_relevance(item: NewsItem, names: list[str]) -> Relevance:
    """제목에 있으면 TITLE, 요약에만 있으면 SUMMARY, 둘 다 없으면 NONE.

    names가 비어 있으면 판정 근거가 없으므로 NONE.

    벤더가 티커로 태깅하는 소스(Finnhub)에도 **이 판정을 함께 매긴다.** 태깅이
    관련성을 보장하지 않기 때문이다 — 실측(2026-09-15) NVDA 250건 중 제목에
    회사명이 있는 것은 17%였다. 태깅 여부는 vendor_tagged가 따로 들고 있으므로
    두 신호를 나중에 갈라 볼 수 있다.
    """
    if not names:
        return Relevance.NONE
    if any(term_matches(name, item.title) for name in names):
        return Relevance.TITLE
    if any(term_matches(name, item.summary) for name in names):
        return Relevance.SUMMARY
    return Relevance.NONE
