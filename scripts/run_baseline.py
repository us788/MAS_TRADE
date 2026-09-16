"""DeepSeek 단일 호출 베이스라인 실행 — 기획서 4.5절.

    python scripts/run_baseline.py --dry-run           무엇을 보낼지만 (호출 없음)
    python scripts/run_baseline.py --market KR
    python scripts/run_baseline.py --as-of 2026-09-15T16:00+09:00 --limit 3
    python scripts/run_baseline.py --status            누적 시그널·비용

**실행 시각은 KST 07:00~08:00이 기본이다** (docs/harness.md 2절). 그 시각이라야
US 전 거래일 종가와 KR 전 거래일 종가가 모두 확정돼 두 시장의 기준가가 대칭이 된다.
DeepSeek 오프피크 구간이기도 해서 비용도 절반이다.
"""
import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agents.baseline import BaselineAgent, run_market
from src.agents.context import ContextBuilder
from src.agents.prompts import prompt_version, system_prompt
from src.agents.store import SignalStore
from src.data.universe import load_universe

KST = ZoneInfo("Asia/Seoul")
SIGNAL_HOUR = 7          # harness.md 2절
SIGNAL_WEEKDAY = 2       # 수요일. 기획서 9.1절 — 요일을 고정해야 구간 간 비교가 된다


def default_as_of() -> datetime:
    """오늘 KST 07:00. 이미 지났으면 오늘, 아니면 어제."""
    now = datetime.now(KST)
    base = now.replace(hour=SIGNAL_HOUR, minute=0, second=0, microsecond=0)
    return base if now >= base else base - timedelta(days=1)


def weekly_as_of(now: datetime | None = None) -> datetime:
    """직전(또는 오늘)의 고정 요일 KST 07:00으로 **스냅**한다.

    launchd는 잠든 사이 밀린 실행을 깨어날 때 돌린다. 그때 `default_as_of()`를 쓰면
    실행된 날 기준이 되어 **요일 고정이 깨진다.** 기획서 9.1절이 요일·시각 고정을
    요구하는 이유는 구간 간 비교 가능성이므로, 늦게 실행되더라도 as_of는 원래 요일에
    맞춘다. 그 시점 데이터만 보게 되니 룩어헤드도 없다.

    실제 실행 시각은 `runs.started_at`에 따로 남는다 — 얼마나 늦었는지는 거기서 본다.
    """
    now = now or datetime.now(KST)
    base = now.replace(hour=SIGNAL_HOUR, minute=0, second=0, microsecond=0)
    if now < base:
        base -= timedelta(days=1)
    back = (base.weekday() - SIGNAL_WEEKDAY) % 7
    return base - timedelta(days=back)


def show_status(store: SignalStore) -> int:
    c = store.counts()
    print(f"실행 {c['runs']}회 · 시그널 {c['signals']}건 · 의견 {c['opinions']}건 · "
          f"LLM 호출 {c['llm_calls']}건")
    if c["by_direction"]:
        print("  방향 " + "  ".join(f"{k} {v}" for k, v in sorted(c["by_direction"].items())))
    hit = c["cached_tokens"] / c["prompt_tokens"] * 100 if c["prompt_tokens"] else 0
    print(f"  토큰 입력 {c['prompt_tokens']:,}(캐시 적중 {c['cached_tokens']:,} = {hit:.0f}%) "
          f"출력 {c['completion_tokens']:,}")
    print(f"  누적 추정 비용 ${c['estimated_cost_usd']:.4f}")
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT market, as_of, attempted, succeeded, prompt_version, notes"
            " FROM runs ORDER BY started_at DESC LIMIT 8").fetchall()
    for r in rows:
        note = f"  실패: {r['notes'][:60]}" if r["notes"] else ""
        print(f"  {r['market']} {r['as_of'][:16]}  {r['succeeded']}/{r['attempted']}"
              f"  프롬프트 {r['prompt_version']}{note}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="베이스라인 시그널 생성")
    ap.add_argument("--market", choices=("KR", "US"), action="append")
    ap.add_argument("--as-of", dest="as_of", default=None,
                    help="ISO 타임스탬프. 기본은 오늘 KST 07:00")
    ap.add_argument("--limit", type=int, default=None, help="종목 수 제한 (검증용)")
    ap.add_argument("--weekly", action="store_true",
                    help="as_of를 직전 고정 요일 07:00 KST로 스냅하고, 이미 돌린 "
                         "시점이면 건너뛴다. launchd 주간 작업이 쓴다")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    store = SignalStore()
    if args.status:
        return show_status(store)

    if args.weekly:
        as_of = weekly_as_of()
    elif args.as_of:
        as_of = datetime.fromisoformat(args.as_of)
    else:
        as_of = default_as_of()
    if as_of.tzinfo is None:
        print("--as-of는 타임존이 있어야 합니다 (예: 2026-09-15T16:00+09:00)", file=sys.stderr)
        return 2

    markets = args.market or ["KR", "US"]
    builder = ContextBuilder()
    universe = load_universe()
    total_ok = total = 0

    for market in markets:
        if args.weekly and store.has_successful_run("baseline", market, as_of):
            print(f"{market} {as_of.isoformat()}: 이미 돌렸습니다. 건너뜁니다.")
            continue
        contexts = builder.build_market(market, as_of)
        if args.limit:
            contexts = contexts[:args.limit]
        if not contexts:
            print(f"{market}: 기준봉이 없습니다. as_of를 확인하세요.")
            continue

        if args.dry_run:
            prefix = system_prompt(market)
            print(f"════ {market} · {as_of.isoformat()} ════")
            print(f"  고정 프리픽스 {len(prefix):,}자 (버전 {prompt_version(market)})")
            for c in contexts:
                payload = json.dumps(c.to_payload(), ensure_ascii=False, default=float)
                print(f"    {c.symbol:<8s} {c.name:<18s} 기준 {c.base_date} "
                      f"{c.base_price:>12,.2f}  뉴스 {len(c.news):>2}건  "
                      f"가변부 {len(payload):>6,}자")
            continue

        print(f"════ {market} · {as_of.isoformat()} · {len(contexts)}종목 ════")
        out = run_market(contexts, market=market, as_of=as_of, store=store,
                         agent=BaselineAgent(), universe_version=universe.version)
        for r in out["results"]:
            if r.ok:
                print(f"  OK   {r.context.symbol:<8s} {r.context.name:<18s} "
                      f"{r.response.direction.value:<5s} 확신도 {r.response.confidence:.2f}  "
                      f"${r.call.estimated_cost_usd:.4f}")
            else:
                print(f"  FAIL {r.context.symbol:<8s} {r.context.name:<18s} {r.error[:70]}")
        print(f"  {out['succeeded']}/{out['attempted']} 성공  run_id={out['run_id']}")
        total_ok += out["succeeded"]
        total += out["attempted"]

    if args.dry_run:
        return 0
    if total == 0:
        print("돌릴 것이 없습니다.")
        return 0
    print(f"\n합계 {total_ok}/{total}")
    show_status(store)
    return 0 if total_ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
