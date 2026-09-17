"""가격 수집 — US(yfinance) + KR(FinanceDataReader) + 벤치마크 지수.

뉴스와 달리 **과거를 나중에 다시 받을 수 있다.** 그래서 놓친 날이 영구 손실이 아니고,
정기 실행이 뉴스만큼 급하지 않다. 대신 채점 직전에 최신 상태인지가 중요하다.

    python scripts/collect_prices.py --status         저장 현황과 열린 gap
    python scripts/collect_prices.py --dry-run        무엇을 받을지만
    python scripts/collect_prices.py                  부족한 구간만 이어받기
    python scripts/collect_prices.py --years 3        3년치 초기 적재
    python scripts/collect_prices.py --market KR
    python scripts/collect_prices.py --symbol 005930 --years 1

설계 근거는 docs/harness.md.
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data.price_sources import BENCHMARKS, PriceFetchError, fetch, fetch_benchmark
from src.data.prices import PriceStore
from src.data.universe import load_universe

DEFAULT_YEARS = 3
# 마지막 봉 다음날부터 받되 며칠 겹쳐 받는다. 벤더가 뒤늦게 채우는 경우가 있고,
# 겹친 구간은 upsert가 unchanged로 흡수한다.
OVERLAP_DAYS = 5


def targets(markets, symbols):
    """(symbol, market, label). 지수는 market='INDEX'로 저장되지만 조회는 시장별이다."""
    universe = load_universe()
    out = []
    for market in markets:
        for h in universe.market(market):
            if symbols and h.symbol not in symbols:
                continue
            out.append((h.symbol, market, market, h.name))
        bench, label = BENCHMARKS[market]
        if not symbols or bench in symbols:
            out.append((bench, market, "INDEX", label))
    return out


def plan_range(store, symbol, years, today):
    """받을 구간. 이미 있으면 마지막 봉 근처부터 이어받는다."""
    floor = today - timedelta(days=int(365.25 * years))
    last = store.latest_date(symbol)
    if last is None:
        return floor, today
    start = last - timedelta(days=OVERLAP_DAYS)
    return (min(start, floor) if start > floor else start), today


def show_status(store):
    c = store.counts()
    print(f"봉 {c['total']:,}개 · 종목 {c['symbols']}개 · 수정 이력 {c['revisions']}건")
    for market, n in sorted(c["by_market"].items()):
        print(f"  {market:6s} {n:,}")
    print()
    universe = load_universe()
    for market in ("KR", "US"):
        for h in universe.market(market):
            cov = store.coverage(h.symbol)
            missing = len(store.missing_weekdays(h.symbol))
            flag = f"  결측평일 {missing}" if missing else ""
            print(f"  {market} {h.name:<18s} {cov['bars']:>5}봉  "
                  f"{cov['first']} ~ {cov['last']}{flag}")
        bench, label = BENCHMARKS[market]
        cov = store.coverage(bench)
        print(f"  {market} {label:<18s} {cov['bars']:>5}봉  "
              f"{cov['first']} ~ {cov['last']}  [지수]")
    gaps = store.open_gaps()
    print(f"\n열린 gap {len(gaps)}건")
    for g in gaps:
        print(f"  {g['symbol']} {g['from_date']}~{g['to_date']} — {g['reason']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="가격 수집")
    ap.add_argument("--market", choices=("KR", "US"), action="append")
    ap.add_argument("--symbol", action="append")
    ap.add_argument("--years", type=int, default=DEFAULT_YEARS)
    ap.add_argument("--refetch", action="store_true",
                    help="close_px 보류 규칙을 넘겨 벤더 값으로 덮는다. "
                         "미완결 봉이 저장돼 정정이 거부된 경우에 쓴다")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    store = PriceStore()
    if args.status:
        return show_status(store)

    markets = args.market or ["KR", "US"]
    today = date.today()
    rows = targets(markets, set(args.symbol or []))

    if args.dry_run:
        for symbol, market, kind, label in rows:
            start, end = plan_range(store, symbol, args.years, today)
            tag = "[지수]" if kind == "INDEX" else ""
            print(f"  {market} {label:<18s} {symbol:<8s} {start} ~ {end} {tag}")
        print("\n--dry-run: 호출하지 않았습니다.")
        return 0

    ok = failed = 0
    for symbol, market, kind, label in rows:
        start, end = plan_range(store, symbol, args.years, today)
        try:
            bars = (fetch_benchmark(market, start, end) if kind == "INDEX"
                    else fetch(symbol, market, start, end))
        except PriceFetchError as e:
            store.record_gap(symbol, start, end, str(e)[:200])
            print(f"  FAIL {market} {label:<18s} {e}")
            failed += 1
            continue
        except Exception as e:   # 벤더 라이브러리는 무엇을 던질지 모른다
            store.record_gap(symbol, start, end, f"{type(e).__name__}: {e}"[:200])
            print(f"  FAIL {market} {label:<18s} {type(e).__name__}: {e}")
            failed += 1
            continue

        r = store.upsert(bars, force=args.refetch)
        note = ""
        if r.revised:
            note += f" 수정 {r.revised}"
        if r.conflicted:
            note += f" !!충돌 {r.conflicted}"
        print(f"  OK   {market} {label:<18s} 신규 {r.inserted:>4} "
              f"유지 {r.unchanged:>4}{note}")
        ok += 1

    print(f"\n{ok}/{ok + failed} 성공")
    c = store.counts()
    print(f"누적 봉 {c['total']:,}개 · 종목 {c['symbols']}개")
    if c["revisions"]:
        print(f"수정 이력 {c['revisions']}건 — price_revisions 확인")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
