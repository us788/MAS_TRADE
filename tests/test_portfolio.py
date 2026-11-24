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

from src.data.price_sources import BENCHMARKS
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
    s.upsert([Bar(BENCHMARKS["KR"][0], "INDEX", d,
                  close_px=100.0, close_tr=100.0, source="t")
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


# ---- 현금 보유 (2026-11-24) ----
#
# 앞의 두 규칙은 편입 종목이 몇 개든 NAV를 전액 쓴다. 15종목 중 11종목이 매도여도
# 남은 4종목에 100%를 몰아넣는다. 10-07 KR 회차가 실제로 11/15 매도였다.
# 모델이 "다 팔아라"라고 말해도 포트폴리오가 풀 포지션이던 것은 **정보가 부족해서가
# 아니라 그 말을 실행할 수단이 없어서**였다.


def _four_symbols(tmp, jump_from=6):
    """A·B·C는 100 고정, D만 `jump_from` 이후 2배."""
    s = PriceStore(db_path=Path(tmp) / "p.sqlite3")
    days = _days(20)
    for i, d in enumerate(days):
        bars = [Bar(x, "KR", d, close_px=100.0, close_tr=100.0, source="t")
                for x in ("A", "B", "C")]
        px = 100.0 if i < jump_from else 200.0
        bars.append(Bar("D", "KR", d, close_px=px, close_tr=px, source="t"))
        bars.append(Bar(BENCHMARKS["KR"][0], "INDEX", d,
                        close_px=100.0, close_tr=100.0, source="t"))
        s.upsert(bars)
    return s, days


def test_매도분이_현금으로_남는다():
    """`sell_to_cash`는 유니버스 1/N씩 준다. 4종목 중 3개가 매도면 25%만 투자된다."""
    with tempfile.TemporaryDirectory() as tmp:
        store, days = _four_symbols(tmp)
        sig = lambda w: [Sig("A", "sell"), Sig("B", "sell"),
                         Sig("C", "sell"), Sig("D", "buy")]
        series = [(_as_of(days[i]), sig(_as_of(days[i]))) for i in (0, 5, 10)]
        rb = VirtualPortfolio(store, market="KR", rule="sell_to_cash",
                              initial_capital=1_000_000).run(series).rebalances[0]
        invested = rb.holdings.get("D", 0.0) * 100.0
        assert abs(invested / rb.nav_after - 0.25) < 0.01       # 1/4만 투자
        assert set(rb.holdings) == {"D"}


def test_기존_규칙은_전액을_쓴다():
    """**병행 측정이라 기존 동작이 바뀌면 안 된다.**"""
    with tempfile.TemporaryDirectory() as tmp:
        store, days = _four_symbols(tmp)
        sig = lambda w: [Sig("A", "sell"), Sig("B", "sell"),
                         Sig("C", "sell"), Sig("D", "buy")]
        series = [(_as_of(days[i]), sig(_as_of(days[i]))) for i in (0, 5, 10)]
        for rule in ("exclude_sell", "buy_only"):
            rb = VirtualPortfolio(store, market="KR", rule=rule,
                                  initial_capital=1_000_000).run(series).rebalances[0]
            invested = rb.holdings.get("D", 0.0) * 100.0
            assert abs(invested / rb.nav_after - 1.0) < 0.01    # 전액


def test_전원_매도면_청산한다():
    """**전에는 리밸런싱을 건너뛰어 직전 보유를 그대로 들고 갔다.**
    전원 매도가 '현금화'가 아니라 '유지'로 번역된 것이다 — 어느 규칙의 정의로도 아니다.
    """
    with tempfile.TemporaryDirectory() as tmp:
        store, days = _four_symbols(tmp, jump_from=99)          # D도 100 고정
        buy = [Sig(x, "buy") for x in ("A", "B", "C", "D")]
        sell = [Sig(x, "sell") for x in ("A", "B", "C", "D")]
        series = [(_as_of(days[0]), buy), (_as_of(days[5]), sell),
                  (_as_of(days[10]), sell)]
        for rule in ("exclude_sell", "buy_only", "sell_to_cash"):
            res = VirtualPortfolio(store, market="KR", rule=rule,
                                   initial_capital=1_000_000).run(series)
            after_sell = [r for r in res.rebalances if r.as_of == _as_of(days[5])][0]
            assert after_sell.holdings == {}, f"{rule}: 청산되지 않았다"
