"""저장된 기사 텍스트를 현재 정규화 규칙으로 다시 씻는다.

정규화 규칙은 앞으로도 바뀐다. **과거 구간은 다시 받을 수 없으므로** 이미 받은
것을 새 규칙으로 다시 읽을 수 있어야 한다 — `reclassify.py`와 같은 논리다.

2026-09-16에 추가된 규칙 둘:

- **모지바케 복구** — Finnhub 응답에 `â\\x80\\x94`(EM DASH가 latin-1로 깨진 것)가 온다
- **보이지 않는 문자 제거** — 네이버 기사에 소프트 하이픈(U+00AD)이 섞인다.
  한국어는 부분 문자열 매칭이라 종목명 사이에 끼면 관련성 판정이 조용히 실패한다

    python scripts/clean_news_text.py           # 무엇이 바뀔지만 (기본)
    python scripts/clean_news_text.py --apply   # 실제로 반영

**반영한 뒤에는 `reclassify.py`를 돌린다.** 텍스트가 바뀌면 관련성 등급도 다시
매겨야 한다. 이 스크립트는 등급을 건드리지 않는다.
"""
import sys
import unicodedata
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data.news import renormalize
from src.data.storage import Store


def _invisible(text: str) -> Counter:
    out = Counter()
    for ch in text or "":
        cat = unicodedata.category(ch)
        if cat == "Cf" or (cat == "Cc" and ch not in "\t\n\r\f\v"):
            out[f"U+{ord(ch):04X} {unicodedata.name(ch, '?')}"] += 1
    return out


def main() -> int:
    apply = "--apply" in sys.argv
    store = Store()
    rows = store.iter_rows()

    updates, found = [], Counter()
    changed_by_market = Counter()
    samples = []
    for r in rows:
        # clean_text가 아니라 renormalize다 - 재적용하면 본문이 사라진다
        title, summary = renormalize(r["title"]), renormalize(r["summary"])
        if title == (r["title"] or "") and summary == (r["summary"] or ""):
            continue
        found.update(_invisible(r["title"]) + _invisible(r["summary"]))
        changed_by_market[(r["market"], r["relevance"])] += 1
        updates.append((r["symbol"], r["content_hash"], title, summary))
        if len(samples) < 6:
            src = r["title"] if title != (r["title"] or "") else r["summary"]
            dst = title if title != (r["title"] or "") else summary
            samples.append((r["market"], r["symbol"], src, dst))

    print(f"기사 {len(rows):,}건 중 {len(updates)}건이 바뀐다")
    if found:
        print("\n발견한 문자")
        for k, v in found.most_common():
            print(f"  {k:<42s} {v}회")
    if changed_by_market:
        print("\n시장 × 현재 등급")
        for (market, rel), n in sorted(changed_by_market.items()):
            print(f"  {market} {rel:<8s} {n}")
    if samples:
        print("\n예시")
        for market, symbol, src, dst in samples:
            print(f"  [{market}] {symbol}")
            print(f"    전: {src[:78]!r}")
            print(f"    후: {dst[:78]!r}")

    if not updates:
        print("\n바꿀 것이 없습니다.")
        return 0
    if not apply:
        print("\n--apply 를 붙이면 반영합니다. 반영 뒤 reclassify.py를 돌리세요.")
        return 0

    n = store.update_text(updates)
    print(f"\n{n}행 갱신했습니다.")
    print("이제 `python scripts/reclassify.py` 로 등급을 다시 매기세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
