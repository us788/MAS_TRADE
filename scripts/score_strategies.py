"""룰 기반 비LLM 베이스라인 백테스트 — 기획서 2절·9.4절의 비교군.

LLM이 20줄짜리 룰을 이기는가. 기획서 12절의 "멀티에이전트가 단일 호출을 이기는가"
앞에 놓인 질문이다.

    python scripts/score_strategies.py                    3년 전체
    python scripts/score_strategies.py --from 2026-04-24  LLM 비교 구간만
    python scripts/score_strategies.py --market KR

**구간이 둘로 나뉘는 이유**: 기획서 4.4절은 백테스트를 2026-04-24(DeepSeek V4 프리뷰
공개일) 이후로 제한한다. LLM이 과거를 이미 알기 때문이다. 룰에는 그 문제가 없으므로
3년 전체를 쓸 수 있지만, **LLM과 맞대결할 때는 같은 구간으로 잘라야 공정하다.**
"""
import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data.prices import PriceStore
from src.eval.benchmarks import SIGNAL_HOUR, SIGNAL_TZ, signal_dates
from src.eval.scoring import HORIZONS, Scorer, summarize
from src.eval.strategies import ALL_STRATEGIES, StrategyRunner

# 지표 계산에 필요한 과거 구간. 이만큼 지난 뒤부터 시그널을 낸다.
WARMUP_DAYS = 260
# 마지막 시그널이 20거래일을 채울 수 있게 남겨두는 구간.
TAIL_DAYS = 45


def main() -> int:
    ap = argparse.ArgumentParser(description="룰 기반 베이스라인 백테스트")
    ap.add_argument("--market", choices=("KR", "US"), action="append")
    ap.add_argument("--from", dest="start", default=None, help="YYYY-MM-DD")
    ap.add_argument("--to", dest="end", default=None, help="YYYY-MM-DD")
    args = ap.parse_args()

    store = PriceStore()
    runner = StrategyRunner(store)
    scorer = Scorer(store)
    markets = args.market or ["KR", "US"]

    for market in markets:
        probe = "005930" if market == "KR" else "AAPL"
        cov = store.coverage(probe)
        if not cov["first"]:
            print(f"{market}: 가격이 없습니다.")
            return 1
        start = date.fromisoformat(args.start) if args.start else \
            date.fromisoformat(cov["first"]) + timedelta(days=WARMUP_DAYS)
        end = date.fromisoformat(args.end) if args.end else \
            date.fromisoformat(cov["last"]) - timedelta(days=TAIL_DAYS)
        dates = signal_dates(start, end)

        print(f"════ {market} · {start} ~ {end} · 주 1회 {len(dates)}회 ════")
        header = f"  {'전략':16s} {'호라이즌':>6s} {'채점':>6s} {'적중률':>7s} " \
                 f"{'평균초과':>9s} {'비용후':>9s} {'확신도상관':>10s}"
        print(header)

        for strategy in ALL_STRATEGIES:
            signals = []
            for d in dates:
                as_of = datetime(d.year, d.month, d.day, SIGNAL_HOUR, 0,
                                 tzinfo=SIGNAL_TZ)
                signals.extend(runner.generate(strategy, market, as_of))
            if not signals:
                print(f"  {strategy.name:16s} 시그널 없음 (구간이 짧습니다)")
                continue
            scored = scorer.score_many(signals)
            for n in HORIZONS:
                sm = summarize(scored, n)
                if not sm["scored"]:
                    print(f"  {strategy.name:16s} {n:>4}일  채점 0  "
                          f"미채점 {sm['by_status']}")
                    continue
                c = sm["confidence_correlation"]
                corr = f"{c['spearman']:+.3f}" if c["spearman"] is not None else "   -  "
                print(f"  {strategy.name:16s} {n:>4}일 {sm['scored']:>6,} "
                      f"{sm['hit_rate']:>7.3f} {sm['mean_excess']*100:>8.2f}% "
                      f"{sm['mean_excess_after_cost']*100:>8.2f}% {corr:>10s}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
