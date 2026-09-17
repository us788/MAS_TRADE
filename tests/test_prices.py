"""가격 어댑터 검증. 네트워크를 타지 않는다.

여기서 지키는 것은 **저울이 고장 나지 않는 것**이다. 특히 둘.

- `as_of` 시점에 모르던 종가를 돌려주면 룩어헤드다
- "5거래일 뒤"가 달력 5일이 되면 채점이 통째로 어긋난다

설계 근거는 `docs/harness.md`.
"""
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.data.prices import Bar, PriceStore, known_trading_date

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")


def _store(tmp):
    return PriceStore(db_path=Path(tmp) / "p.sqlite3")


def _bar(symbol, d, close, adj=None, market="KR", source="test"):
    return Bar(symbol=symbol, market=market, date=d, close_px=close,
               close_tr=close if adj is None else adj, source=source)


# ---------------------------------------------------------------- as-of 경계

def test_kr_종가는_장마감_30분_뒤부터_알_수_있다():
    d = date(2026, 9, 15)
    # 15:59 KST — 아직 모른다. 전날이 상한이다.
    assert known_trading_date("KR", datetime(2026, 9, 15, 15, 59, tzinfo=KST)) == d - timedelta(days=1)
    # 16:00 KST — 알 수 있다.
    assert known_trading_date("KR", datetime(2026, 9, 15, 16, 0, tzinfo=KST)) == d


def test_us_종가는_서머타임과_무관하게_마감_30분_뒤부터():
    # EDT (여름)
    assert known_trading_date("US", datetime(2026, 7, 15, 16, 29, tzinfo=NY)) == date(2026, 7, 14)
    assert known_trading_date("US", datetime(2026, 7, 15, 16, 30, tzinfo=NY)) == date(2026, 7, 15)
    # EST (겨울) — 고정 오프셋을 썼다면 여기서 깨진다
    assert known_trading_date("US", datetime(2026, 1, 15, 16, 29, tzinfo=NY)) == date(2026, 1, 14)
    assert known_trading_date("US", datetime(2026, 1, 15, 16, 30, tzinfo=NY)) == date(2026, 1, 15)


def test_kst_새벽_실행은_미국_종가가_하루_더_밀린다():
    """harness.md 2절의 근거. 이게 깨지면 기준가 시차 판단이 틀린 것이다."""
    # KST 02:00 = ET 전날 12~13시 → 그 세션은 아직 안 끝났다
    early = known_trading_date("US", datetime(2026, 7, 16, 2, 0, tzinfo=KST))
    # KST 07:00 = ET 전날 17~18시 → 끝났다
    late = known_trading_date("US", datetime(2026, 7, 16, 7, 0, tzinfo=KST))
    assert early == date(2026, 7, 14)
    assert late == date(2026, 7, 15)
    assert (late - early).days == 1


def test_타임존_없는_as_of는_거부한다():
    with pytest.raises(ValueError):
        known_trading_date("KR", datetime(2026, 9, 15, 16, 0))


def test_close_at은_as_of_이후_봉을_주지_않는다():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.upsert([_bar("005930", date(2026, 9, 14), 100),
                  _bar("005930", date(2026, 9, 15), 200)])
        # 09-15 15:00 KST — 그날 종가는 아직 모른다
        got = s.close_at("005930", "KR", datetime(2026, 9, 15, 15, 0, tzinfo=KST))
        assert got.date == date(2026, 9, 14) and got.close_px == 100
        # 16:00 이후면 안다
        got = s.close_at("005930", "KR", datetime(2026, 9, 15, 16, 0, tzinfo=KST))
        assert got.date == date(2026, 9, 15) and got.close_px == 200


def test_휴장일에_걸리면_직전_거래일_봉을_준다():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.upsert([_bar("005930", date(2026, 9, 11), 100)])   # 금
        # 9/13은 일요일 — 봉이 없다. 상한 이하의 마지막 봉을 집는다.
        got = s.close_at("005930", "KR", datetime(2026, 9, 13, 20, 0, tzinfo=KST))
        assert got.date == date(2026, 9, 11)


# ------------------------------------------------------------- 거래일 이동

