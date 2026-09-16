"""벤치마크 채점과 저울 검정 — 기획서 9.4절.

랜덤 시그널을 실제 가격에 맞대어 채점한다. 두 가지를 본다.

1. **저울 검정.** 랜덤이면 적중률 0.5, 확신도 상관 0이어야 한다. 벗어나면
   시스템 성적을 해석하기 전에 채점기부터 의심한다.
2. **지수 buy&hold** 기준선.

    python scripts/score_benchmarks.py
    python scripts/score_benchmarks.py --seed 12345 --from 2024-01-01
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.eval.benchmarks import (calibration_report, index_buy_and_hold,
                                 random_signals)
from src.eval.scoring import HORIZONS, Scorer, summarize
from src.data.prices import PriceStore

DEFAULT_SEED = 20260916
# 마지막 시그널이 20거래일을 채울 수 있게 여유를 둔다. 안 그러면 pending이 쌓인다.
TAIL_BUFFER_DAYS = 45


def main() -> int:
    ap = argparse.ArgumentParser(description="벤치마크 채점")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--seeds", type=int, default=5,
                    help="검정에 쓸 시드 개수. 기획서 6절이 3회 이상을 요구한다")
    ap.add_argument("--from", dest="start", default=None, help="YYYY-MM-DD")
    ap.add_argument("--to", dest="end", default=None, help="YYYY-MM-DD")
    ap.add_argument("--market", choices=("KR", "US"), action="append")
    args = ap.parse_args()

    store = PriceStore()
    markets = args.market or ["KR", "US"]

    for market in markets:
        cov = store.coverage("005930" if market == "KR" else "AAPL")
        if not cov["first"]:
            print(f"{market}: 가격이 없습니다. scripts/collect_prices.py를 먼저 돌리세요.")
            return 1
        start = date.fromisoformat(args.start or cov["first"])
        end = (date.fromisoformat(args.end) if args.end
               else date.fromisoformat(cov["last"]) - timedelta(days=TAIL_BUFFER_DAYS))

        bh = index_buy_and_hold(store, market, start, end)
        print(f"════ {market} ════")
        if bh.get("total_return") is not None:
            print(f"  지수 buy&hold  {bh['index']}  {bh['from']} ~ {bh['to']}  "
                  f"총 {bh['total_return']*100:+.1f}%  CAGR {bh['cagr']*100:+.1f}%")

        scorer = Scorer(store)
        runs = []
        for i in range(max(1, args.seeds)):
            seed = args.seed + i
            scored = scorer.score_many(random_signals(market, start, end, seed))
            runs.append((seed, {n: summarize(scored, n) for n in HORIZONS}))
        seed0, summaries = runs[0]

        print(f"  동일가중 랜덤  시그널 {len(random_signals(market, start, end, seed0)):,}건 "
              f"× 시드 {len(runs)}개  (대표 seed={seed0})")
        for n, s in sorted(summaries.items()):
            c = s["confidence_correlation"]
            print(f"    {n:>2}거래일  채점 {s['scored']:,}/{s['signals']:,}  "
                  f"적중률 {s['hit_rate']:.3f}  "
                  f"평균초과 {s['mean_excess']*100:+.3f}%  "
                  f"비용후 {s['mean_excess_after_cost']*100:+.3f}%")
            print(f"              확신도상관 spearman {c['spearman']:+.3f} "
                  f"pearson {c['pearson']:+.3f} (n={c['n']:,})  "
                  f"hold {s['hold_ratio']:.2f}")
            if s["by_status"].keys() - {"scored"}:
                print(f"              미채점 "
                      f"{ {k: v for k, v in s['by_status'].items() if k != 'scored'} }")
            if s["by_regime"]:
                print("              레짐 " + "  ".join(
                    f"{k} {v['n']}건 적중 {v['hit_rate']:.2f}"
                    for k, v in s["by_regime"].items()))

        rep = calibration_report(runs)
        mark = "통과" if rep["passed"] else "실패 — 채점기를 의심할 것"
        print(f"  저울 검정 · 시드 {len(rep['seeds'])}개 평균 "
              f"(랜덤은 적중률 0.5 · 상관 0, 허용 ±{rep['tolerance']}): {mark}")
        for c in rep["checks"]:
            lo, hi = c["hit_rate_range"]
            clo, chi = c["spearman_range"]
            print(f"    {c['trading_days']:>2}일  적중률 {c['hit_rate_mean']:.3f} "
                  f"±{c['hit_rate_sd']:.3f} [{lo:.3f}~{hi:.3f}] "
                  f"{'OK' if c['hit_rate_ok'] else 'NG'}")
            print(f"          상관   {c['spearman_mean']:+.3f} "
                  f"±{c['spearman_sd']:.3f} [{clo:+.3f}~{chi:+.3f}] "
                  f"{'OK' if c['confidence_ok'] else 'NG'}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
