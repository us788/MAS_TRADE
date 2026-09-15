"""종목별 뉴스 유입 속도를 재고 수집 주기를 배정한다.

네이버는 최신순 1,000건(start 상한)까지만 준다. 그래서 한 번의 수집이 덮는 시간은
**종목의 언급 빈도에 반비례**한다. 실측(2026-09-15)으로 27배 차이가 났다.

    삼성전자          1,000건 =   7.0시간
    한화에어로스페이스 1,000건 = 192.5시간

하루 1회로 통일하면 삼성전자는 매일 17시간이 빈다. 그 구멍은 나중에 메울 수 없다.

    python scripts/measure_news_rate.py          # 측정만
    python scripts/measure_news_rate.py --apply  # 반영 (주기 단축만)
    python scripts/measure_news_rate.py --apply --allow-longer  # 늘리는 것도 허용
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data.naver_news import MAX_START, NaverNews, parse_item
from src.data.universe import UNIVERSE_PATH, load_universe

# 한 번의 수집이 커버해야 할 시간 = 주기. 여기에 안전 여유를 둔다.
#
# 여유를 1/4(250건)로 잡는 이유: 일 한도 25,000회 중 실제 사용은 1,000회 미만이라
# 한도가 전혀 병목이 아니다. 반면 놓친 구간은 영구 손실이다. 비용이 거의 0인 쪽으로
# 치우치는 것이 맞다. 측정도 장중(뉴스가 몰리는 시간)에 하므로 야간 속도는 더 낮고,
# 실적 시즌에는 몇 배로 뛴다 — 평균치에 딱 맞춘 주기는 스파이크에서 반드시 뚫린다.
SAFETY_BUDGET = MAX_START // 4
TIERS = (1, 3, 6, 12, 24)


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

        # 제목 매칭률이 낮은 원인은 둘 중 하나다.
        #   (a) 별칭 부족 — 제목이 다른 표기를 쓴다
        #   (b) 시황·업계 기사에 본문으로만 딸려 나온다 (정상)
        # 가르려면 **매칭되지 않은** 제목을 봐야 한다. 실측상 대부분 (b)였다.
        if title_pct < 15:
            missed = [i for i in items if not any(n and n in i.title for n in names)]
            print(f"{'':<20} └ 제목 매칭 낮음. 매칭 안 된 제목 (별칭 부족 vs 시황 기사 판단용):")
            for sample in missed[:3]:
                print(f"{'':<22} {sample.title[:56]}")

        # 벤더 집계가 비현실적으로 낮으면 쿼리가 너무 좁은 것이다 (다단어 AND).
        if span_h > 72:
            print(f"{'':<20} └ 경고: 100건이 {span_h/24:.0f}일을 덮는다. "
                  f"쿼리 {holding.news_query!r}가 너무 좁을 수 있다.")

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
    relax = "--allow-longer" in sys.argv

    # 측정은 흔들린다 — 같은 종목이 한 시간 사이 25% 차이 났다 (2026-09-15).
    # 한산한 시간에 재서 주기가 늘어나면 그만큼 구멍이 생기고 되돌릴 수 없다.
    # 그래서 기본은 **줄이기만** 한다. 늘리려면 --allow-longer 를 명시해야 한다.
    changed, kept = [], []
    for row in payload["KR"]:
        proposed = tiers.get(row["symbol"])
        if proposed is None:
            continue
        current = row.get("collect_every_hours")
        if current is None or proposed < current or relax:
            if current != proposed:
                changed.append(f"{row['name']} {current}h -> {proposed}h")
            row["collect_every_hours"] = proposed
        else:
            kept.append(f"{row['name']} {current}h 유지 (이번 측정 {proposed}h)")

    for line in changed:
        print(f"  변경 {line}")
    for line in kept:
        print(f"  유지 {line}")
    if kept and not relax:
        print("  (주기를 늘리려면 --allow-longer. 늘리면 그만큼 구멍이 생길 수 있다)")
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
