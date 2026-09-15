"""뉴스 파이프라인 검증. 네트워크를 타지 않는다.

지키는 것: 시점 정합(known_at), 동명이의 필터, 타임존 정규화, 중복 제거.
"""
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.data.finnhub_news import parse_item as finnhub_parse
from src.data.naver_news import parse_item as naver_parse, parse_pub_date, publisher_from
from src.data.news import (
    NewsItem,
    Relevance,
    classify_relevance,
    clean_text,
    filter_as_of,
    is_excluded,
    term_matches,
)
from src.data.storage import Store

UTC = timezone.utc
NOW = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)


def make(title="t", summary="", published=None, collected=None, symbol="X", url="u"):
    return NewsItem(
        source="test", market="KR", symbol=symbol, title=title, summary=summary,
        url=url, publisher="p",
        published_at=published or NOW - timedelta(hours=1),
        collected_at=collected or NOW,
    )


# ---- 텍스트 정규화 ----

def test_b태그와_엔티티를_걷어낸다():
    raw = "<b>삼성전자</b>, 3분기 실적 &quot;호조&quot; &amp; 전망"
    assert clean_text(raw) == '삼성전자, 3분기 실적 "호조" & 전망'


def test_빈값도_안전하다():
    assert clean_text(None) == "" and clean_text("") == ""


# ---- 타임존 ----

def test_KST_발행시각을_UTC로_정규화한다():
    parsed = parse_pub_date("Tue, 15 Sep 2026 01:38:00 +0900")
    assert parsed == datetime(2026, 9, 14, 16, 38, tzinfo=UTC)
    assert parsed.tzinfo == UTC


def test_깨진_날짜는_None():
    assert parse_pub_date("어제") is None
    assert parse_pub_date(None) is None


def test_유닉스초를_UTC로_읽는다():
    item = finnhub_parse({"datetime": 1789392614, "headline": "h", "url": "http://a",
                          "source": "Reuters"}, "AAPL", NOW)
    assert item.published_at.tzinfo == UTC
    assert item.published_at == datetime.fromtimestamp(1789392614, tz=UTC)


def test_발행시각_없으면_버린다():
    # 시점 정합을 보장할 수 없는 기사는 애초에 들이지 않는다.
    assert finnhub_parse({"headline": "h", "url": "http://a"}, "AAPL", NOW) is None
    assert naver_parse({"title": "t", "link": "http://a"}, "005930", NOW) is None


# ---- known_at (시점 정합의 핵심) ----

def test_평소에는_발행시각을_쓴다():
    item = make(published=NOW - timedelta(hours=3), collected=NOW)
    assert item.timestamp_suspect is False
    assert item.known_at == NOW - timedelta(hours=3)


def test_발행시각이_수집시각보다_미래면_수집시각을_쓴다():
    # 벤더 값이 깨진 경우. 늦게 알았다고 보는 쪽이 룩어헤드에 안전하다.
    item = make(published=NOW + timedelta(hours=5), collected=NOW)
    assert item.timestamp_suspect is True
    assert item.known_at == NOW


def test_as_of_이후_기사는_잘린다():
    before = make(published=NOW - timedelta(hours=2), url="a")
    after = make(published=NOW + timedelta(hours=2),
                 collected=NOW + timedelta(hours=3), url="b")
    kept = filter_as_of([before, after], NOW)
    assert [i.url for i in kept] == ["a"]


def test_lookback_밖_기사도_잘린다():
    old = make(published=NOW - timedelta(days=30), collected=NOW - timedelta(days=30), url="old")
    new = make(published=NOW - timedelta(days=1), collected=NOW - timedelta(days=1), url="new")
    kept = filter_as_of([old, new], NOW, lookback_days=7)
    assert [i.url for i in kept] == ["new"]


def test_최신순으로_정렬된다():
    a = make(published=NOW - timedelta(hours=5), url="a")
    b = make(published=NOW - timedelta(hours=1), url="b")
    assert [i.url for i in filter_as_of([a, b], NOW)] == ["b", "a"]


