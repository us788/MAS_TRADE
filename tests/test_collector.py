"""수집 스케줄 검증. 네트워크를 타지 않는다.

여기서 지키는 것: 주기가 된 종목만 돌되, **경계에서 한 판을 통째로 건너뛰지 않는 것**.
건너뛴 구간의 뉴스는 나중에 살 수 없다.
"""
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.data.collector import (
    DUE_SLACK,
    MAX_LOOKBACK_DAYS,
    MIN_LOOKBACK_HOURS,
    Plan,
    build_plan,
    collect_one,
    compute_lookback_days,
)
from src.data.news import NewsBatch, NewsItem
from src.data.storage import Store
from src.data.universe import Holding, Universe

UTC = timezone.utc
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def _store(tmp):
    return Store(db_path=Path(tmp) / "t.sqlite3", raw_dir=Path(tmp) / "raw")


def _universe():
    fast = Holding("005930", "삼성전자", "반도체", "KR", "mktcap", "삼성전자",
                   ("삼성전자",), (), 1)
    slow = Holding("105560", "KB금융", "금융", "KR", "mktcap", "KB금융지주",
                   ("KB금융",), (), 24)
    us = Holding("AAPL", "Apple", "Technology", "US", "mktcap", "AAPL", (), (), 24)
    return Universe(version="t", holdings={"KR": (fast, slow), "US": (us,)})


def _plans(store, now=NOW, **kw):
    return {p.holding.symbol: p for p in build_plan(store, _universe(), now, **kw)}


# ---- 스케줄 ----

def test_첫_수집은_무조건_대상():
    with tempfile.TemporaryDirectory() as tmp:
        plans = _plans(_store(tmp))
        assert all(p.due for p in plans.values())
        assert plans["005930"].reason == "첫 수집"


def test_주기_전에는_대상이_아니다():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.record_run("105560", "naver", ran_at=NOW - timedelta(hours=3),
                         oldest_seen=None, reached_floor=True, pages=1,
                         fetched=0, inserted=0, duplicates=0)
        assert _plans(store)["105560"].due is False   # 24h 주기에 3h 경과


def test_주기가_지나면_대상():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.record_run("005930", "naver", ran_at=NOW - timedelta(hours=2),
                         oldest_seen=None, reached_floor=True, pages=1,
                         fetched=0, inserted=0, duplicates=0)
        assert _plans(store)["005930"].due is True    # 1h 주기에 2h 경과


def test_cron_지터로_한_판을_건너뛰지_않는다():
    # 1시간 주기인데 cron이 몇 분 일찍/늦게 깨우면, 여유가 없을 경우 다음 시간까지
    # 밀려 한 판이 통째로 빈다. DUE_SLACK이 그것을 막는다.
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.record_run("005930", "naver",
                         ran_at=NOW - timedelta(hours=1) + timedelta(minutes=2),
                         oldest_seen=None, reached_floor=True, pages=1,
                         fetched=0, inserted=0, duplicates=0)
        assert DUE_SLACK >= timedelta(minutes=2)
        assert _plans(store)["005930"].due is True


def test_force는_주기를_무시한다():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.record_run("105560", "naver", ran_at=NOW, oldest_seen=None,
                         reached_floor=True, pages=1, fetched=0, inserted=0, duplicates=0)
        assert _plans(store, force=True)["105560"].due is True


def test_시장과_종목을_한정할_수_있다():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        assert set(_plans(store, markets=("US",))) == {"AAPL"}
        assert set(_plans(store, symbols=("005930",))) == {"005930"}


# ---- lookback ----
# 주기 기준 고정값은 두 방향으로 틀린다. 평소엔 과하게 받아 페이지 상한에 가까워지고
# (실측: 1시간 주기에 6시간을 받아 10페이지 중 8 소진), 장기 중단 뒤엔 못 메운다.

def test_첫_수집은_주기의_두_배를_받는다():
    assert compute_lookback_days(None, 24, NOW) == 2.0
    assert compute_lookback_days(None, 1, NOW) * 24 == 2.0


def test_평소에는_경과시간의_1_5배만_받는다():
    last = NOW - timedelta(hours=1)
    assert compute_lookback_days(last, 1, NOW) * 24 == 1.5


def test_하한_아래로_내려가지_않는다():
    # cron이 거의 바로 다시 깨워도 최소 구간은 받는다.
    last = NOW - timedelta(minutes=5)
    assert compute_lookback_days(last, 1, NOW) * 24 == MIN_LOOKBACK_HOURS


def test_장기_중단_뒤에는_자동으로_넓어진다():
    # 고정 하한이었다면 5시간 구멍을 못 메운다.
    assert compute_lookback_days(NOW - timedelta(hours=5), 1, NOW) * 24 == 7.5


def test_상한을_넘지_않는다():
    # 무한정 거슬러 올라가도 벤더가 주지 않는다.
    assert compute_lookback_days(NOW - timedelta(days=90), 24, NOW) == MAX_LOOKBACK_DAYS


def test_계획에_lookback이_실려_나온다():
    with tempfile.TemporaryDirectory() as tmp:
        assert _plans(_store(tmp))["005930"].lookback_days > 0


# ---- 실패 처리 ----

class _Boom:
    def collect(self, *a, **k):
        raise RuntimeError("HTTP 429")


class _Pages:
    """여러 페이지를 돌려주는 어댑터."""

    def collect(self, symbol, as_of, lookback_days, **k):
        return NewsBatch(items=[], pages=3, oldest_seen=as_of, reached_floor=True,
                         raw=[{"page": 1}, {"page": 2}, {"page": 3}])


class _Partial:
    """페이지 상한에 걸려 구간을 다 못 받은 어댑터."""

    def collect(self, symbol, as_of, lookback_days, **k):
        return NewsBatch(items=[], pages=10, oldest_seen=as_of - timedelta(hours=2),
                         reached_floor=False, raw=[])


def test_수집_실패는_예외가_아니라_gap으로_남는다():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        plan = Plan(_universe().get("KR", "005930"), "naver", 1, None, True, "")
        batch, problem = collect_one(store, _Boom(), plan, NOW)
        assert batch is None and "HTTP 429" in problem
        gaps = store.open_gaps()
        assert len(gaps) == 1 and gaps[0]["symbol"] == "005930"


def test_한_종목이_실패해도_이력은_남지_않는다():
    # 실패한 수집을 성공으로 기록하면 다음 주기까지 재시도하지 않는다.
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        plan = Plan(_universe().get("KR", "005930"), "naver", 1, None, True, "")
        collect_one(store, _Boom(), plan, NOW)
        assert store.last_run_at("005930", "naver") is None


def test_커버리지_미달도_gap으로_남는다():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        plan = Plan(_universe().get("KR", "005930"), "naver", 1, None, True, "")
        batch, problem = collect_one(store, _Partial(), plan, NOW)
        assert batch is not None and "커버리지 미달" in problem
        assert len(store.open_gaps()) == 1
        # 다만 수집 자체는 일어났으므로 이력은 남는다
        assert store.last_run_at("005930", "naver") == NOW


def test_페이지가_여러개여도_원본이_덮어써지지_않는다():
    import json
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        plan = Plan(_universe().get("KR", "005930"), "naver", 1, None, True, "", 0.1)
        collect_one(store, _Pages(), plan, NOW)
        files = list((Path(tmp) / "raw").rglob("*.json"))
        assert len(files) == 1
        assert json.loads(files[0].read_text(encoding="utf-8"))["page_count"] == 3
