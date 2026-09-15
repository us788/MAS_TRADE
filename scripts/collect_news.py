"""뉴스 수집 — KR(네이버) + US(Finnhub).

cron은 한 시간마다 이 스크립트를 깨우기만 하면 된다. 어떤 종목을 돌지는
`collection_runs`의 마지막 수집 시각과 종목별 주기를 보고 여기서 고른다.

    python scripts/collect_news.py --dry-run     # 대상만 확인 (호출 없음)
    python scripts/collect_news.py               # 주기가 된 종목 수집
    python scripts/collect_news.py --market KR
    python scripts/collect_news.py --symbol 005930 --force
    python scripts/collect_news.py --status      # 저장 현황과 열린 gap

crontab 예시 (KST 기준, 매시 5분):
    5 * * * * cd ~/Desktop/2026\\ 개인프로젝트/MAS_TRADE && \\
              .venv/bin/python scripts/collect_news.py >> logs/collect.log 2>&1
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data.collector import RunSummary, build_plan, collect_one
from src.data.storage import Store
from src.data.universe import load_universe


def make_adapter(source: str):
    """어댑터는 실제로 쓸 때만 만든다 — 키가 없는 시장을 건너뛸 수 있게."""
    if source == "naver":
        from src.data.naver_news import NaverNews
        return NaverNews()
    if source == "finnhub":
        from src.data.finnhub_news import FinnhubNews
        return FinnhubNews()
    raise ValueError(f"알 수 없는 소스: {source}")


RELEVANCE_ORDER = ("title", "summary", "none")


def show_status(store: Store) -> int:
    counts = store.counts()
    primary = store.primary_count()
    print(f"기사 {counts['total']:,}건")
    for market, n in sorted(counts["by_market"].items()):
        print(f"  {market} {n:>6,}  그중 1차 자료 {primary.get(market, 0):>6,}")

    # 등급만 세면 오해를 부른다 — 벤더가 태깅한 기사는 제목에 회사명이 없어도
    # 1차 자료다. 두 축을 겹쳐 봐야 한다.
    matrix = store.relevance_matrix()
    if matrix:
        print("\n  등급 × 벤더태깅")
        print(f"    {'':<14}" + "".join(f"{r:>9}" for r in RELEVANCE_ORDER))
        keys = sorted({(m["market"], m["vendor_tagged"]) for m in matrix})
        for market, tagged in keys:
            label = f"{market} {'태깅됨' if tagged else '태깅없음'}"
            cells = []
            for rel in RELEVANCE_ORDER:
                n = sum(m["n"] for m in matrix
                        if m["market"] == market and m["vendor_tagged"] == tagged
                        and m["relevance"] == rel)
                cells.append(f"{n:>9,}" if n else f"{'-':>9}")
            print(f"    {label:<14}" + "".join(cells))
        print("    * 태깅됨은 등급과 무관하게 1차 자료로 쓴다 (벤더가 종목을 지정)")

    gaps = store.open_gaps()
    print(f"\n열린 gap {len(gaps)}건")
    for gap in gaps[:15]:
        print(f"  [{gap['at'][:16]}] {gap['market']}/{gap['symbol']} — {gap['reason'][:70]}")
    if len(gaps) > 15:
        print(f"  ... 외 {len(gaps) - 15}건")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="뉴스 수집")
    parser.add_argument("--market", action="append", choices=["KR", "US"],
                        help="시장 한정 (반복 지정 가능)")
    parser.add_argument("--symbol", action="append", help="종목 한정 (반복 지정 가능)")
    parser.add_argument("--force", action="store_true", help="주기를 무시하고 수집")
    parser.add_argument("--dry-run", action="store_true", help="대상만 출력, 호출 없음")
    parser.add_argument("--status", action="store_true", help="저장 현황과 gap 출력")
    args = parser.parse_args()

    store = Store()
    if args.status:
        return show_status(store)

    now = datetime.now(timezone.utc)
    plans = build_plan(
        store, load_universe(), now,
        markets=tuple(args.market) if args.market else ("KR", "US"),
        symbols=tuple(args.symbol) if args.symbol else (),
        force=args.force,
    )
    due = [p for p in plans if p.due]

    print(f"{now.isoformat(timespec='seconds')}  대상 {len(due)} / 전체 {len(plans)}")
    for plan in plans:
        mark = "->" if plan.due else "  "
        print(f" {mark} {plan.holding.market} {plan.holding.name:<18} "
              f"주기 {plan.interval_hours:>2}h  lookback {plan.lookback_days*24:>5.1f}h  "
              f"{plan.reason}")

    if args.dry_run:
        print("\n--dry-run: 호출하지 않았습니다.")
        return 0
    if not due:
        print("\n수집할 종목이 없습니다.")
        return 0

    summary = RunSummary()
    adapters: dict[str, object] = {}
    print()
    for plan in due:
        summary.attempted += 1
        try:
            if plan.source not in adapters:
                adapters[plan.source] = make_adapter(plan.source)
        except Exception as exc:                               # noqa: BLE001
            # 키가 없는 등 어댑터 자체를 못 만드는 경우. 그 소스만 건너뛴다.
            reason = f"어댑터 생성 실패: {type(exc).__name__}: {exc}"
            store.record_gap(plan.holding.market, plan.source, plan.holding.symbol, reason, now)
            summary.gaps.append(f"{plan.holding.name}: {reason}")
            print(f"  SKIP {plan.holding.name:<18} {reason[:60]}")
            continue

        batch, problem = collect_one(store, adapters[plan.source], plan, now)
        if batch is None:
            summary.gaps.append(f"{plan.holding.name}: {problem}")
            print(f"  FAIL {plan.holding.name:<18} {problem[:60]}")
            continue

        summary.succeeded += 1
        stored = store.news_for(plan.holding.symbol, now, plan.lookback_days)
        primary = len(batch.primary)
        note = f"  ⚠ {problem[:50]}" if problem else ""
        if problem:
            summary.gaps.append(f"{plan.holding.name}: {problem}")
        print(f"  OK   {plan.holding.name:<18} 수집 {len(batch.items):>4} "
              f"(1차 {primary:>3}) 페이지 {batch.pages}{note}")

    counts = store.counts()
    print(f"\n{summary.succeeded}/{summary.attempted} 성공 · 누적 기사 {counts['total']:,}건")
    if summary.gaps:
        print(f"gap {len(summary.gaps)}건 — --status 로 확인")
    return 0 if summary.succeeded == summary.attempted else 1


if __name__ == "__main__":
    raise SystemExit(main())