# ---- 동명이의 배제 ----

def test_기아대책_기사는_배제된다():
    assert is_excluded(make(title="기아대책, 아동 후원 캠페인 진행"), ["기아대책"]) is True


def test_한화_야구_기사는_배제된다():
    assert is_excluded(make(title="한화 이글스, 야구 경기 승리"), ["이글스", "야구"]) is True


def test_배제어가_없으면_통과():
    assert is_excluded(make(title="기아, 3분기 판매량 증가"), ["기아대책"]) is False


# ---- 표기 매칭 ----
# 영문과 한국어를 다르게 다뤄야 한다. US를 붙이기 전까지 드러나지 않던 문제.

def test_영문은_대소문자를_무시한다():
    # 제목은 'Nvidia', 유니버스는 'NVIDIA'. 구분하면 전부 놓친다.
    assert term_matches("NVIDIA", "Nvidia stock jumps") is True
    assert term_matches("Apple", "APPLE'S new chip") is True


def test_짧은_티커가_일반_단어에_걸리지_않는다():
    # 부분 문자열이면 NEE가 engineer에, CAT이 category에 걸린다.
    assert term_matches("NEE", "The engineer said") is False
    assert term_matches("CAT", "New category launched") is False


def test_티커가_독립_단어면_매칭된다():
    assert term_matches("NEE", "NEE beats estimates") is True
    assert term_matches("CAT", "CAT raises guidance") is True


def test_한국어_기사_속_영문_약칭도_조사_뒤에서_매칭된다():
    # \b는 한글도 단어 문자로 보기 때문에 'SKT가'에서 경계가 성립하지 않는다.
    # 경계 기준을 "앞뒤가 ASCII 영숫자가 아닐 것"으로 둬야 양쪽이 다 된다.
    assert term_matches("SKT", "SKT가 RCS 표준 제안") is True
    assert term_matches("NAVER", "NAVER의 유럽 승부수") is True
    assert term_matches("SKT", "SKT, 실적 발표") is True
    # 영문 단어 내부는 여전히 배제한다.
    assert term_matches("SKT", "ASKTV 방송") is False


def test_한국어는_조사가_붙어도_매칭된다():
    # 한국어는 조사가 바로 붙어 단어 경계가 성립하지 않는다.
    assert term_matches("삼성전자", "삼성전자는 오늘 발표했다") is True
    assert term_matches("기아", "기아가 판매 증가") is True


def test_빈_표기는_매칭되지_않는다():
    assert term_matches("", "아무 텍스트") is False


# ---- 관련성 등급 ----
# 네이버는 본문까지 매칭하므로 "언급됐는가"로는 못 거른다. 제목에 있는지로 가른다.

def test_제목에_있으면_TITLE():
    item = make(title="삼성전자, 3분기 영업이익 발표", summary="...")
    assert classify_relevance(item, ["삼성전자"]) is Relevance.TITLE


def test_요약에만_있으면_SUMMARY():
    # 검색 스니펫에는 검색어가 거의 항상 들어 있다. 이게 잡음의 정체다.
    item = make(title="코스피, 4거래일 연속 하락", summary="삼성전자 등 대형주가...")
    assert classify_relevance(item, ["삼성전자"]) is Relevance.SUMMARY


def test_둘_다_없으면_NONE():
    item = make(title="다른 회사 뉴스", summary="관련 없음")
    assert classify_relevance(item, ["삼성전자"]) is Relevance.NONE


def test_별칭_중_하나만_걸려도_TITLE():
    item = make(title="LG엔솔, 수주 확대")
    assert classify_relevance(item, ["LG에너지솔루션", "LG엔솔"]) is Relevance.TITLE


def test_기본_파이프라인은_TITLE만_쓴다():
    title = make(title="삼성전자 실적")
    summary = make(title="코스피 하락", summary="삼성전자 포함")
    from dataclasses import replace
    assert replace(title, relevance="title").is_primary is True
    assert replace(summary, relevance="summary").is_primary is False


