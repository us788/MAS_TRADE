"""저장된 시그널을 실제 등락에 맞대어 채점한다 — 기획서 9.1절.

    python scripts/score_signals.py                  전체
    python scripts/score_signals.py --market KR
    python scripts/score_signals.py --run-id <id>    특정 실행만
    python scripts/score_signals.py --detail         종목별로 펼쳐서

**미채점(pending)은 실패가 아니다.** 5거래일이 아직 안 지난 시그널은 그렇게 나오는
것이 맞다. `stale`이 보이면 가격 적재가 밀린 것이니 collect_prices.py를 돌린다.
"""
import argparse
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agents.store import SignalStore
from src.eval.scoring import HORIZONS, Scorer, summarize


@dataclass(frozen=True)
class StoredSignal:
    """DB 행을 채점기가 읽는 형태로. 채점기는 이 다섯 필드만 본다."""
    symbol: str
    market: str
    as_of: datetime
    direction: str
    confidence: float
    signal_id: str = ""


def load(store: SignalStore, market: str | None, run_id: str | None
         ) -> list[StoredSignal]:
    sql = "SELECT * FROM signals"
    where, params = [], []
    if market:
        where.append("market = ?"); params.append(market)
    if run_id:
        where.append("run_id = ?"); params.append(run_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY as_of, symbol"
    with store.connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, params)]
    return [StoredSignal(r["symbol"], r["market"],
                         datetime.fromisoformat(r["as_of"]), r["direction"],
                         r["confidence"], r["signal_id"]) for r in rows]


def lateness_line(rows) -> str:
    """as_of → 생성 지연을 요약한다.

    **이 지연은 표본에서 빼지 않는다** (2026-11-22 결정). 컨텍스트가
    `known_at <= as_of`로 막혀 있어 LLM은 미래를 못 보고, 이 프로젝트가 재는 것은
    실행 가능성이 아니라 **구조의 판단력**이다. 다만 **한 번도 명시적으로 결정한
    적 없던 사실**이라 숫자를 늘 보이게 둔다 — 노트북이 자다 깨며 도는 구조라
    회차가 몇 시간에서 며칠까지 늘어진다.

    층 2(가상 포트폴리오)는 "체결은 시그널 당일 종가" 전제라 이 지연이 더 아프다.
    `docs/baselines.md` 7.1절과 같이 읽는다.
    """
    hours = []
    for r in rows:
        try:
            c = datetime.fromisoformat(r["created_at"])
            a = datetime.fromisoformat(r["as_of"])
        except (ValueError, TypeError, KeyError):
            continue
        hours.append((c - a).total_seconds() / 3600)
    if not hours:
        return ""
    hours.sort()
    mid = hours[len(hours) // 2]
    over24 = sum(1 for h in hours if h > 24)
    return (f"  생성 지연  중앙값 {mid:.1f}h · 최대 {hours[-1]:.0f}h · "
            f"24h 초과 {over24}건({over24/len(hours)*100:.1f}%)")


def main() -> int:
    ap = argparse.ArgumentParser(description="시그널 채점")
    ap.add_argument("--market", choices=("KR", "US"), action="append")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--detail", action="store_true")
    args = ap.parse_args()

    store = SignalStore()
    scorer = Scorer()
    markets = args.market or ["KR", "US"]

    for market in markets:
        sigs = load(store, market, args.run_id)
        if not sigs:
            print(f"{market}: 시그널이 없습니다.")
            continue
        scored = scorer.score_many(sigs)
        print(f"════ {market} · 시그널 {len(sigs)}건 ════")
        with store.connect() as conn:
            raw = [dict(r) for r in conn.execute(
                "SELECT as_of, created_at FROM signals WHERE market = ?", (market,))]
        line = lateness_line(raw)
        if line:
            print(line)

        if args.detail:
            for s in scored:
                h5, h20 = s.horizons.get(5), s.horizons.get(20)
                def fmt(h):
                    if h is None or h.status != "scored":
                        return f"{h.status if h else '-':>10s}"
                    return (f"{h.directional*100:+6.2f}%"
                            f"{'  적중' if h.hit else ('  빗나감' if h.hit is False else '  hold')}")
                print(f"  {s.symbol:<8s} {s.direction:<5s} 확신도 {s.confidence:.2f}  "
                      f"기준 {s.base_date}  5일 {fmt(h5)}   20일 {fmt(h20)}")

        for n in HORIZONS:
            sm = summarize(scored, n)
            status_counts = sm["by_status"]
            line = f"  {n:>2}거래일  채점 {sm['scored']}/{sm['signals']}"
            if sm["scored"]:
                c = sm["confidence_correlation"]
                if sm["hit_rate"] is not None:
                    line += f"  적중률 {sm['hit_rate']:.3f}"
                    ci = sm["hit_rate_ci"]
                    if ci:
                        # 구간이 0.5를 품으면 동전과 구분되지 않는다. 그게 보이게 찍는다.
                        mark = " " if ci[0] > 0.5 or ci[1] < 0.5 else "~"
                        line += f"[{ci[0]:.2f},{ci[1]:.2f}]{mark}"
                else:
                    line += "  적중률 -"
                if sm["mean_excess"] is not None:
                    line += (f"  평균초과 {sm['mean_excess']*100:+.2f}%"
                             f"  비용후 {sm['mean_excess_after_cost']*100:+.2f}%")
                    ci = sm["mean_excess_ci"]
                    if ci:
                        line += f"[{ci[0]*100:+.2f},{ci[1]*100:+.2f}]%p"
                if c["spearman"] is not None:
                    line += f"  확신도상관 {c['spearman']:+.3f}(n={c['n']})"
            print(line)
            rest = {k: v for k, v in status_counts.items() if k != "scored"}
            if rest:
                print(f"            미채점 {rest}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
