"""층 2 가상 포트폴리오 — 기획서 9.2절.

    python scripts/run_portfolio.py --strategy momentum_60d   룰 시그널로
    python scripts/run_portfolio.py --signals                 저장된 LLM 시그널로
    python scripts/run_portfolio.py --rule buy_only

**보조 지표다.** 주 평가는 층 1(시그널 채점)이다. 3개월간 주 1회면 판단 시점이
12번뿐이라 이 숫자만으로는 어떤 결론도 낼 수 없다(기획서 9.3절).

**두 시장을 합산하지 않는다.** 현지통화로 끝까지 따로 본다.
"""
import argparse
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agents.store import SignalStore
from src.data.prices import PriceStore
from src.eval.benchmarks import SIGNAL_HOUR, SIGNAL_TZ, signal_dates
from src.eval.portfolio import VirtualPortfolio
from src.eval.strategies import ALL_STRATEGIES, StrategyRunner

WARMUP_DAYS = 260


def rule_signals(runner, strategy, market, dates):
    out = []
    for d in dates:
        as_of = datetime(d.year, d.month, d.day, SIGNAL_HOUR, 0, tzinfo=SIGNAL_TZ)
        sigs = runner.generate(strategy, market, as_of)
        if sigs:
            out.append((as_of, sigs))
    return out


def stored_signals(store, market):
    grouped = defaultdict(list)
    for r in store.all_signals(market):
        grouped[datetime.fromisoformat(r["as_of"])].append(
            type("S", (), {"symbol": r["symbol"], "direction": r["direction"]})())
    return sorted(grouped.items())


def show(label, res, bench=None):
    s = res.summary()
    if not res.nav:
        print(f"  {label:18s} 실행 없음"); return
    line = (f"  {label:18s} 리밸런싱 {s['rebalances']:>3}회  "
            f"총수익 {s['total_return']*100:>7.1f}%")
    if s["cagr"] is not None:
        line += f"  CAGR {s['cagr']*100:>6.1f}%"
    if s["max_drawdown"] is not None:
        line += f"  MDD {s['max_drawdown']*100:>6.1f}%"
    if s["sharpe"] is not None:
        line += f"  샤프 {s['sharpe']:>5.2f}"
    print(line)
    print(f"  {'':18s} 평균 보유 {s['mean_holdings']:.1f}종목  "
          f"평균 회전 {s['mean_turnover']*100:.1f}%  "
          f"누적 비용 {s['total_cost']/res.initial_capital*100:.2f}%")
    if bench and bench.total_return is not None:
        diff = (res.total_return - bench.total_return) * 100
        print(f"  {'':18s} 지수 대비 {diff:+.1f}%p "
              f"(지수 {bench.total_return*100:+.1f}%)")


def main() -> int:
    ap = argparse.ArgumentParser(description="가상 포트폴리오")
    ap.add_argument("--market", choices=("KR", "US"), action="append")
    ap.add_argument("--rule", choices=("exclude_sell", "buy_only"), default=None,
                    help="기본은 둘 다 돌려 비교한다")
    ap.add_argument("--strategy", default=None, help="룰 전략 이름 (기본: 전부)")
    ap.add_argument("--signals", action="store_true", help="저장된 LLM 시그널로 돌린다")
    args = ap.parse_args()

    store = PriceStore()
    runner = StrategyRunner(store)
    markets = args.market or ["KR", "US"]
    rules = [args.rule] if args.rule else ["exclude_sell", "buy_only"]

    for market in markets:
        cov = store.coverage("005930" if market == "KR" else "AAPL")
        start = date.fromisoformat(cov["first"]) + timedelta(days=WARMUP_DAYS)
        end = date.fromisoformat(cov["last"])
        dates = signal_dates(start, end)
        print(f"════ {market} · {start} ~ {end} · 주 1회 {len(dates)}회 ════")

        if args.signals:
            sig_store = SignalStore()
            series = stored_signals(sig_store, market)
            if len(series) < 2:
                print(f"  저장된 시그널이 {len(series)}회차뿐이라 포트폴리오를 돌릴 수 없다.")
                print("  (주 1회 × 최소 몇 주는 쌓여야 한다)\n")
                continue
            for rule in rules:
                pf = VirtualPortfolio(store, market=market, rule=rule)
                show(f"LLM/{rule}", pf.run(series),
                     pf.index_buy_and_hold([d for d, _ in series]))
            print()
            continue

        bench = VirtualPortfolio(store, market=market).index_buy_and_hold(dates)
        picked = [s for s in ALL_STRATEGIES
                  if args.strategy is None or s.name == args.strategy]
        for strategy in picked:
            if strategy.name == "buy_and_hold":
                continue            # 지수 벤치마크와 역할이 겹친다
            series = rule_signals(runner, strategy, market, dates)
            for rule in rules:
                pf = VirtualPortfolio(store, market=market, rule=rule)
                show(f"{strategy.name}/{rule}", pf.run(series), bench)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
