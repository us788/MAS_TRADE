"""매크로 vintage 검증. 네트워크를 타지 않는다.

여기서 지키는 것은 하나다 — **그 시점에 알 수 있었던 값만 돌려주는 것.**
매크로는 사후 수정이 일상이라 vintage 없이 받으면 조용히 룩어헤드가 된다.
실측(2026-11-24): 2026-04-01 실질 GDP가 당시 24,269.6인데 지금 받으면 24,408.0이다.
"""
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.data.macro import (ET, MacroPoint, MacroStore, SERIES, _point,
                            vintage_date)

KST = ZoneInfo("Asia/Seoul")


def _store(tmp):
    return MacroStore(db_path=Path(tmp) / "m.sqlite3")


def _pt(series, d, value, vintage):
    return MacroPoint(series, date.fromisoformat(d), value, date.fromisoformat(vintage))


# ---- vintage 날짜 ----

def test_시그널_시각은_마감_뒤라_당일_vintage다():
    """수 07:00 KST = 화 17:00 ET. 미국장 마감 뒤라 그날 발표분이 다 나와 있다."""
    assert vintage_date(datetime(2026, 11, 18, 7, 0, tzinfo=KST)) == date(2026, 11, 17)


def test_마감_전이면_하루_물린다():
    """CPI는 08:30 ET, FOMC는 14:00 ET다. 같은 날이어도 시각에 따라 아는 것이 다르다."""
    before = datetime(2026, 11, 17, 10, 0, tzinfo=ET)      # 16:30 전
    assert vintage_date(before) == date(2026, 11, 16)
    after = datetime(2026, 11, 17, 16, 30, tzinfo=ET)      # 경계 포함
    assert vintage_date(after) == date(2026, 11, 17)


def test_타임존이_없으면_거부한다():
    """날짜가 아니라 타임스탬프여야 한다. 날짜로 조인하면 장중 발표가 샌다."""
    with pytest.raises(ValueError):
        vintage_date(datetime(2026, 11, 17, 10, 0))


# ---- 파싱 ----

def test_결측은_0이_아니라_None이다():
    """FRED는 결측을 '.'으로 준다. 0으로 바꾸면 없는 값이 숫자가 된다."""
    assert _point("DGS10", {"date": "2026-11-17", "value": "."}, date(2026, 11, 17)).value is None
    assert _point("DGS10", {"date": "2026-11-17", "value": "5.12"}, date(2026, 11, 17)).value == 5.12


# ---- 저장 ----

def test_같은_관측이_vintage마다_따로_남는다():
    """**이 테이블의 존재 이유다.** 덮어쓰면 사후 수정이 과거를 삼킨다."""
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.save([_pt("GDPC1", "2026-04-01", 24269.613, "2026-09-15"),
                _pt("GDPC1", "2026-04-01", 24408.011, "2026-11-17")])
        assert s.counts()["total"] == 2


def test_그_시점에_알려져_있던_값을_돌려준다():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.save([_pt("GDPC1", "2026-04-01", 24269.613, "2026-09-15"),
                _pt("GDPC1", "2026-04-01", 24408.011, "2026-11-17")])
        old = s.snapshot_as_of(datetime(2026, 9, 16, 7, 0, tzinfo=KST), ["GDPC1"])["GDPC1"]
        new = s.snapshot_as_of(datetime(2026, 11, 18, 7, 0, tzinfo=KST), ["GDPC1"])["GDPC1"]
        assert old.value == 24269.613       # 수정 전 값
        assert new.value == 24408.011


def test_미래_vintage는_보이지_않는다():
    """미발표 관측이 새어 들어오면 그게 가장 나쁜 룩어헤드다."""
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.save([_pt("UNRATE", "2026-10-01", 4.1, "2026-11-17")])
        early = s.snapshot_as_of(datetime(2026, 9, 16, 7, 0, tzinfo=KST), ["UNRATE"])
        assert early["UNRATE"] is None


def test_정확히_일치하는_vintage가_없으면_직전_것을_쓴다():
    """주 1회 수집이라 그 사이 시점은 직전 vintage를 쓴다.
    더 오래된 vintage는 정보가 **덜** 들어 있으므로 보수적이고 룩어헤드가 아니다.
    """
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.save([_pt("DGS10", "2026-11-10", 5.0, "2026-11-10")])
        got = s.snapshot_as_of(datetime(2026, 11, 18, 7, 0, tzinfo=KST), ["DGS10"])["DGS10"]
        assert got.value == 5.0 and got.vintage == date(2026, 11, 10)


def test_결측은_건너뛰고_그_전_관측을_준다():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.save([_pt("DGS10", "2026-11-16", 5.12, "2026-11-17"),
                _pt("DGS10", "2026-11-17", None, "2026-11-17")])
        got = s.snapshot_as_of(datetime(2026, 11, 18, 7, 0, tzinfo=KST), ["DGS10"])["DGS10"]
        assert got.date == date(2026, 11, 16) and got.value == 5.12


def test_계열_목록에_일별과_월별이_함께_있다():
    """일별이 주간 변동을 만들고, 월별이 시점 정합성 검증을 맡는다."""
    assert {"DGS10", "T10Y2Y", "BAMLH0A0HYM2", "VIXCLS"} <= set(SERIES)   # 일별
    assert {"UNRATE", "CPIAUCSL"} <= set(SERIES)                          # 월별
