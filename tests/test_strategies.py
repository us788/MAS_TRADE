"""룰 기반 베이스라인 검증. 네트워크를 타지 않는다.

지킬 핵심 둘.

- **as_of 이후 데이터가 점수에 새지 않는 것.** 룰 베이스라인의 존재 이유가
  "룩어헤드가 없다는 걸 우리가 보장한다"인데 그게 깨지면 비교군 자체가 무의미해진다.
- **없는 신호를 만들어내지 않는 것.** 전 종목이 동점이면 방향을 가를 수 없다.
"""
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.data.prices import Bar, PriceStore
from src.eval.strategies import (CONF_MAX, CONF_MIN, BuyAndHold, Momentum,
                                 Strategy, StrategyRunner)

KST = ZoneInfo("Asia/Seoul")
AS_OF = datetime(2026, 3, 4, 7, 0, tzinfo=KST)


class Fixed(Strategy):
    """주어진 점수를 그대로 돌려주는 시험용 전략."""
    name = "fixed"
    min_bars = 1

    def __init__(self, scores):
        self.scores = scores

    def score(self, bars, index_bars):
        return self.scores.get(bars[0].symbol)


def _sig(strategy, scores, market="KR"):
    return strategy.to_signals(list(scores.items()), market, AS_OF)


# ---------------------------------------------------------- 횡단면 순위

def test_상위는_매수_하위는_매도_가운데는_hold다():
    s = _sig(Fixed({}), {f"S{i}": float(i) for i in range(9)})
    by_dir = {}
    for x in s:
        by_dir.setdefault(x.direction, []).append(x.symbol)
    assert by_dir["buy"] == ["S6", "S7", "S8"]
    assert by_dir["sell"] == ["S0", "S1", "S2"]
    assert len(by_dir["hold"]) == 3


def test_확신도는_극단에서_높고_가운데서_낮다():
    s = {x.symbol: x.confidence for x in _sig(Fixed({}), {f"S{i}": float(i) for i in range(9)})}
    assert s["S8"] == CONF_MAX and s["S0"] == CONF_MAX      # 양 끝이 가장 높다
    assert s["S4"] == CONF_MIN                               # 한가운데가 가장 낮다
    assert s["S8"] > s["S7"] > s["S6"]


def test_확신도_폭이_llm보다_넓다():
    """LLM 첫 실행은 0.42~0.62였다. 룰은 정의가 명확해 폭이 넓다."""
    confs = [x.confidence for x in _sig(Fixed({}), {f"S{i}": float(i) for i in range(15)})]
    assert max(confs) - min(confs) > 0.4


def test_전_종목이_동점이면_전부_hold다():
    """없는 신호를 만들어내지 않는다."""
    s = _sig(Fixed({}), {f"S{i}": 1.0 for i in range(9)})
    assert {x.direction for x in s} == {"hold"}


def test_동점은_평균_순위로_묶인다():
    s = {x.symbol: x.direction for x in
         _sig(Fixed({}), {"A": 1.0, "B": 1.0, "C": 1.0, "D": 5.0, "E": 9.0, "F": 9.0})}
    assert s["E"] == s["F"]          # 같은 점수는 같은 판정
    assert s["A"] == s["B"] == s["C"]


def test_종목이_너무_적으면_시그널을_내지_않는다():
    assert _sig(Fixed({}), {"A": 1.0, "B": 2.0}) == []


def test_buy_and_hold는_전부_매수다():
    s = _sig(BuyAndHold(), {f"S{i}": 0.0 for i in range(9)})
    assert {x.direction for x in s} == {"buy"}
    assert {x.confidence for x in s} == {CONF_MIN}


# ------------------------------------------------------------ 룩어헤드

def _store(tmp):
    """3개월치 봉. 기준일 이후에 급등을 심어 둔다."""
    store = PriceStore(db_path=Path(tmp) / "s.sqlite3")
    days, cur = [], date(2025, 10, 1)
    while len(days) < 120:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    cut = date(2026, 3, 3)          # AS_OF(3/4 07:00 KST)가 알 수 있는 마지막 거래일
    for symbol, market in (("005930", "KR"), ("KS200", "INDEX")):
        bars = []
        for i, d in enumerate(days):
            # 기준일 이후 구간만 10배로 띄운다. 이게 점수에 섞이면 룩어헤드다.
            px = 100.0 + i * 0.1
            if d > cut:
                px *= 10
            bars.append(Bar(symbol, market, d, close_px=px, close_tr=px, source="t"))
        store.upsert(bars)
    return store


def test_기준일_이후_봉은_점수에_들어가지_않는다():
    """룰 베이스라인의 존재 이유가 이것이다."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        st = Momentum(20)
        bars = store.series("005930", "KR", AS_OF, lookback_days=400)
        assert bars, "봉이 있어야 한다"
        assert max(b.date for b in bars) <= date(2026, 3, 3)   # 미래가 안 섞였다
        score = st.score(bars, store.series("KS200", "KR", AS_OF, lookback_days=400))
        # 10배 급등이 섞였다면 점수가 터무니없이 커진다
        assert score is not None and abs(score) < 1.0


def test_min_bars가_모자라면_그_종목은_빠진다():
    """짧은 구간으로 대신 계산하지 않는다 — 다른 종목과 기준이 달라진다."""
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(db_path=Path(tmp) / "s.sqlite3")
        store.upsert([Bar("005930", "KR", date(2026, 3, 2), close_px=100,
                          close_tr=100, source="t")])
        runner = StrategyRunner(store)
        assert runner.generate(Momentum(60), "KR", AS_OF) == []