def test_벤더가_태깅한_기사는_제목_매칭_없이도_1차자료():
    # Finnhub는 티커로 태깅해 돌려주므로 제목에 회사명이 없어도 관련 기사다.
    from dataclasses import replace
    item = replace(make(title="Chip stocks rally"), vendor_tagged=True, relevance="none")
    assert item.is_primary is True


# ---- 매체명 ----

def test_원문_도메인을_매체명으로_쓴다():
    assert publisher_from("https://www.hankyung.com/article/123") == "hankyung.com"
    assert publisher_from("") == ""


# ---- 저장소 (SQLite) ----

def _store(tmp):
    return Store(db_path=Path(tmp) / "t.sqlite3", raw_dir=Path(tmp) / "raw")


def test_같은_기사를_두번_저장하지_않는다():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        items = [make(url="http://a/1"), make(url="http://a/2")]
        first = store.add_news(items)
        second = store.add_news(items)
        assert (first.written, first.skipped_duplicate) == (2, 0)
        assert (second.written, second.skipped_duplicate) == (0, 2)


def test_날짜가_바뀌어도_중복이_생기지_않는다():
    # JSONL 시절의 버그: 중복 검사 범위가 날짜별 파일이라 자정을 넘기면
    # 같은 기사가 다시 저장됐다. 1시간 주기면 매일 한 번씩 발생한다.
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        article = make(url="http://a/1", published=NOW - timedelta(hours=1))
        store.add_news([replace(article, collected_at=NOW.replace(hour=23, minute=50))])
        after_midnight = replace(article,
                                 collected_at=NOW.replace(hour=0, minute=10) + timedelta(days=1))
        result = store.add_news([after_midnight])
        assert result.written == 0
        assert store.counts()["total"] == 1


def test_최초_관측_시각이_유지된다():
    # known_at의 보수적 판정이 collected_at에 기댄다. 나중 값으로 덮으면 안 된다.
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        article = make(url="http://a/1")
        store.add_news([replace(article, collected_at=NOW, relevance="title")])
        store.add_news([replace(article, collected_at=NOW + timedelta(hours=5),
                                relevance="title")])
        rows = store.news_for("X", NOW + timedelta(days=1), 7)
        assert rows[0]["collected_at"] == NOW.isoformat()


def test_다른_종목의_같은_기사는_각각_남는다():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.add_news([make(url="http://a/1", symbol="005930"),
                        make(url="http://a/1", symbol="000660")])
        assert store.counts()["total"] == 2


def test_as_of_이후_기사는_조회되지_않는다():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.add_news([
            replace(make(url="a", published=NOW - timedelta(hours=2)), relevance="title"),
            replace(make(url="b", published=NOW + timedelta(hours=2),
                         collected=NOW + timedelta(hours=3)), relevance="title"),
        ])
        rows = store.news_for("X", NOW, 7)
        assert [r["url"] for r in rows] == ["a"]


def test_기본_조회는_1차자료만_돌려준다():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.add_news([
            replace(make(url="a"), relevance="title"),
            replace(make(url="b"), relevance="summary"),
            replace(make(url="c"), relevance="none", vendor_tagged=True),
        ])
        assert {r["url"] for r in store.news_for("X", NOW + timedelta(hours=1), 7)} == {"a", "c"}
        assert len(store.news_for("X", NOW + timedelta(hours=1), 7, primary_only=False)) == 3


def test_수집_실패는_기록으로_남는다():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.record_gap("KR", "naver", "005930", "HTTP 429")
        gaps = store.open_gaps()
        assert len(gaps) == 1 and gaps[0]["symbol"] == "005930"
        store.resolve_gap(gaps[0]["id"])
        assert store.open_gaps() == []


