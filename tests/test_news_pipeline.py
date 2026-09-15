"""뉴스 파이프라인 검증. 네트워크를 타지 않는다.

지키는 것: 시점 정합(known_at), 동명이의 필터, 타임존 정규화, 중복 제거.
"""
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.data.finnhub_news import parse_item as finnhub_parse
from src.data.naver_news import parse_item as naver_parse, parse_pub_date, publisher_from
from src.data.news import NewsItem, clean_text, filter_as_of, matches_symbol
from src.data.storage import SnapshotStore

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


# ---- 동명이의 필터 ----

def test_기아대책_기사는_걸러진다():
    item = make(title="기아대책, 아동 후원 캠페인 진행")
    assert matches_symbol(item, ["기아"], ["기아대책"]) is False


def test_기아_자동차_기사는_통과한다():
    item = make(title="기아, 3분기 판매량 증가")
    assert matches_symbol(item, ["기아"], ["기아대책"]) is True


def test_한화에어로_야구_기사는_걸러진다():
    item = make(title="한화 이글스, 야구 경기 승리")
    assert matches_symbol(item, ["한화에어로"], ["이글스", "야구"]) is False


def test_require가_비면_통과시킨다():
    # 미국은 벤더가 티커를 태깅해주므로 재확인이 필요 없다.
    assert matches_symbol(make(title="anything"), [], []) is True


def test_require에_하나도_안걸리면_버린다():
    assert matches_symbol(make(title="다른 회사 뉴스"), ["삼성전자"], []) is False


# ---- 매체명 ----

def test_원문_도메인을_매체명으로_쓴다():
    assert publisher_from("https://www.hankyung.com/article/123") == "hankyung.com"
    assert publisher_from("") == ""


# ---- 저장소 ----

def test_같은_기사를_두번_저장하지_않는다():
    with tempfile.TemporaryDirectory() as tmp:
        store = SnapshotStore(root=Path(tmp) / "snap", log_dir=Path(tmp) / "logs")
        items = [make(url="http://a/1"), make(url="http://a/2")]
        first = store.append_news(items)
        second = store.append_news(items)          # 같은 기사 재수집
        assert (first.written, first.skipped_duplicate) == (2, 0)
        assert (second.written, second.skipped_duplicate) == (0, 2)
        assert len(store.read_news("KR", NOW, "test")) == 2


def test_다른_종목의_같은_기사는_각각_남는다():
    # 한 기사가 두 종목에 모두 해당할 수 있다. 종목별로 1건씩이 맞다.
    with tempfile.TemporaryDirectory() as tmp:
        store = SnapshotStore(root=Path(tmp) / "snap", log_dir=Path(tmp) / "logs")
        store.append_news([make(url="http://a/1", symbol="005930"),
                           make(url="http://a/1", symbol="000660")])
        assert len(store.read_news("KR", NOW, "test")) == 2


def test_수집_실패는_기록으로_남는다():
    with tempfile.TemporaryDirectory() as tmp:
        store = SnapshotStore(root=Path(tmp) / "snap", log_dir=Path(tmp) / "logs")
        store.record_gap("KR", "naver", "005930", "HTTP 429")
        gaps = store.open_gaps()
        assert len(gaps) == 1 and gaps[0]["symbol"] == "005930"


def test_원본_응답을_따로_남긴다():
    with tempfile.TemporaryDirectory() as tmp:
        store = SnapshotStore(root=Path(tmp) / "snap", log_dir=Path(tmp) / "logs")
        path = store.save_raw("naver", "005930", {"items": []}, NOW)
        assert path.exists() and "005930" in path.name
