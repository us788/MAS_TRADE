"""네이버 뉴스 검색의 관련성·시간 범위 진단.

확인하려는 것 두 가지:
1. 검색 결과 중 실제로 그 종목 기사인 비율 (제목에 종목명이 있는가)
2. 최신순 N페이지가 실제로 몇 시간을 덮는가 (lookback이 의미가 있는가)

    python scripts/diagnose_naver.py [종목코드]
"""
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# scripts/ 아래에서 실행해도 저장소 루트의 src를 찾게 한다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data.naver_news import NaverNews, parse_item
from src.data.universe import load_universe

symbol = sys.argv[1] if len(sys.argv) > 1 else "005930"
holding = load_universe().get("KR", symbol)
client = NaverNews()
now = datetime.now(timezone.utc)

print(f"종목: {holding.name} ({symbol})  검색어: {holding.news_query!r}")
print(f"require={list(holding.news_require)}  exclude={list(holding.news_exclude)}\n")

rows, pages = [], 0
for start in range(1, 1001, 100):          # start 상한 1000
    payload = client.fetch_raw(holding.news_query, display=100, start=start)
    batch = payload.get("items", [])
    if not batch:
        break
    rows.extend(batch)
    pages += 1
    if start == 1:
        print(f"total(벤더 집계) = {payload.get('total'):,}")

items = [i for i in (parse_item(r, symbol, now) for r in rows) if i]
print(f"{pages}페이지 / 원본 {len(rows)}건 / 파싱 {len(items)}건\n")

if not items:
    raise SystemExit("결과 없음")

# ---- 1. 시간 범위 ----
stamps = sorted(i.published_at for i in items)
span_h = (stamps[-1] - stamps[0]).total_seconds() / 3600
print("=== 시간 범위 ===")
print(f"  최신 {stamps[-1]}  ~  최古 {stamps[0]}")
print(f"  덮는 구간: {span_h:.1f}시간  ({span_h/24:.2f}일)")
print(f"  -> lookback 7일을 이 방식으로 채울 수 있는가: {'예' if span_h >= 168 else '아니오'}\n")

# ---- 2. 관련성 ----
names = [holding.name] + list(holding.news_require)
in_title = sum(1 for i in items if any(n and n in i.title for n in names))
in_summary_only = sum(
    1 for i in items
    if not any(n and n in i.title for n in names)
    and any(n and n in i.summary for n in names)
)
neither = len(items) - in_title - in_summary_only
print("=== 관련성 ===")
print(f"  제목에 종목명 있음      {in_title:4d}건  ({in_title/len(items):.0%})")
print(f"  요약에만 있음           {in_summary_only:4d}건  ({in_summary_only/len(items):.0%})")
print(f"  둘 다 없음              {neither:4d}건  ({neither/len(items):.0%})")
print("  -> 현재 require 필터는 '요약에만' 까지 통과시킨다. 이게 잡음의 정체다.\n")

# ---- 3. 매체 분포 ----
print("=== 매체 상위 15 ===")
for pub, n in Counter(i.publisher for i in items).most_common(15):
    flag = "  <- 제목매칭 " + str(sum(
        1 for i in items if i.publisher == pub and any(n2 and n2 in i.title for n2 in names)
    ))
    print(f"  {n:4d}  {pub or '(없음)'}{flag}")

print("\n=== 제목에 종목명 없는 기사 표본 5건 ===")
for i in [x for x in items if not any(n and n in x.title for n in names)][:5]:
    print(f"  [{i.publisher}] {i.title[:60]}")
