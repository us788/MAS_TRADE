"""KR 재무 수집 — DART. 기획서 8절 시점 정합성 규칙.

**접수일자 기준으로만 쓴다.** 회계기간 종료일이 아니라 공시된 시점부터 시장이
아는 정보다. 재무 응답의 `rcept_no` 앞 8자리가 접수일자라 공시검색을 따로
조인하지 않는다 (`src/data/dart.py` 모듈 docstring).

    python scripts/collect_financials.py                 최근 2년치
    python scripts/collect_financials.py --years 4
    python scripts/collect_financials.py --symbol 005930
    python scripts/collect_financials.py --status
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data.dart import (REPORT_CODES, DartClient, DartFetchError, DartStore)
from src.data.universe import load_universe

#: 한 종목이 1년에 내는 보고서. 분기·반기·사업.
PERIODS = ("11013", "11012", "11014", "11011")


def show_status(store: DartStore) -> int:
    c = store.counts()
    print(f"공시 {c['total']:,}건 · 종목 {c['symbols']}개 "
          f"· 정정이 있는 기간 {c['amended_periods']}건")
    now = datetime.now(timezone.utc)
    for h in load_universe().market("KR"):
        row = store.as_of(h.symbol, now)
        if not row:
            print(f"  {h.name:<16} 없음")
            continue
        rev = row["revenue"]
        print(f"  {h.name:<16} {row['bsns_year']} "
              f"{REPORT_CODES.get(row['reprt_code'], row['reprt_code']):<10} "
              f"접수 {row['filed_on']}  "
              + (f"매출 {rev/1e12:>8,.1f}조" if rev else "매출 없음(금융업)"))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="KR 재무 수집 (DART)")
    ap.add_argument("--symbol", action="append")
    ap.add_argument("--years", type=int, default=2)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    store = DartStore()
    if args.status:
        return show_status(store)

    symbols = args.symbol or [h.symbol for h in load_universe().market("KR")]
    this_year = datetime.now(timezone.utc).year
    years = list(range(this_year - args.years + 1, this_year + 1))
    print(f"종목 {len(symbols)}개 × 연도 {years} × 보고서 {len(PERIODS)}종 "
          f"= 최대 {len(symbols)*len(years)*len(PERIODS)}회 (일 한도 20,000)")
    if args.dry_run:
        print("--dry-run: 호출하지 않았습니다.")
        return 0

    client = DartClient()
    saved = missing = failed = 0
    for sym in symbols:
        got = []
        for year in years:
            for code in PERIODS:
                try:
                    f = client.financials(sym, year, code)
                except DartFetchError as e:
                    # 재무는 뉴스와 달리 **나중에 다시 받을 수 있다.** 놓쳐도
                    # 영구 손실이 아니다 — 실패를 세고 넘어간다.
                    print(f"  FAIL {sym} {year}/{code}: {e}")
                    failed += 1
                    continue
                if f is None:
                    missing += 1
                    continue
                got.append(f)
        n = store.save(got)
        saved += n
        if got:
            latest = max(got, key=lambda x: x.filed_on)
            print(f"  OK   {sym}  신규 {n:>2} / 받음 {len(got):>2}  "
                  f"최신 {latest.bsns_year} {latest.report_name} 접수 {latest.filed_on}")

    print(f"\n신규 {saved}건 · 미공시 {missing}건 · 실패 {failed}건")
    c = store.counts()
    print(f"누적 {c['total']:,}건 · 종목 {c['symbols']}개")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
