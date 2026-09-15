"""저장된 기사의 관련성 등급을 현재 규칙으로 다시 매긴다.

관련성 규칙은 앞으로도 바뀐다 — 별칭 보강, 매체 화이트리스트, 판정 기준 변경.
**과거 구간은 다시 받을 수 없으므로**, 이미 받은 것을 다시 해석할 수 있어야 한다.
제목·요약이 DB에 있으니 재수집 없이 UPDATE로 끝난다.

    python scripts/reclassify.py            # 무엇이 바뀔지만 보여준다
    python scripts/reclassify.py --apply    # 실제로 반영
"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data.news import NewsItem, Relevance, classify_relevance, is_excluded
from src.data.storage import Store
from src.data.universe import load_universe

from datetime import datetime, timezone

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _as_item(row: dict) -> NewsItem:
    """판정에 필요한 건 제목·요약뿐이다. 나머지는 자리만 채운다."""
    return NewsItem(
        source="", market=row["market"], symbol=row["symbol"],
        title=row["title"] or "", summary=row["summary"] or "",
        url="", publisher="", published_at=_EPOCH, collected_at=_EPOCH,
    )


def main() -> int:
    apply = "--apply" in sys.argv
    store = Store()
    universe = load_universe()
    rows = store.iter_rows()
    print(f"대상 {len(rows):,}건\n")

    updates: list[tuple[str, str, str]] = []
    moved: Counter = Counter()
    per_symbol: dict[str, Counter] = {}
    now_excluded = 0
    unknown: set[str] = set()

    for row in rows:
        try:
            holding = universe.get(row["market"], row["symbol"])
        except KeyError:
            unknown.add(f"{row['market']}/{row['symbol']}")
            continue

        item = _as_item(row)
        if is_excluded(item, list(holding.news_exclude)):
            # 이미 저장된 것은 지우지 않는다. 수집 데이터를 삭제하는 쪽이 더 나쁘다.
            now_excluded += 1

        new = classify_relevance(item, holding.names).value
        old = row["relevance"]
        per_symbol.setdefault(row["symbol"], Counter())[new] += 1
        if new != old:
            moved[f"{old} -> {new}"] += 1
            updates.append((new, row["symbol"], row["content_hash"]))

    if unknown:
        print(f"유니버스에 없는 종목 {len(unknown)}개는 건너뜀: {sorted(unknown)[:5]}\n")

    if not updates:
        print("바뀌는 것이 없습니다.")
        return 0

    print(f"변경 {len(updates):,}건")
    for change, n in moved.most_common():
        print(f"  {change:<22} {n:>6,}")

    print("\n종목별 새 등급 (1차 자료 = title)")
    for holding in [*universe.market("US"), *universe.market("KR")]:
        counts = per_symbol.get(holding.symbol)
        if not counts:
            continue
        total = sum(counts.values())
        title = counts.get(Relevance.TITLE.value, 0)
        print(f"  {holding.market} {holding.name:<18} "
              f"title {title:>4}/{total:<5} ({title/total*100:>4.0f}%)")

    if now_excluded:
        print(f"\n현재 배제 규칙이었다면 들어오지 않았을 기사 {now_excluded}건 "
              "(이미 저장된 것은 지우지 않는다)")

    if not apply:
        print("\n--apply 를 붙이면 반영합니다.")
        return 0

    changed = store.update_relevance(updates)
    print(f"\n{changed:,}건 반영 완료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
