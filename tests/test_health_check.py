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


def test_단발_구멍은_경고에_그친다():
    """수집기가 "받을 게 없다"고 건너뛴 한 시간은 손실이 아니다.

    단발까지 심각으로 올리면 매일 울리고, 그러면 진짜 구멍을 흘려보낸다.
    """
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        cur = NOW - timedelta(hours=24)
        while cur <= NOW:
            k = cur.astimezone(KST)
            if not (k.hour == 11 and k.weekday() < 5):     # 장중 한 시간만 비움
                _run(s, "005930", cur)
            cur += timedelta(hours=1)
        r = hc.check_market_hours_holes(s, NOW)
        assert r["state"] == hc.WARN and "최장 연속 1" in r["detail"]


def test_붙어있는_시간만_연속으로_센다():
    assert hc._adjacent_hour("09-28 11시", "09-28 12시")
    assert not hc._adjacent_hour("09-28 11시", "09-28 13시")
    assert not hc._adjacent_hour("09-27 15시", "09-28 09시")   # 날이 다르면 안 잇는다


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


# ---- 멈춘 작업 판정 (2026-10-07) ----
#
# 종료코드 0만 보다가 주간 베이스라인이 한 주 통째로 죽은 것을 못 잡았다.
# 판정의 재료는 `ps -o etime`인데 macOS ps에는 초 단위 `etimes`가 없다.

def test_etime을_초로_읽는다():
    assert hc._etime_seconds("01:51") == 111                 # MM:SS
    assert hc._etime_seconds("1:02:03") == 3723              # HH:MM:SS
    assert hc._etime_seconds("07-00:18:36") == 605916        # DD-HH:MM:SS  (실제 장애값)


def test_etime이_이상하면_None이다():
    """ps가 에러 문자열을 뱉을 수 있다. 그걸 숫자로 읽으면 판정이 조용히 틀린다."""
    assert hc._etime_seconds("") is None
    assert hc._etime_seconds("ps: etimes: keyword not found") is None
    assert hc._etime_seconds(None) is None


def test_멈춘_기준은_정상_회차보다_넉넉하다():
    """09-30 실측 KR 59분 + US 10분. 정상 회차가 걸리면 거짓 경보가 된다."""
    assert hc.STUCK_JOB_SECONDS > 2 * 3600
    assert hc._etime_seconds("07-00:18:36") > hc.STUCK_JOB_SECONDS   # 장애는 잡힌다
    assert hc._etime_seconds("01:12:00") < hc.STUCK_JOB_SECONDS      # 정상은 안 잡힌다


# ---- 창 경계 (2026-10-07) ----

def test_반만_걸친_첫_시간은_판정하지_않는다():
    """창이 10:29에 시작하면 10:19의 수집은 10분 차이로 밖이다. 그 시간을 구멍으로
    세면, **같은 데이터가 창이 미끄러질 때마다 경고에서 심각으로 바뀐다.**
    실제로 09시·11시(연속 1)가 다음날 10시·11시(연속 2)로 바뀌었다.
    """
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        base = datetime(2026, 10, 7, 1, 19, tzinfo=timezone.utc)   # 10:19 KST
        _run(s, "005930", base)
        for h in (3, 4, 5):                                        # 12·13·14시 KST
            _run(s, "005930", base.replace(hour=h, minute=5))
        # 지금은 다음날 01:29 UTC(10:30 KST) — 창 시작이 10:29라 10:19가 밖이다
        now = datetime(2026, 10, 8, 1, 29, tzinfo=timezone.utc)
        r = hc.check_market_hours_holes(s, now)
        assert "10-07 10시" not in r["detail"]      # 반만 걸친 시간은 빼고
        assert r["state"] != hc.CRIT                # 연속 2로 부풀지 않는다


def test_온전히_들어온_시간의_구멍은_그대로_잡는다():
    """경계를 느슨하게 만든 대가로 진짜 구멍을 놓치면 안 된다."""
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        base = datetime(2026, 10, 7, 0, 0, tzinfo=timezone.utc)    # 09:00 KST
        _run(s, "005930", base)
        # 10·11시 KST(01·02 UTC)를 통째로 비운다
        for h in (3, 4, 5):
            _run(s, "005930", base.replace(hour=h))
        now = datetime(2026, 10, 7, 6, 0, tzinfo=timezone.utc)     # 15:00 KST
        r = hc.check_market_hours_holes(s, now)
        assert r["state"] == hc.CRIT                                # 연속 2 = 심각
        assert "10시" in r["detail"] and "11시" in r["detail"]


# ---- macOS 알림 (2026-10-15) ----
#
# 조용히 죽는 것을 잡으려고 만든 장치가 2026-09-17부터 조용히 죽어 있었다.
# 22번 전부 실패했고 에러는 health.log에만 남았다.

def test_알림에_유니코드_이스케이프를_넣지_않는다():
    """`json.dumps` 기본값은 한글을 `\\uXXXX`로 바꾸는데 **AppleScript는 `\\u`를 모른다.**
    점검 항목 이름이 전부 한글이라 이상이 있을 때마다 100% 실패했다.
    """
    script = hc._notification_script("한국 장중 커버리지: 3시간 비어 있다", hc.WARN)
    assert "\\u" not in script                      # 이스케이프가 없어야 하고
    assert "한국 장중 커버리지" in script              # 한글이 그대로 들어가야 한다


def test_알림이_따옴표를_이스케이프한다():
    """본문에 따옴표가 섞여도 AppleScript 문자열이 깨지면 안 된다."""
    script = hc._notification_script('벤더가 "빈 응답"을 줬다', hc.CRIT)
    assert '\\"' in script
    assert script.startswith("display notification ")
    assert script.endswith(f'with title "MAS_TRADE 점검: {hc.CRIT}"')
