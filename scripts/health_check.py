"""일일 점검 — 파이프라인이 조용히 죽지 않았는지 본다.

**launchd의 가장 큰 약점은 조용히 죽는 것이다.** 로그를 안 보면 며칠 뒤에야 안다.
이 스크립트가 기계적 판정을 대신한다 (`CLAUDE.md` 3절 — 계산은 코드).

    python scripts/health_check.py            사람이 읽는 형태
    python scripts/health_check.py --notify   이상이 있으면 macOS 알림도 띄운다
    python scripts/health_check.py --json     기계가 읽는 형태

종료 코드: 0 정상 · 1 경고 · 2 심각.

**심각/경고를 가르는 기준은 "되돌릴 수 있는가"다.** 과거 뉴스는 다시 살 수 없으므로
한국 장중 구멍은 심각이고, 디스크 여유나 베이스라인 지연은 경고다.
"""
import argparse
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agents.store import SignalStore
from src.data.prices import PriceStore
from src.data.storage import Store
from src.data.universe import load_universe

KST = timezone(timedelta(hours=9))
UTC = timezone.utc

OK, WARN, CRIT = "OK", "경고", "심각"
RANK = {OK: 0, WARN: 1, CRIT: 2}

# 한국 정규장 (KST). 이 구간의 구멍은 1,000건 한도에 잘려 영구 손실이 된다.
KR_MARKET = (9, 16)
# 수집 주기의 몇 배까지 봐주는가. 1배는 정상 흔들림에도 걸린다.
STALE_FACTOR = 2.5
BASELINE_MAX_DAYS = 8          # 주 1회 + 하루 여유
DISK_WARN_GB, DISK_CRIT_GB = 10, 3


def _check(name, state, detail):
    return {"name": name, "state": state, "detail": detail}


def check_collection_freshness(store, now):
    """종목별 마지막 수집이 주기를 크게 넘겼는가."""
    u = load_universe()
    late = []
    for market in ("KR", "US"):
        for h in u.market(market):
            last = store.last_run_at(h.symbol, "naver" if market == "KR" else "finnhub")
            if last is None:
                late.append((h.name, None, h.collect_every_hours)); continue
            hours = (now - last).total_seconds() / 3600
            if hours > h.collect_every_hours * STALE_FACTOR:
                late.append((h.name, hours, h.collect_every_hours))
    if not late:
        return _check("수집 신선도", OK, "전 종목이 주기 안에 있다")
    worst = max((h for _, h, _ in late if h), default=None)
    state = CRIT if worst and worst > 12 else WARN
    detail = ", ".join(f"{n} {f'{h:.1f}h' if h else '기록없음'}/{c}h" for n, h, c in late[:6])
    return _check("수집 신선도", state, f"{len(late)}종목 주기 초과 — {detail}")


def check_market_hours_holes(store, now):
    """지난 24시간 한국 장중에 시간당 수집 기록이 있는가."""
    since = (now - timedelta(hours=24)).isoformat()
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT ran_at FROM collection_runs WHERE ran_at >= ?", (since,)).fetchall()
    seen = defaultdict(int)
    for r in rows:
        k = datetime.fromisoformat(r["ran_at"]).astimezone(KST)
        seen[(k.date(), k.hour)] += 1

    holes = []
    cur = (now - timedelta(hours=24)).astimezone(KST)
    end = now.astimezone(KST)
    while cur <= end:
        if cur.weekday() < 5 and KR_MARKET[0] <= cur.hour < KR_MARKET[1]:
            if not seen.get((cur.date(), cur.hour)):
                holes.append(f"{cur:%m-%d %H}시")
        cur += timedelta(hours=1)
    if not holes:
        return _check("한국 장중 커버리지", OK, "지난 24시간 장중에 빈 시간 없음")
    return _check("한국 장중 커버리지", CRIT,
                  f"{len(holes)}시간 비어 있다 — {', '.join(holes[:8])} "
                  f"(과거 뉴스는 다시 살 수 없다)")


