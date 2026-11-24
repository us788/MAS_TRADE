"""매크로 수집 — ALFRED vintage.

**vintage 없이 받는 경로를 두지 않는다.** 그냥 받으면 최종 수정치가 오고, 그건
그 시점에 세상에 없던 숫자다 (`src/data/macro.py` 모듈 docstring 참고).

    python scripts/collect_macro.py              오늘 vintage로 수집
    python scripts/collect_macro.py --as-of '2026-11-18T07:00:00+09:00'
    python scripts/collect_macro.py --status     저장 현황
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data.macro import (AlfredClient, MacroFetchError, MacroStore, SERIES,
                            vintage_date)


def show_status(store: MacroStore) -> int:
    c = store.counts()
    print(f"관측 {c['total']:,}건 · 계열 {len(c['by_series'])}개 · vintage {c['vintages']}개")
    now = datetime.now(timezone.utc)
    snap = store.snapshot_as_of(now)
    for s, label in SERIES.items():
        p = snap.get(s)
        n = c["by_series"].get(s, 0)
        if p:
            print(f"  {s:<14} {label:<18} {n:>5}건  최신 {p.date} {p.value}"
                  f"  (vintage {p.vintage})")
        else:
            print(f"  {s:<14} {label:<18} {n:>5}건  최신 없음")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="매크로 수집 (ALFRED vintage)")
    ap.add_argument("--as-of", default=None, help="YYYY-MM-DDTHH:MM:SS+09:00")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    store = MacroStore()
    if args.status:
        return show_status(store)

    as_of = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        print("--as-of는 타임존이 있어야 합니다.", file=sys.stderr)
        return 2
    vint = vintage_date(as_of)
    print(f"as_of {as_of.isoformat()}  →  vintage {vint}")
    if args.dry_run:
        print(f"  계열 {len(SERIES)}개: {', '.join(SERIES)}")
        print("--dry-run: 호출하지 않았습니다.")
        return 0

    client = AlfredClient()
    saved = failed = 0
    for s, label in SERIES.items():
        try:
            points = client.observations(s, as_of)
        except MacroFetchError as e:
            # 매크로는 뉴스와 달리 **나중에 다시 받을 수 있다.** 그 vintage를
            # 놓치면 그 시점 값은 영영 못 받지만, 다음 회차가 최신을 덮는다.
            print(f"  FAIL {s:<14} {e}")
            failed += 1
            continue
        n = store.save(points)
        saved += n
        print(f"  OK   {s:<14} {label:<18} 신규 {n:>4} / 받음 {len(points):>4}")

    print(f"\n{len(SERIES)-failed}/{len(SERIES)} 성공 · 신규 {saved:,}건")
    c = store.counts()
    print(f"누적 {c['total']:,}건 · vintage {c['vintages']}개")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
