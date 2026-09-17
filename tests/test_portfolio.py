"""가상 포트폴리오 검증. 네트워크를 타지 않는다.

지킬 핵심 둘.

- **체결가는 판단 시점 이후여야 한다.** 기준가(전날 종가)로 체결하면 실행 불가능한
  가격으로 성적을 내는 것이다.
- **동일 금액 가중이 실제로 동일해야 한다.** 반올림이 비중을 흔들면 "종목 선택만
  평가한다"는 전제가 깨진다 (기획서 9.2절).
"""
import math
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.data.prices import Bar, PriceStore
from src.eval.portfolio import VirtualPortfolio
from src.eval.scoring import COSTS

KST = ZoneInfo("Asia/Seoul")


@dataclass
class Sig:
    symbol: str
    direction: str


def _days(n, start=date(2026, 1, 5)):
    out, cur = [], start
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur)
        cur += timedelta(days=1)
    return out


def _store(tmp, prices, market="KR"):
    """prices: {symbol: [가격...]} — 거래일 순서대로."""
    s = PriceStore(db_path=Path(tmp) / "p.sqlite3")
    n = max(len(v) for v in prices.values())
    days = _days(n)
    for symbol, px in prices.items():
        s.upsert([Bar(symbol, market, d, close_px=p, close_tr=p, source="t")
                  for d, p in zip(days, px)])
    s.upsert([Bar("KS200", "INDEX", d, close_px=100.0, close_tr=100.0, source="t")
              for d in days])
    return s, days


def _as_of(d):
    return datetime(d.year, d.month, d.day, 7, 0, tzinfo=KST)


# ------------------------------------------------------- 체결가

def test_체결은_판단_당일_종가로_한다():
    """시그널은 07:00에 나오지만 기준가는 전날 종가다. 그 가격으로는 살 수 없다."""
    with tempfile.TemporaryDirectory() as tmp:
        s, days = _store(tmp, {"A": [100, 200, 400]})
        pf = VirtualPortfolio(s, market="KR", initial_capital=1000.0)
        res = pf.run([(_as_of(days[1]), [Sig("A", "buy")])])
        r = res.rebalances[0]
        assert r.executed_on == days[1]
        assert r.trades[0].price == 200      # 전날 100이 아니다


def test_휴장이면_다음_거래일에_체결된다():
    with tempfile.TemporaryDirectory() as tmp:
        s, days = _store(tmp, {"A": [100, 200, 300]})
        holiday = days[0] - timedelta(days=2)      # 주말 — 봉이 없다
        assert holiday.weekday() >= 5
        pf = VirtualPortfolio(s, market="KR", initial_capital=1000.0)
        res = pf.run([(_as_of(holiday), [Sig("A", "buy")])])
        r = res.rebalances[0]
        assert r.executed_on == days[0]            # 다음 거래일로 밀린다
        assert r.trades[0].price == 100


# --------------------------------------------------- 동일 금액 가중

def test_동일_금액으로_나눈다():
    """주식 수가 아니라 금액이 같아야 종목 선택만 평가된다."""
    with tempfile.TemporaryDirectory() as tmp:
        s, days = _store(tmp, {"A": [100, 100], "B": [400, 400], "C": [25, 25]})
        pf = VirtualPortfolio(s, market="KR", initial_capital=3000.0,
                              cost_model=COSTS["US"].__class__(0, 0, 0, "t"))
        res = pf.run([(_as_of(days[0]),
                       [Sig(x, "buy") for x in ("A", "B", "C")])])
        h = res.rebalances[0].holdings
        values = {k: v * p for (k, v), p in zip(sorted(h.items()), (100, 400, 25))}
        assert all(math.isclose(v, 1000.0, rel_tol=1e-9) for v in values.values())


def test_소수점_주식을_허용한다():
    """정수로 끊으면 반올림이 비중을 왜곡한다."""
    with tempfile.TemporaryDirectory() as tmp:
        s, days = _store(tmp, {"A": [333, 333]})
        pf = VirtualPortfolio(s, market="KR", initial_capital=1000.0,
                              cost_model=COSTS["US"].__class__(0, 0, 0, "t"))
        res = pf.run([(_as_of(days[0]), [Sig("A", "buy")])])
        assert not float(res.rebalances[0].holdings["A"]).is_integer()


