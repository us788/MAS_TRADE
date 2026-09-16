"""시그널 채점 검증. 네트워크를 타지 않는다.

여기서 지킬 핵심은 둘이다.

- **"올랐으니 맞혔다"로 새지 않는 것.** 적중은 지수 대비 초과로 판정한다(기획서 9.1).
- **못 잰 것을 틀린 것으로 세지 않는 것.** pending과 stale을 0점으로 세면 성적이
  조용히 왜곡된다.
"""
import math
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.data.prices import Bar, PriceStore
from src.eval.scoring import COSTS, Scorer, summarize

KST = ZoneInfo("Asia/Seoul")
KR_INDEX = "KS200"


@dataclass
class Sig:
    """채점기가 실제로 읽는 필드만 가진 최소 시그널."""
    symbol: str
    market: str
    as_of: datetime
    direction: str
    confidence: float = 0.5


def _days(n, start=date(2026, 1, 5)):
    """주말을 건너뛴 거래일 n개."""
    out, cur = [], start
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur)
        cur += timedelta(days=1)
    return out


def _fill(store, symbol, dates, prices, market="KR"):
    store.upsert([Bar(symbol, market, d, close_px=p, close_tr=p, source="t")
                  for d, p in zip(dates, prices)])


def _setup(tmp, sym_prices, idx_prices, n=None, today=date(2026, 3, 1)):
    store = PriceStore(db_path=Path(tmp) / "s.sqlite3")
    dates = _days(n or len(sym_prices))
    _fill(store, "005930", dates, sym_prices)
    _fill(store, KR_INDEX, dates, idx_prices, market="INDEX")
    return store, dates, Scorer(store, horizons=(5,), today=today)


# ------------------------------------------------- 적중은 초과수익으로 판정한다

def test_올랐어도_지수보다_못하면_buy는_틀린_것이다():
    """이게 깨지면 저울이 고장 난 것이다."""
    with tempfile.TemporaryDirectory() as tmp:
        sym = [100, 100, 100, 100, 100, 103]     # +3%
        idx = [100, 100, 100, 100, 100, 105]     # +5%
        store, dates, scorer = _setup(tmp, sym, idx)
        r = scorer.score(Sig("005930", "KR", datetime(2026, 1, 5, 16, 0, tzinfo=KST), "buy"))
        h = r.horizons[5]
        assert h.status == "scored"
        assert math.isclose(h.symbol_return, 0.03)
        assert math.isclose(h.index_return, 0.05)
        assert math.isclose(h.excess, -0.02)
        assert h.hit is False          # 올랐지만 틀렸다


def test_떨어졌어도_지수보다_덜_떨어지면_buy는_맞힌_것이다():
    with tempfile.TemporaryDirectory() as tmp:
        sym = [100, 100, 100, 100, 100, 98]      # -2%
        idx = [100, 100, 100, 100, 100, 95]      # -5%
        store, dates, scorer = _setup(tmp, sym, idx)
        r = scorer.score(Sig("005930", "KR", datetime(2026, 1, 5, 16, 0, tzinfo=KST), "buy"))
        assert r.horizons[5].hit is True


def test_sell은_덜_오르면_맞힌_것이다():
    with tempfile.TemporaryDirectory() as tmp:
        sym = [100, 100, 100, 100, 100, 103]
        idx = [100, 100, 100, 100, 100, 105]
        store, dates, scorer = _setup(tmp, sym, idx)
        r = scorer.score(Sig("005930", "KR", datetime(2026, 1, 5, 16, 0, tzinfo=KST), "sell"))
        h = r.horizons[5]
        assert math.isclose(h.directional, 0.02)     # 부호가 뒤집힌다
        assert h.hit is True


def test_hold은_적중_판정에서_빠진다():
    with tempfile.TemporaryDirectory() as tmp:
        store, dates, scorer = _setup(tmp, [100] * 5 + [110], [100] * 5 + [100])
        r = scorer.score(Sig("005930", "KR", datetime(2026, 1, 5, 16, 0, tzinfo=KST), "hold"))
        h = r.horizons[5]
        assert h.status == "scored"
        assert h.excess is not None      # 초과수익 자체는 기록한다
        assert h.directional is None and h.hit is None


# -------------------------------------------------------------- 비용

def test_비용은_왕복_1회분을_뺀다():
    with tempfile.TemporaryDirectory() as tmp:
        store, dates, scorer = _setup(tmp, [100] * 5 + [110], [100] * 5 + [100])
        r = scorer.score(Sig("005930", "KR", datetime(2026, 1, 5, 16, 0, tzinfo=KST), "buy"))
        h = r.horizons[5]
        assert math.isclose(h.directional - h.directional_after_cost,
                            COSTS["KR"].roundtrip)


