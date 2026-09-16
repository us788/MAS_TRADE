"""벤치마크 — 기획서 9.4절.

| 시장 | 벤치마크 |
|---|---|
| US | S&P500 buy&hold, 동일가중 랜덤 15종목, DeepSeek 단일 호출 |
| KR | KOSPI200 buy&hold, 동일가중 랜덤 15종목, DeepSeek 단일 호출 |

여기 있는 것은 **LLM 없이 계산되는 둘**이다. DeepSeek 단일 호출 베이스라인은
에이전트 레이어가 생긴 뒤에 붙는다.

랜덤 벤치마크는 두 가지 역할을 한다.

1. 기획서가 요구한 비교 기준
2. **저울 자체의 검정.** 랜덤 시그널의 적중률이 50%에서 멀거나 확신도 상관이 0에서
   멀면 채점기가 편향된 것이다. 시스템 성적을 해석하기 전에 이걸 먼저 본다.

**시드를 반드시 남긴다** (9.4절). 안 남기면 재현이 안 되고, 유리한 시드를 고른 것처럼
보인다. 모든 함수가 쓴 시드를 결과에 담아 돌려준다.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Sequence
from zoneinfo import ZoneInfo

from src.compute import metrics
from src.data.price_sources import BENCHMARKS
from src.data.prices import PriceStore
from src.data.universe import load_universe

DIRECTIONS = ("buy", "sell", "hold")

# 시그널 생성 시각. 기획서 4.2절의 오프피크 배치이면서, US/KR 기준가가 대칭이 되는
# 시각이다 (docs/harness.md 2절). KST 07:00 = 미국 전 거래일 마감 뒤.
SIGNAL_TZ = ZoneInfo("Asia/Seoul")
SIGNAL_HOUR = 7
SIGNAL_WEEKDAY = 2          # 수요일. 요일을 고정해야 비교가 가능하다 (9.1절)


@dataclass(frozen=True)
class RandomSignal:
    """채점기가 읽는 필드만 가진 최소 시그널."""
    symbol: str
    market: str
    as_of: datetime
    direction: str
    confidence: float


def signal_dates(start: date, end: date, weekday: int = SIGNAL_WEEKDAY) -> list[date]:
    """주 1회, 고정 요일. 요일·시각을 고정해야 구간 간 비교가 성립한다 (9.1절)."""
    out, cur = [], start
    while cur <= end:
        if cur.weekday() == weekday:
            out.append(cur)
        cur += timedelta(days=1)
    return out


def random_signals(market: str, start: date, end: date, seed: int) -> list[RandomSignal]:
    """동일가중 랜덤 벤치마크. 유니버스 전 종목에 매주 무작위 방향·확신도를 준다.

    `as_of`는 KST 07:00로 고정한다 — US/KR 기준가가 대칭이 되는 시각이다.
    """
    rng = random.Random(seed)
    universe = load_universe()
    out = []
    for d in signal_dates(start, end):
        as_of = datetime(d.year, d.month, d.day, SIGNAL_HOUR, 0, tzinfo=SIGNAL_TZ)
        for h in universe.market(market):
            out.append(RandomSignal(h.symbol, market, as_of,
                                    rng.choice(DIRECTIONS), round(rng.random(), 2)))
    return out


def index_buy_and_hold(store: PriceStore, market: str,
                       start: date, end: date) -> dict:
    """지수 buy&hold. 종목 초과수익의 기준선이자 "그냥 지수 샀으면" 비교군.

    `close_px`를 쓴다 — 채점기와 같은 기준이어야 나란히 놓을 수 있다.
    """
    symbol, label = BENCHMARKS[market]
    first = store.close_at_date(symbol, start)
    last = store.close_at_date(symbol, end)
    if first is None or last is None:
        return {"market": market, "index": label, "status": "no_data"}
    years = (last.date - first.date).days / 365.25
    return {
        "market": market, "index": label, "symbol": symbol,
        "from": first.date, "to": last.date,
        "total_return": metrics.pct_return(first.close_px, last.close_px),
        "cagr": metrics.cagr(first.close_px, last.close_px, years) if years > 0 else None,
    }


def calibration_report(runs: Sequence[tuple[int, dict[int, dict]]],
                       tolerance: float = 0.05) -> dict:
    """랜덤 벤치마크로 채점기를 검정한다. `runs`는 (시드, {호라이즌: 집계}) 목록이다.

    랜덤이면 적중률은 0.5, 확신도 상관은 0이어야 한다. 벗어나면 **시스템 성적을
    해석하기 전에 저울부터 의심한다.**

    **시드 하나로 판정하지 않는다** (기획서 6절: 3회 이상 반복해 평균이 아니라
    분산까지 본다). 시드 하나의 적중률은 표본 분할 운에 흔들린다.

    `mean_excess`는 검정 지표로 쓰지 않는다. 유니버스 15종목 동일가중은 지수가
    아니므로 원 초과수익 평균이 0이 아니고, buy/sell 표본이 어느 쪽에 몰리느냐에
    따라 방향 평균이 흔들린다. 적중률과 순위상관이 그 흔들림에 훨씬 둔감하다.
    """
    horizons = sorted({n for _, summ in runs for n in summ})
    checks = []
    for n in horizons:
        hits = [summ[n].get("hit_rate") for _, summ in runs if n in summ]
        corrs = [(summ[n].get("confidence_correlation") or {}).get("spearman")
                 for _, summ in runs if n in summ]
        hits = [h for h in hits if h is not None]
        corrs = [c for c in corrs if c is not None]
        hit_mean, corr_mean = metrics.mean(hits), metrics.mean(corrs)
        checks.append({
            "trading_days": n,
            "runs": len(hits),
            "hit_rate_mean": hit_mean,
            "hit_rate_sd": metrics.stdev(hits),
            "hit_rate_range": (min(hits), max(hits)) if hits else None,
            "hit_rate_ok": hit_mean is not None and abs(hit_mean - 0.5) <= tolerance,
            "spearman_mean": corr_mean,
            "spearman_sd": metrics.stdev(corrs),
            "spearman_range": (min(corrs), max(corrs)) if corrs else None,
            "confidence_ok": corr_mean is not None and abs(corr_mean) <= tolerance,
        })
    return {"seeds": [seed for seed, _ in runs], "tolerance": tolerance,
            "checks": checks,
            "passed": bool(checks) and all(c["hit_rate_ok"] and c["confidence_ok"]
                                           for c in checks)}
