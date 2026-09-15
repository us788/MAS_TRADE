"""수집 오케스트레이션.

스케줄은 **마지막 수집 시각**으로 판단한다 (`collection_runs` 테이블). cron은
한 시간마다 깨우기만 하고, 이 모듈이 "지금 돌 종목"을 고른다. cron에 종목별
스케줄을 흩어 놓으면 주기를 바꿀 때마다 crontab을 고쳐야 한다.

수집 실패와 커버리지 미달은 **예외가 아니라 정상 경로**다. 한 종목이 실패해도
나머지는 계속 돌고, 실패는 gap으로 남아 다음 실행의 대상이 된다. 과거 뉴스는
나중에 살 수 없으므로 "조용히 건너뛰기"가 최악이다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from src.data.news import NewsBatch
from src.data.storage import Store
from src.data.universe import Holding, Universe

# 주기 경계에서 cron 지터로 한 판을 통째로 건너뛰는 것을 막는 여유.
# 1시간 주기인데 cron이 1분 늦게 깨우면 다음 시간까지 밀려 구멍이 생긴다.
DUE_SLACK = timedelta(minutes=5)

# lookback은 **마지막 수집 이후 경과 시간** 기준으로 정한다. 주기 기준 고정값을 쓰면
# 두 방향으로 틀린다 — 평소엔 필요 이상으로 받아 페이지 상한에 가까워지고
# (실측: 1시간 주기에 6시간을 받아 10페이지 중 8을 소진), cron이 몇 시간 멈췄다
# 살아났을 때는 그 구간을 못 메운다.
LOOKBACK_MULTIPLIER = 1.5
"""경과 시간의 몇 배를 받을지. 1배면 경계가 딱 붙어 조금만 어긋나도 샌다."""
FIRST_RUN_MULTIPLIER = 2.0
"""첫 수집은 기준이 없으므로 주기의 배수로 잡는다."""
MIN_LOOKBACK_HOURS = 1.0
MAX_LOOKBACK_DAYS = 7.0
"""장기 중단 뒤 무한정 거슬러 올라가지 않게 막는다. 어차피 벤더가 안 준다."""


@dataclass
class Plan:
    holding: Holding
    source: str
    interval_hours: int
    last_run: datetime | None
    due: bool
    reason: str
    lookback_days: float = 0.0
    """직전 수집 이후 구간을 겹쳐 덮는다. build_plan이 계산해 넣는다."""


@dataclass
class RunSummary:
    attempted: int = 0
    succeeded: int = 0
    inserted: int = 0
    duplicates: int = 0
    gaps: list[str] = field(default_factory=list)


SOURCE_BY_MARKET = {"KR": "naver", "US": "finnhub"}


def build_plan(
    store: Store,
    universe: Universe,
    now: datetime,
    markets: tuple[str, ...] = ("KR", "US"),
    symbols: tuple[str, ...] = (),
    force: bool = False,
) -> list[Plan]:
    """지금 돌아야 할 종목을 고른다."""
    plans: list[Plan] = []
    for market in markets:
        source = SOURCE_BY_MARKET[market]
        for holding in universe.market(market):
            if symbols and holding.symbol not in symbols:
                continue
            last = store.last_run_at(holding.symbol, source)
            interval = max(1, holding.collect_every_hours)
            if force:
                due, reason = True, "강제 실행"
            elif last is None:
                due, reason = True, "첫 수집"
            else:
                elapsed = _utc(now) - last
                due = elapsed + DUE_SLACK >= timedelta(hours=interval)
                reason = (f"{elapsed.total_seconds()/3600:.1f}h 경과 / {interval}h 주기"
                          if due else
                          f"대기 ({(timedelta(hours=interval) - elapsed).total_seconds()/3600:.1f}h 남음)")
            plans.append(Plan(holding, source, interval, last, due, reason,
                              lookback_days=compute_lookback_days(last, interval, now)))
    return plans


def compute_lookback_days(
    last_run: datetime | None, interval_hours: int, now: datetime
) -> float:
    """받아올 구간의 길이(일).

    - 첫 수집: 기준이 없으므로 주기의 2배
    - 평소: 마지막 수집 이후 경과 시간의 1.5배 (겹쳐 받고 중복은 DB가 막는다)
    - 하한 1시간, 상한 7일
    """
    if last_run is None:
        hours = interval_hours * FIRST_RUN_MULTIPLIER
    else:
        elapsed_h = (_utc(now) - _utc(last_run)).total_seconds() / 3600
        hours = max(elapsed_h, 0.0) * LOOKBACK_MULTIPLIER
    hours = max(hours, MIN_LOOKBACK_HOURS)
    return min(hours / 24, MAX_LOOKBACK_DAYS)


def collect_one(store: Store, adapter, plan: Plan, as_of: datetime) -> tuple[NewsBatch | None, str]:
    """한 종목을 수집해 저장한다. 실패는 예외를 올리지 않고 사유를 돌려준다."""
    holding = plan.holding
    try:
        batch = adapter.collect(
            holding.symbol,
            as_of,
            plan.lookback_days,
            query=holding.news_query,
            names=holding.names,
            exclude=list(holding.news_exclude),
        )
    except Exception as exc:                                   # noqa: BLE001
        reason = f"{type(exc).__name__}: {exc}"
        store.record_gap(holding.market, plan.source, holding.symbol, reason, as_of)
        return None, reason

    if batch.raw:
        store.save_raw(plan.source, holding.symbol, batch.raw, as_of)

    result = store.add_news(batch.items)
    store.record_run(
        holding.symbol, plan.source,
        ran_at=as_of, oldest_seen=batch.oldest_seen, reached_floor=batch.reached_floor,
        pages=batch.pages, fetched=len(batch.items),
        inserted=result.written, duplicates=result.skipped_duplicate,
    )

    if not batch.reached_floor:
        # 페이지 상한에 걸려 lookback 구간을 다 못 받았다. 그 앞은 영구 손실이므로
        # 기록해 두고 주기를 줄여야 한다.
        reason = (f"커버리지 미달: {batch.pages}페이지로 {plan.lookback_days:.2f}일을 "
                  f"덮지 못함 (최古 {batch.oldest_seen})")
        store.record_gap(holding.market, plan.source, holding.symbol, reason, as_of)
        return batch, reason

    return batch, ""


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
