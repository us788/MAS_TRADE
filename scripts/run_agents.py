"""멀티 에이전트 회차 실행 — 기획서 8절.

    python scripts/run_agents.py --dry-run             무엇을 보낼지만 (호출 없음)
    python scripts/run_agents.py --market KR --limit 2 작게 한 번
    python scripts/run_agents.py --as-of 2026-11-18T07:00+09:00
    python scripts/run_agents.py --status              이 트랙 누적

**베이스라인과 같은 `as_of`로 돌려야 비교가 성립한다**(기획서 12절). 기준가·유니버스·
시점이 같고 **구조만 다른** 두 트랙을 만드는 것이 목적이다. `--as-of`를 비우면
베이스라인과 같은 규칙(수요일 07:00 KST)으로 맞춘다.

호출 수가 베이스라인의 3~4배다 — KR 15×4=60, US 15×3=45. 돈이 나가므로
`--dry-run`으로 먼저 본다.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agents.analyst_prompts import analyst_system_prompt
from src.agents.analysts import analysts_for
from src.agents.context import ContextBuilder
from src.agents.orchestrator import AGENTS_KIND, run_market_agents
from src.agents.store import SignalStore
from src.data.dart import DartStore, fundamentals_as_of
from src.data.macro import MacroStore, features_as_of
from src.data.universe import load_universe

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_baseline import weekly_as_of                                    # noqa: E402


def market_payloads(market: str, as_of: datetime,
                    symbols: list[str]) -> tuple[dict, dict]:
    """에이전트에 넘길 외부 데이터. **as-of 판정은 여기서 끝난다.**

    오케스트레이터는 `MacroStore`도 `DartStore`도 모른다 — 데이터 접근은 반드시
    as-of 인터페이스를 통한다(CLAUDE.md 3절).
    """
    macro = features_as_of(MacroStore(), as_of)
    if market != "KR":
        # **US는 재무가 없다.** EDGAR가 IP 차단으로 막혀 있다.
        return macro, {}
    store = DartStore()
    fund = {}
    for symbol in symbols:
        f = fundamentals_as_of(store, symbol, as_of)
        if f is not None:
            fund[symbol] = f
    return macro, fund


def show_status(store: SignalStore, kind: str) -> int:
    rows = store.all_signals(kind=kind)
    if not rows:
        print(f"'{kind}' 트랙에 아직 시그널이 없습니다.")
        return 0
    by_dir: dict[str, int] = {}
    cost = 0.0
    rejected = 0
    for r in rows:
        by_dir[r["direction"]] = by_dir.get(r["direction"], 0) + 1
        cost += r["estimated_cost_usd"] or 0.0
        rejected += 0 if r["risk_approved"] else 1
    print(f"'{kind}' 시그널 {len(rows)}건 · 추정 비용 ${cost:.4f}")
    print("  방향 " + "  ".join(f"{k} {v}" for k, v in sorted(by_dir.items())))
    print(f"  리스크 기각 {rejected}건")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="멀티 에이전트 시그널 생성")
    ap.add_argument("--market", choices=("KR", "US"), action="append")
    ap.add_argument("--as-of", dest="as_of", default=None,
                    help="ISO8601. 비우면 베이스라인과 같은 수요일 07:00 KST")
    ap.add_argument("--limit", type=int, default=None, help="종목 수 제한 (검증용)")
    ap.add_argument("--rule", default="majority",
                    choices=("majority", "confidence_weighted", "unanimous"))
    ap.add_argument("--kind", default=AGENTS_KIND,
                    help="트랙 이름. 구성을 바꾸면 올린다 — 전후를 섞으면 안 된다")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    store = SignalStore()
    if args.status:
        return show_status(store, args.kind)

    as_of = (datetime.fromisoformat(args.as_of) if args.as_of else weekly_as_of())
    if as_of.tzinfo is None:
        print("--as-of에 시간대를 붙이세요 (예: 2026-11-18T07:00+09:00)")
        return 2

    markets = args.market or ["US", "KR"]
    builder = ContextBuilder()
    universe = load_universe()
    total_ok = total = 0

    for market in markets:
        contexts = builder.build_market(market, as_of)
        if args.limit:
            contexts = contexts[:args.limit]
        if not contexts:
            print(f"{market}: 기준봉이 없습니다. as_of를 확인하세요.")
            continue

        classes = analysts_for(market)
        macro, fund = market_payloads(market, as_of, [c.symbol for c in contexts])

        if args.dry_run:
            print(f"════ {market} · {as_of.isoformat()} · {len(contexts)}종목 "
                  f"× 에이전트 {len(classes)} = {len(contexts) * len(classes)}회 ════")
            for name, cls in classes.items():
                prefix = analyst_system_prompt(
                    name, market if cls.market_specific else None)
                print(f"  {name:<12s} 프리픽스 {len(prefix):>6,}자")
            for c in contexts:
                sizes = []
                for name, cls in classes.items():
                    sl = cls().slice(c, macro, fundamentals=fund.get(c.symbol))
                    sizes.append(f"{name[:4]} {len(json.dumps(sl, ensure_ascii=False, default=float)):>6,}")
                print(f"    {c.symbol:<8s} {c.name:<18s} " + "  ".join(sizes))
            continue

        print(f"════ {market} · {as_of.isoformat()} · {len(contexts)}종목 "
              f"× {len(classes)} 에이전트 ════")
        out = run_market_agents(
            contexts, market=market, as_of=as_of, store=store, macro=macro,
            fundamentals=fund, rule=args.rule, kind=args.kind,
            universe_version=universe.version)

        for o in out["outcomes"]:
            if o.agg is None:
                print(f"  FAIL {o.context.symbol:<8s} {o.context.name:<18s} 의견 0건")
                continue
            votes = " ".join(f"{k[:4]}:{v}" for k, v in sorted(o.agg.votes.items()))
            gate = "" if o.verdict.approved else f"  ⟂기각 {'; '.join(o.verdict.reasons)}"
            print(f"  OK   {o.context.symbol:<8s} {o.context.name:<18s} "
                  f"{o.agg.direction.value:<5s} 확신도 {o.agg.confidence:.2f} "
                  f"분산 {o.agg.dispersion:.2f}  [{votes}]{gate}")
        if out["failures"]:
            print(f"  빠진 에이전트: {'; '.join(out['failures'])[:300]}")
        print(f"  {out['succeeded']}/{out['attempted']} 종목  run_id={out['run_id']}")
        total_ok += out["succeeded"]
        total += out["attempted"]

    if args.dry_run:
        return 0
    print(f"합계 {total_ok}/{total}")
    return 0 if total_ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