# -------------------------------------------------------- 규칙

def test_exclude_sell은_매도만_뺀다():
    with tempfile.TemporaryDirectory() as tmp:
        s, days = _store(tmp, {"A": [100]*2, "B": [100]*2, "C": [100]*2})
        pf = VirtualPortfolio(s, market="KR", rule="exclude_sell", initial_capital=900.0)
        res = pf.run([(_as_of(days[0]),
                       [Sig("A", "buy"), Sig("B", "hold"), Sig("C", "sell")])])
        assert set(res.rebalances[0].holdings) == {"A", "B"}


def test_buy_only는_매수만_담는다():
    with tempfile.TemporaryDirectory() as tmp:
        s, days = _store(tmp, {"A": [100]*2, "B": [100]*2, "C": [100]*2})
        pf = VirtualPortfolio(s, market="KR", rule="buy_only", initial_capital=900.0)
        res = pf.run([(_as_of(days[0]),
                       [Sig("A", "buy"), Sig("B", "hold"), Sig("C", "sell")])])
        assert set(res.rebalances[0].holdings) == {"A"}


# -------------------------------------------------------- 비용

def test_거래세는_매도에만_붙는다():
    """KR 거래세 0.20%는 매도 시에만이다 (기획서 6절)."""
    with tempfile.TemporaryDirectory() as tmp:
        s, days = _store(tmp, {"A": [100]*3, "B": [100]*3})
        pf = VirtualPortfolio(s, market="KR", rule="buy_only", initial_capital=1000.0)
        res = pf.run([(_as_of(days[0]), [Sig("A", "buy"), Sig("B", "sell")]),
                      (_as_of(days[1]), [Sig("A", "sell"), Sig("B", "buy")])])
        buy_leg = [t for t in res.rebalances[0].trades if t.shares > 0][0]
        sell_leg = [t for t in res.rebalances[1].trades if t.shares < 0][0]
        c = COSTS["KR"]
        assert math.isclose(buy_leg.cost / buy_leg.value,
                            (c.commission_bps + c.slippage_bps) / 10_000, rel_tol=1e-9)
        assert math.isclose(sell_leg.cost / sell_leg.value,
                            (c.commission_bps + c.slippage_bps + c.tax_bps) / 10_000,
                            rel_tol=1e-9)


def test_비용은_nav에서_빠진다():
    with tempfile.TemporaryDirectory() as tmp:
        s, days = _store(tmp, {"A": [100]*2})
        pf = VirtualPortfolio(s, market="KR", initial_capital=1000.0)
        r = pf.run([(_as_of(days[0]), [Sig("A", "buy")])]).rebalances[0]
        assert r.cost > 0
        assert math.isclose(r.nav_after, r.nav_before - r.cost, rel_tol=1e-9)


def test_가격이_없는_종목은_건너뛰고_기록한다():
    """조용히 빠뜨리면 비중이 틀어진 채로 성적이 나온다."""
    with tempfile.TemporaryDirectory() as tmp:
        s, days = _store(tmp, {"A": [100]*2})
        pf = VirtualPortfolio(s, market="KR", initial_capital=1000.0)
        res = pf.run([(_as_of(days[0]), [Sig("A", "buy"), Sig("없는종목", "buy")])])
        assert res.rebalances[0].skipped == ["없는종목"]
        assert set(res.rebalances[0].holdings) == {"A"}


def test_수익률은_가격을_따라간다():
    with tempfile.TemporaryDirectory() as tmp:
        s, days = _store(tmp, {"A": [100, 100, 150]})
        pf = VirtualPortfolio(s, market="KR", initial_capital=1000.0,
                              cost_model=COSTS["US"].__class__(0, 0, 0, "t"))
        res = pf.run([(_as_of(days[0]), [Sig("A", "buy")]),
                      (_as_of(days[2]), [Sig("A", "buy")])])
        assert math.isclose(res.total_return, 0.5, rel_tol=1e-9)
