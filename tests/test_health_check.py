"""일일 점검 판정 검증. 네트워크를 타지 않는다.

**장중 구멍 판정이 틀리면 점검 자체가 무의미해진다.** 조용히 죽는 것을 잡으려고
만든 것인데 판정이 조용히 틀리면 두 배로 나쁘다.
"""
import importlib.util
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.data.storage import Store

spec = importlib.util.spec_from_file_location(
    "hc", Path(__file__).resolve().parent.parent / "scripts" / "health_check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

KST = timezone(timedelta(hours=9))


def _store(tmp):
    return Store(db_path=Path(tmp) / "t.sqlite3", raw_dir=Path(tmp) / "raw")


def _run(store, symbol, at):
    store.record_run(symbol, "naver", ran_at=at, oldest_seen=at - timedelta(hours=2),
                     reached_floor=True, pages=1, fetched=1, inserted=1, duplicates=0)


# 목요일 18:00 KST = 09:00 UTC. 그 전 24시간에 장중(09~16 KST)이 들어온다.
NOW = datetime(2026, 9, 17, 9, 0, tzinfo=timezone.utc)


def test_장중이_다_덮이면_ok다():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        cur = NOW - timedelta(hours=24)
        while cur <= NOW:                      # 매시 수집한 상태
            _run(s, "005930", cur)
            cur += timedelta(hours=1)
        assert hc.check_market_hours_holes(s, NOW)["state"] == hc.OK


def test_장중에_빈_시간이_있으면_심각이다():
    """과거 뉴스는 다시 살 수 없다. 경고가 아니라 심각이다."""
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        cur = NOW - timedelta(hours=24)
        while cur <= NOW:
            k = cur.astimezone(KST)
            if not (k.hour in (11, 12) and k.weekday() < 5):   # 장중 두 시간 비움
                _run(s, "005930", cur)
            cur += timedelta(hours=1)
        r = hc.check_market_hours_holes(s, NOW)
        assert r["state"] == hc.CRIT and "2시간" in r["detail"]


def test_장_밖의_빈_시간은_문제가_아니다():
    """야간 구멍은 다음 수집의 lookback이 메운다."""
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        cur = NOW - timedelta(hours=24)
        while cur <= NOW:
            k = cur.astimezone(KST)
            if not (3 <= k.hour <= 5):          # 새벽만 비움
                _run(s, "005930", cur)
            cur += timedelta(hours=1)
        assert hc.check_market_hours_holes(s, NOW)["state"] == hc.OK


def test_커버리지_미달_gap은_심각으로_뜬다():
    """영구 손실이므로 재수집 실패와 다르게 취급한다."""
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.record_gap("KR", "naver", "005930", "커버리지 미달: 10페이지로 ...", NOW)
        assert hc.check_gaps(s)["state"] == hc.CRIT


def test_수집_실패_gap만_있으면_경고다():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.record_gap("KR", "naver", "005930", "VendorError: DNS", NOW)
        assert hc.check_gaps(s)["state"] == hc.WARN


def test_gap이_없으면_ok다():
    with tempfile.TemporaryDirectory() as tmp:
        assert hc.check_gaps(_store(tmp))["state"] == hc.OK