def test_kr_왕복비용은_수수료와_슬리피지_양방향에_거래세_한번이다():
    c = COSTS["KR"]
    assert math.isclose(c.roundtrip, (2 * 1.5 + 2 * 5.0 + 20.0) / 10_000)
    assert math.isclose(COSTS["US"].roundtrip, (2 * 5.0 + 2 * 5.0) / 10_000)


# ------------------------------------------------ 못 잰 것은 틀린 것이 아니다

def test_아직_안_지난_구간은_pending이다():
    """실패가 아니다. 0점으로 세면 성적이 왜곡된다."""
    today = date(2026, 1, 9)       # 데이터 끝(01-07) 이틀 뒤
    with tempfile.TemporaryDirectory() as tmp:
        store, dates, scorer = _setup(tmp, [100, 101, 102], [100, 100, 100], today=today)
        r = scorer.score(Sig("005930", "KR", datetime(2026, 1, 5, 16, 0, tzinfo=KST), "buy"))
        assert r.horizons[5].status == "pending"


def test_적재가_밀린_것은_stale로_구분한다():
    """pending과 섞이면 '아직 안 왔다'와 '안 받았다'를 구분 못 한다."""
    today = date(2026, 3, 1)       # 데이터 끝에서 한참 뒤 -> 전체가 밀렸다
    with tempfile.TemporaryDirectory() as tmp:
        store, dates, scorer = _setup(tmp, [100, 101, 102], [100, 100, 100], today=today)
        r = scorer.score(Sig("005930", "KR", datetime(2026, 1, 5, 16, 0, tzinfo=KST), "buy"))
        assert r.horizons[5].status == "stale"


def test_종목만_지수보다_뒤처지면_연휴와_무관하게_stale이다():
    """달력만 보면 연휴 뭉치에서 오탐한다. 지수가 그 시장의 거래일을 알고 있다."""
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(db_path=Path(tmp) / "s.sqlite3")
        dates = _days(10)
        _fill(store, "005930", dates[:3], [100, 101, 102])        # 3일치만
        _fill(store, KR_INDEX, dates, [100] * 10, market="INDEX")  # 지수는 최신
        scorer = Scorer(store, horizons=(5,), today=dates[-1])
        r = scorer.score(Sig("005930", "KR", datetime(2026, 1, 5, 16, 0, tzinfo=KST), "buy"))
        assert r.horizons[5].status == "stale"


def test_기준봉이_없으면_no_base다():
    with tempfile.TemporaryDirectory() as tmp:
        store, dates, scorer = _setup(tmp, [100] * 6, [100] * 6)
        r = scorer.score(Sig("000660", "KR", datetime(2026, 1, 5, 16, 0, tzinfo=KST), "buy"))
        assert r.horizons[5].status == "no_base"


def test_종목과_지수의_날짜가_어긋나면_계산하지_않는다():
    """하루치 어긋난 초과수익을 조용히 만들어내지 않는다."""
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(db_path=Path(tmp) / "s.sqlite3")
        dates = _days(8)
        _fill(store, "005930", dates, [100] * 8)
        # 기준일은 같고 중간 하루(dates[3])만 비었다 -> 5거래일 뒤가 서로 달라진다
        idx_dates = [d for i, d in enumerate(dates) if i != 3]
        _fill(store, KR_INDEX, idx_dates, [100] * len(idx_dates), market="INDEX")
        scorer = Scorer(store, horizons=(5,), today=dates[-1])
        r = scorer.score(Sig("005930", "KR", datetime(2026, 1, 5, 16, 0, tzinfo=KST), "buy"))
        assert r.horizons[5].status == "misaligned"


# -------------------------------------------------------------- 집계

def test_집계는_채점된_것만_분모에_넣는다():
    with tempfile.TemporaryDirectory() as tmp:
        store, dates, scorer = _setup(tmp, [100] * 5 + [110], [100] * 5 + [100])
        as_of = datetime(2026, 1, 5, 16, 0, tzinfo=KST)
        scored = scorer.score_many([
            Sig("005930", "KR", as_of, "buy", 0.9),
            Sig("000660", "KR", as_of, "buy", 0.8),     # 데이터 없음 -> no_base
        ])
        s = summarize(scored, 5)
        assert s["signals"] == 2 and s["scored"] == 1
        assert s["by_status"] == {"scored": 1, "no_base": 1}
        assert s["hit_rate"] == 1.0                     # 분모는 1


def test_집계에_레짐_분해가_들어간다():
    with tempfile.TemporaryDirectory() as tmp:
        store, dates, scorer = _setup(tmp, [100] * 5 + [110], [100] * 5 + [100])
        r = scorer.score(Sig("005930", "KR", datetime(2026, 1, 5, 16, 0, tzinfo=KST), "buy"))
        s = summarize([r], 5)
        assert "by_regime" in s and "confidence_correlation" in s
