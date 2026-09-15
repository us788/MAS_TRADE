"""종목별 뉴스 유입 속도를 재고 수집 주기를 배정한다.

네이버는 최신순 1,000건(start 상한)까지만 준다. 그래서 한 번의 수집이 덮는 시간은
**종목의 언급 빈도에 반비례**한다. 실측(2026-09-15)으로 27배 차이가 났다.

    삼성전자          1,000건 =   7.0시간
    한화에어로스페이스 1,000건 = 192.5시간

하루 1회로 통일하면 삼성전자는 매일 17시간이 빈다. 그 구멍은 나중에 메울 수 없다.

    python scripts/measure_news_rate.py          # 측정만
    python scripts/measure_news_rate.py --apply  # universe.json에 반영
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data.naver_news import MAX_START, NaverNews, parse_item
from src.data.universe import UNIVERSE_PATH, load_universe

# 한 번의 수집이 커버해야 할 시간 = 주기. 여기에 안전 여유를 둔다.
# 1,000건 한도의 절반(500건)만으로 주기를 덮도록 잡아, 뉴스가 갑자기 몰려도 버틴다.
SAFETY_BUDGET = MAX_START // 2
TIERS = (3, 6, 12, 24)


def assign_tier(rate_per_hour: float) -> int:
    """유입 속도 → 수집 주기 등급(시간)."""
    if rate_per_hour <= 0:
        return TIERS[-1]
    capacity_hours = SAFETY_BUDGET / rate_per_hour
    for tier in TIERS:
        if capacity_hours >= tier:
            candidate = tier
        else:
            break
    return candidate if capacity_hours >= TIERS[0] else TIERS[0]


def main() -> int:
    apply = "--apply" in sys.argv
    universe = load_universe()
    client = NaverNews()
    now = datetime.now(timezone.utc)
    rows = []

    print(f"{'종목':<20} {'표본':>5} {'구간(h)':>8} {'건/h':>8} {'제목%':>6} {'주기':>5}")
    print("-" * 60)

    for holding in universe.market("KR"):
        try:
            payload = client.fetch_raw(holding.news_query, display=100, start=1)
        except Exception as exc:                      # noqa: BLE001
            print(f"{holding.name:<20} 실패: {type(exc).__name__}")
            continue

        items = [i for i in (parse_item(r, holding.symbol, now)
                             for r in payload.get("items", [])) if i]
        if len(items) < 2:
            print(f"{holding.name:<20} 표본 부족({len(items)}건) -> 24시간")
            rows.append((holding, 24, 0.0, 0.0))
            continue

        stamps = sorted(i.published_at for i in items)
        span_h = (stamps[-1] - stamps[0]).total_seconds() / 3600
        rate = len(items) / span_h if span_h > 0 else float(len(items))

        names = [holding.name, *holding.news_require]
        in_title = sum(1 for i in items if any(n and n in i.title for n in names))
        title_pct = in_title / len(items) * 100

        tier = assign_tier(rate)
        rows.append((holding, tier, rate, title_pct))
        print(f"{holding.name:<20} {len(items):>5} {span_h:>8.1f} {rate:>8.1f} "
              f"{title_pct:>5.0f}% {tier:>4}h")

    print()
    by_tier = {}
    for holding, tier, _, _ in rows:
        by_tier.setdefault(tier, []).append(holding.name)
    for tier in sorted(by_tier):
        print(f"  {tier:>2}시간 주기: {len(by_tier[tier])}종목 — {', '.join(by_tier[tier])}")
    daily_calls = sum(24 // t * 10 for _, t, _, _ in rows)   # 종목당 최대 10페이지
    print(f"\n  하루 최대 호출 수(KR): 약 {daily_calls:,}회 / 한도 25,000회")

    if not apply:
        print("\n--apply 를 붙이면 universe.json의 collect_every_hours에 반영합니다.")
        return 0

    payload = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
    tiers = {h.symbol: t for h, t, _, _ in rows}
    for row in payload["KR"]:
        if row["symbol"] in tiers:
            row["collect_every_hours"] = tiers[row["symbol"]]
    payload.setdefault("changelog", []).append({
        "date": now.strftime("%Y-%m-%d"),
        "change": "뉴스 유입 속도 실측 후 종목별 collect_every_hours 배정",
    })
    UNIVERSE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nuniverse.json 갱신 완료 ({len(tiers)}종목)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