def test_수집_이력으로_마지막_실행_시각을_안다():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        assert store.last_run_at("005930", "naver") is None
        store.record_run("005930", "naver", ran_at=NOW, oldest_seen=NOW - timedelta(hours=3),
                         reached_floor=True, pages=2, fetched=200, inserted=180, duplicates=20)
        assert store.last_run_at("005930", "naver") == NOW


def test_원본_응답은_수집_1회를_한_파일에_담는다():
    # 페이지별로 쓰면 파일명이 수집 시각 하나로 같아져 서로를 덮어쓴다.
    # 실제로 8페이지를 받고 1개만 남은 적이 있다 (2026-09-15).
    import json
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        pages = [{"items": [1]}, {"items": [2]}, {"items": [3]}]
        path = store.save_raw("naver", "005930", pages, NOW)
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["page_count"] == 3
        assert len(saved["responses"]) == 3
        assert len(list(path.parent.glob("*.json"))) == 1


# ---- 수집 커버리지 ----

def test_커버리지_미달은_batch가_알린다():
    from src.data.naver_news import NewsBatch
    batch = NewsBatch(items=[], pages=10, oldest_seen=NOW, reached_floor=False)
    # lookback 경계에 못 닿았다 = 그만큼 구멍. 호출부가 gap으로 기록해야 한다.
    assert batch.reached_floor is False


def test_batch의_primary는_TITLE과_벤더태깅만():
    from dataclasses import replace
    from src.data.naver_news import NewsBatch
    items = [
        replace(make(url="a"), relevance="title"),
        replace(make(url="b"), relevance="summary"),
        replace(make(url="c"), relevance="none", vendor_tagged=True),
    ]
    batch = NewsBatch(items=items, pages=1, oldest_seen=NOW, reached_floor=True)
    assert [i.url for i in batch.primary] == ["a", "c"]


def test_US는_티커도_표기_후보다():
    from src.data.universe import Holding
    us = Holding("NVDA", "NVIDIA", "Technology", "US", "mktcap", "NVDA")
    kr = Holding("005930", "삼성전자", "반도체", "KR", "mktcap", "삼성전자", ("삼성전자",))
    assert "NVDA" in us.names
    # KR 종목코드는 제목에 나오지 않으므로 넣지 않는다.
    assert "005930" not in kr.names


def test_벤더_태깅_기사에도_관련성이_매겨진다():
    # 태깅은 "이 기사가 그 종목에 관한 것"을 보장하지 않는다.
    # NVDA 250건 중 제목 매칭은 17%였다 (2026-09-15 실측).
    from src.data.finnhub_news import parse_item as fparse
    row = {"datetime": 1789392614, "headline": "Chip stocks rally on AI demand",
           "summary": "Nvidia led gains", "url": "http://a", "source": "Reuters"}
    item = fparse(row, "NVDA", NOW)
    assert item.vendor_tagged is True
    assert classify_relevance(item, ["NVIDIA", "NVDA"]) is Relevance.SUMMARY


def test_저장된_등급을_다시_매길_수_있다():
    # 관련성 규칙은 앞으로도 바뀐다. 과거 구간은 다시 받을 수 없으므로
    # 이미 받은 것을 다시 해석할 수 있어야 한다.
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.add_news([replace(make(url="a", title="삼성전자 실적"), relevance="none")])
        rows = store.iter_rows()
        assert rows[0]["relevance"] == "none"
        store.update_relevance([("title", rows[0]["symbol"], rows[0]["content_hash"])])
        assert store.iter_rows()[0]["relevance"] == "title"


def test_교차표가_벤더태깅을_구분한다():
    # 등급만 세면 태깅된 기사가 'none'으로만 보여 오해를 부른다.
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.add_news([
            replace(make(url="a"), relevance="title"),
            replace(make(url="b"), relevance="none", vendor_tagged=True),
        ])
        matrix = {(m["relevance"], m["vendor_tagged"]): m["n"]
                  for m in store.relevance_matrix()}
        assert matrix[("title", 0)] == 1
        assert matrix[("none", 1)] == 1
        assert store.primary_count()["KR"] == 2   # 둘 다 1차 자료