def test_5거래일은_달력_5일이_아니다():
    """휴장일이 끼면 달력으로는 7일이 된다. 캘린더 없이 시계열로 센다."""
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        # 9/14(월)~9/18(금), 9/21(월)~9/22(화). 주말은 봉이 없다.
        days = [date(2026, 9, d) for d in (14, 15, 16, 17, 18, 21, 22)]
        s.upsert([_bar("005930", d, 100 + i) for i, d in enumerate(days)])

        got = s.forward_bar("005930", date(2026, 9, 14), 5)
        assert got.date == date(2026, 9, 21)              # 달력으로는 7일 뒤
        assert (got.date - date(2026, 9, 14)).days == 7


def test_기준일이_휴장일이어도_다음_거래일부터_센다():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        days = [date(2026, 9, d) for d in (11, 14, 15)]
        s.upsert([_bar("005930", d, 100) for d in days])
        # 9/13은 일요일
        assert s.forward_bar("005930", date(2026, 9, 13), 1).date == date(2026, 9, 14)


def test_아직_안_온_미래는_실패가_아니라_None이다():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.upsert([_bar("005930", date(2026, 9, 14), 100),
                  _bar("005930", date(2026, 9, 15), 101)])
        assert s.forward_bar("005930", date(2026, 9, 14), 1) is not None
        assert s.forward_bar("005930", date(2026, 9, 14), 5) is None   # 미채점
        assert s.latest_date("005930") == date(2026, 9, 15)            # 구분 근거


def test_forward_days는_1_이상이어야_한다():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        with pytest.raises(ValueError):
            s.forward_bar("005930", date(2026, 9, 14), 0)


# ------------------------------------------------------- 가격수익률 / 총수익률
#
# 두 열의 차이는 분할이 아니라 **배당**이다. 벤더가 분할은 시계열에 소급 적용해서
# 준다 (2026-09-16 실측: NVDA 10:1 분할 전 봉이 44.78로 들어온다).

def test_배당이_반영되면_px는_그대로고_tr만_내려간다():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.upsert([Bar("XOM", "US", date(2026, 9, 14), close_px=100.0,
                      close_tr=100.0, source="t")])
        # 배당락 반영 — 과거 총수익 계열만 소급 조정된다
        r = s.upsert([Bar("XOM", "US", date(2026, 9, 14), close_px=100.0,
                          close_tr=99.0, source="t")])
        assert r.revised == 1 and r.conflicted == 0

        got = s.close_at("XOM", "US", datetime(2026, 9, 14, 17, 0, tzinfo=NY))
        assert got.close_px == 100.0    # 가격 계열은 안 바뀐다
        assert got.close_tr == 99.0     # 총수익 계열만 바뀐다


def test_가격_계열이_바뀌면_덮지_않고_기록만_한다():
    """분할이 새로 반영됐거나 벤더가 값을 고친 것이다. 코드가 구분할 수 없으므로 보류한다."""
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.upsert([Bar("005930", "KR", date(2026, 9, 14), close_px=100,
                      close_tr=100, source="t")])
        r = s.upsert([Bar("005930", "KR", date(2026, 9, 14), close_px=999,
                          close_tr=100, source="t")])
        assert r.conflicted == 1 and r.revised == 0
        assert s.close_at("005930", "KR",
                          datetime(2026, 9, 14, 16, 0, tzinfo=KST)).close_px == 100


def test_가격_계열이_바뀌면_같은_응답의_총수익_계열도_보류한다():
    """한 열만 골라 받으면 행이 내부적으로 어긋난다."""
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.upsert([Bar("005930", "KR", date(2026, 9, 14), close_px=100,
                      close_tr=100, source="t")])
        r = s.upsert([Bar("005930", "KR", date(2026, 9, 14), close_px=999,
                          close_tr=555, source="t")])
        assert r.conflicted == 1 and r.revised == 0
        got = s.close_at("005930", "KR", datetime(2026, 9, 14, 16, 0, tzinfo=KST))
        assert got.close_px == 100 and got.close_tr == 100   # 둘 다 그대로


def test_같은_값을_다시_넣으면_아무것도_안_바뀐다():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.upsert([_bar("005930", date(2026, 9, 14), 100)])
        r = s.upsert([_bar("005930", date(2026, 9, 14), 100)])
        assert r == type(r)(inserted=0, revised=0, conflicted=0, unchanged=1)


