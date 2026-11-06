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


def test_새_영구_손실은_심각으로_뜬다():
    """막 난 영구 손실은 재수집 실패와 다르게 취급한다."""
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.record_gap("KR", "naver", "005930", "커버리지 미달: 10페이지로 ...", NOW)
        r = hc.check_gaps(s, NOW)
        assert r["state"] == hc.CRIT and "새 영구 손실" in r["detail"]


def test_수집_실패_gap만_있으면_경고다():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.record_gap("KR", "naver", "005930", "VendorError: DNS", NOW)
        assert hc.check_gaps(s, NOW)["state"] == hc.WARN


def test_gap이_없으면_ok다():
    with tempfile.TemporaryDirectory() as tmp:
        assert hc.check_gaps(_store(tmp), NOW)["state"] == hc.OK


# ---- 새 손실과 이미 아는 손실을 가른다 (2026-11-06) ----
#
# 커버리지 미달은 영구 손실이라 설계상 닫히지 않는다. 그래서 한 번 나면 매일 심각이
# 되고, 그 안에서 새로 난 손실이 묻힌다 — 10-29·10-30의 두 건을 알아채는 데 엿새가
# 걸렸다. 영구 손실에는 조치가 없으므로, 한 번 알렸으면 조용해지는 것이 맞다.

def test_사흘_지난_영구_손실은_심각에서_내려온다():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.record_gap("KR", "naver", "005930", "커버리지 미달: ...",
                     NOW - timedelta(days=5))
        r = hc.check_gaps(s, NOW)
        assert r["state"] == hc.WARN
        assert "되돌릴 수 없다" in r["detail"]


def test_조용해져도_누적_건수는_계속_보인다():
    """조용해지는 것과 잊히는 것은 다르다."""
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        for d in (5, 9, 20):
            s.record_gap("KR", "naver", "000270", "커버리지 미달: ...",
                         NOW - timedelta(days=d))
        r = hc.check_gaps(s, NOW)
        assert r["state"] != hc.CRIT
        assert "누적 3건" in r["detail"]


def test_쌓인_것_사이에_새_손실이_섞이면_잡는다():
    """**이 테스트가 이 변경의 이유다.** 10-17의 2건에 묻혀 10-29·10-30을 엿새 동안
    못 봤다. 묵은 것이 아무리 많아도 새 것 하나면 심각이어야 한다.
    """
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        for d in (12, 13, 20, 25):
            s.record_gap("KR", "naver", "000270", "커버리지 미달: ...",
                         NOW - timedelta(days=d))
        s.record_gap("KR", "naver", "000270", "커버리지 미달: ...",
                     NOW - timedelta(hours=6))          # 오늘 난 것
        r = hc.check_gaps(s, NOW)
        assert r["state"] == hc.CRIT
        assert "새 영구 손실 1건" in r["detail"]
        assert "누적 5건" in r["detail"]


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


# ---- 멈춤 판정은 진행 흔적으로 한다 (2026-11-05) ----
#
# 벽시계로는 진짜 멈춤과 자다 깬 정상 회차가 같은 축에 있다.
# 임계값을 어디 두든 한쪽이 틀린다 — 실측 셋이 그걸 보여준다.

HOUR = 3600


def test_오래_떠_있어도_방금_진행했으면_멈춘_게_아니다():
    """2026-11-04 실측. 벽시계 9시간 49분인데 최근 1시간에 LLM 호출 25건이었다.
    예전 규칙(벽시계 4시간)은 이걸 심각으로 올려 거짓 경보를 냈다.
    """
    assert hc._stuck_verdict(9 * HOUR + 49 * 60, idle_seconds=141) is False


def test_자다_깬_정상_회차도_멈춘_게_아니다():
    """2026-10-14 실측. 벽시계 7시간 51분, 그래도 진행 중이었다."""
    assert hc._stuck_verdict(7 * HOUR + 51 * 60, idle_seconds=5 * 60) is False


def test_진짜_멈춤은_그대로_잡는다():
    """2026-10-07 실측. nslookup이 7일째 안 끝나 주간 베이스라인이 한 주 죽었다.
    느슨하게 만든 대가로 이걸 놓치면 안 된다.
    """
    assert hc._stuck_verdict(7 * 24 * HOUR, idle_seconds=7 * 24 * HOUR) is True


def test_진행_흔적이_없으면_보수적으로_멈춤으로_본다():
    """흔적이 아예 없는 작업(prices)은 벽시계로 판단할 수밖에 없다."""
    assert hc._stuck_verdict(5 * HOUR, idle_seconds=None) is True


def test_짧게_떠_있으면_진행과_무관하게_정상이다():
    assert hc._stuck_verdict(HOUR, idle_seconds=None) is False
    assert hc._stuck_verdict(None, idle_seconds=None) is False


def test_무진행_기준이_정상_회차_간격보다_넉넉하다():
    """LLM 호출 간격이 수십 초~수 분이다. 기준이 너무 빡빡하면 또 거짓 경보가 난다."""
    assert hc.NO_PROGRESS_SECONDS >= 15 * 60