def check_gaps(store):
    gaps = store.open_gaps()
    if not gaps:
        return _check("열린 gap", OK, "없음")
    shortfall = [g for g in gaps if g["reason"].startswith("커버리지 미달")]
    failed = len(gaps) - len(shortfall)
    parts = []
    if shortfall:
        parts.append(f"커버리지 미달 {len(shortfall)}건 (영구 손실)")
    if failed:
        parts.append(f"미복구 수집 실패 {failed}건")
    return _check("열린 gap", CRIT if shortfall else WARN, " · ".join(parts))


def check_baseline(signal_store, now):
    with signal_store.connect() as conn:
        row = conn.execute(
            "SELECT MAX(started_at) t, COUNT(*) n FROM runs WHERE succeeded > 0").fetchone()
    if not row or not row["t"]:
        return _check("베이스라인", WARN, "실행 기록이 없다")
    days = (now - datetime.fromisoformat(row["t"])).total_seconds() / 86400
    state = WARN if days > BASELINE_MAX_DAYS else OK
    return _check("베이스라인", state,
                  f"마지막 실행 {days:.1f}일 전 (누적 {row['n']}회)")


def check_prices(price_store, now):
    cov = price_store.coverage("005930")
    if not cov["last"]:
        return _check("가격 적재", CRIT, "봉이 없다")
    from datetime import date
    days = (now.astimezone(KST).date() - date.fromisoformat(cov["last"])).days
    state = WARN if days > 5 else OK
    return _check("가격 적재", state, f"마지막 봉 {cov['last']} ({days}일 전) · {cov['bars']}봉")


def check_disk(repo):
    out = subprocess.run(["df", "-g", str(repo)], capture_output=True, text=True).stdout
    free = int(out.strip().split("\n")[-1].split()[3])
    state = CRIT if free < DISK_CRIT_GB else (WARN if free < DISK_WARN_GB else OK)
    return _check("디스크", state, f"여유 {free}GB")


def check_scheduler(repo):
    """launchd 작업이 등록돼 있고 마지막 종료 코드가 0인가."""
    results = []
    for job in ("collect", "baseline"):
        label = f"com.ys.mastrade.{job}"
        out = subprocess.run(["launchctl", "print", f"gui/{__import__('os').getuid()}/{label}"],
                             capture_output=True, text=True)
        if out.returncode != 0:
            results.append(f"{job} 미등록"); continue
        code = next((l.split("=")[1].strip() for l in out.stdout.splitlines()
                     if "last exit code" in l), "?")
        if code not in ("0", "(never exited)"):
            results.append(f"{job} 종료코드 {code}")
    if not results:
        return _check("스케줄러", OK, "collect · baseline 둘 다 정상")
    return _check("스케줄러", WARN, " · ".join(results))


def main() -> int:
    ap = argparse.ArgumentParser(description="파이프라인 일일 점검")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--notify", action="store_true", help="이상 시 macOS 알림")
    args = ap.parse_args()

    repo = Path(__file__).resolve().parent.parent
    now = datetime.now(UTC)
    store, price_store, signal_store = Store(), PriceStore(), SignalStore()

    checks = [
        check_market_hours_holes(store, now),
        check_collection_freshness(store, now),
        check_gaps(store),
        check_prices(price_store, now),
        check_baseline(signal_store, now),
        check_scheduler(repo),
        check_disk(repo),
    ]
    worst = max(checks, key=lambda c: RANK[c["state"]])["state"]

    if args.json:
        print(json.dumps({"at": now.isoformat(), "state": worst, "checks": checks},
                         ensure_ascii=False, indent=1))
    else:
        print(f"점검 {now.astimezone(KST):%Y-%m-%d %H:%M KST}  →  {worst}")
        for c in checks:
            mark = {OK: "  ", WARN: "! ", CRIT: "!!"}[c["state"]]
            print(f"  {mark} {c['name']:<16s} {c['detail']}")

    if args.notify and worst != OK:
        bad = [c for c in checks if c["state"] != OK]
        body = " / ".join(f"{c['name']}: {c['detail'][:60]}" for c in bad[:3])
        subprocess.run(["osascript", "-e",
                        f'display notification {json.dumps(body)} '
                        f'with title "MAS_TRADE 점검: {worst}"'], check=False)
    return RANK[worst]


if __name__ == "__main__":
    raise SystemExit(main())