# -------------------------------------------------------------------- 결측

def test_구간_안의_빠진_평일을_찾는다():
    """거래일 인덱스 이동이 결측 없음을 전제하므로 감시가 필요하다."""
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        # 9/14(월), 9/16(수) — 9/15(화)가 빠졌다
        s.upsert([_bar("005930", date(2026, 9, 14), 100),
                  _bar("005930", date(2026, 9, 16), 102)])
        assert s.missing_weekdays("005930") == [date(2026, 9, 15)]


def test_주말은_결측으로_세지_않는다():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.upsert([_bar("005930", date(2026, 9, 11), 100),    # 금
                  _bar("005930", date(2026, 9, 14), 101)])   # 월
        assert s.missing_weekdays("005930") == []


def test_gap은_예외가_아니라_기록이다():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.record_gap("005930", date(2026, 9, 1), date(2026, 9, 5), "벤더 빈 응답")
        gaps = s.open_gaps()
        assert len(gaps) == 1 and gaps[0]["reason"] == "벤더 빈 응답"
        s.resolve_gap(gaps[0]["id"])
        assert s.open_gaps() == []


# ---- 미완결 세션 차단 (2026-09-18 실제 장애) ----

def test_아직_안_끝난_세션의_봉은_버린다():
    """장중 스냅샷이 종가로 저장되면 틀린 값이 영구히 남는다.

    실제로 당했다 — 한국 장 마감 14분 전에 수집해 삼성전자 09-16 종가가
    253,250(장중)으로 저장됐고, 진짜 종가 253,500은 `close_px` 변경으로 판정돼
    "벤더 오류 의심"으로 거부됐다.
    """
    from src.data.price_sources import drop_unclosed
    today = date.today()
    bars = [_bar("X", today - timedelta(days=10), 100, market="US"),
            _bar("X", today + timedelta(days=1), 200, market="US")]
    kept = drop_unclosed(bars, "US")
    assert [b.date for b in kept] == [today - timedelta(days=10)]


def test_조회와_저장이_같은_규칙을_쓴다():
    """`known_trading_date`를 조회에만 쓰고 저장에 안 쓰면 그 틈으로 샌다."""
    from src.data.price_sources import drop_unclosed
    from src.data.prices import known_trading_date
    now = datetime.now(timezone.utc)
    for market in ("KR", "US"):
        cut = known_trading_date(market, now)
        bars = [_bar("X", cut, 100), _bar("X", cut + timedelta(days=1), 200)]
        assert [b.date for b in drop_unclosed(bars, market)] == [cut]


# ---- 비교 허용오차 ----

def test_부동소수점_잡음은_수정으로_기록하지_않는다():
    """1e-9로 뒀더니 수정 이력 4,759건 중 4,744건이 잡음이었다."""
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.upsert([_bar("005930", date(2026, 9, 14), 100.0)])
        r = s.upsert([_bar("005930", date(2026, 9, 14), 100.0 + 1e-8)])
        assert r.unchanged == 1 and r.revised == 0 and r.conflicted == 0


def test_의미_있는_변화는_여전히_잡는다():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.upsert([_bar("005930", date(2026, 9, 14), 100.0)])
        r = s.upsert([_bar("005930", date(2026, 9, 14), 100.01)])
        assert r.conflicted == 1


# ---- 강제 재적재 ----

def test_force면_보류_규칙을_넘어_덮는다():
    """미완결 봉이 저장돼 정정이 거부된 경우를 사람이 풀어줄 경로."""
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.upsert([_bar("005930", date(2026, 9, 16), 253250)])     # 장중 스냅샷
        r = s.upsert([_bar("005930", date(2026, 9, 16), 253500)], force=True)
        assert r.revised == 1 and r.conflicted == 0
        got = s.close_at("005930", "KR", datetime(2026, 9, 16, 16, 0, tzinfo=KST))
        assert got.close_px == 253500


def test_force가_없으면_그대로_보류한다():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.upsert([_bar("005930", date(2026, 9, 16), 253250)])
        r = s.upsert([_bar("005930", date(2026, 9, 16), 253500)])
        assert r.conflicted == 1 and r.revised == 0
