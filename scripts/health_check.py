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
# launchd 작업이 이보다 오래 떠 있으면 멈춘 것으로 본다. 가장 긴 베이스라인도
# 정상 회차는 1시간 안쪽이다(09-30 실측 KR 59분 + US 10분).
STUCK_JOB_SECONDS = 4 * 3600
DISK_WARN_GB, DISK_CRIT_GB = 10, 3


def _etime_seconds(text):
    """`ps -o etime`의 경과 시간을 초로. `MM:SS` · `HH:MM:SS` · `DD-HH:MM:SS`.

    macOS ps에는 `etimes`(초 단위) 키워드가 없다 — 직접 파싱한다.
    """
    text = (text or "").strip()
    if not text:
        return None
    days = 0
    if "-" in text:
        head, text = text.split("-", 1)
        days = int(head)
    try:
        parts = [int(x) for x in text.split(":")]
    except ValueError:
        return None
    if not 1 <= len(parts) <= 3:
        return None
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, sec = parts
    return days * 86400 + h * 3600 + m * 60 + sec


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


def _adjacent_hour(a: str, b: str) -> bool:
    """"09-28 11시"와 "09-28 12시"처럼 붙어 있는가. 장 시작 전후는 안 잇는다."""
    (da, ha), (db, hb) = [(x[:5], int(x[6:-1])) for x in (a, b)]
    return da == db and hb == ha + 1


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
    # **창에 반만 걸친 시간도 판정하지 않는다** (2026-10-07).
    # `seen`은 24시간 창 **안의** 회차만 센다. 창이 10:29에 시작하면 그 시간대의
    # 수집이 10:19에 있었어도 10분 차이로 밖이라, 10시가 통째로 빈 것으로 보인다.
    # 실제로 같은 데이터가 21:19 점검에서는 `09시·11시 (연속 1, 경고)`였는데
    # 다음날 10:29 점검에서는 `10시·11시 (연속 2, 심각)`이 됐다 —
    # **데이터는 그대로인데 창이 미끄러져 경고가 심각으로 바뀌었다.**
    # 뒤쪽 경계(`end`)는 09-28에 막았는데 앞쪽은 그대로였다. 같은 실수의 반대편이다.
    start = (now - timedelta(hours=24)).astimezone(KST)
    cur = start.replace(minute=0, second=0, microsecond=0)
    if cur < start:
        cur += timedelta(hours=1)      # 반만 걸친 첫 시간은 건너뛴다
    # **진행 중인 시간은 판정하지 않는다** (2026-09-28). 수집기는 종목별 주기를 보고
    # "아직 안 됐다"면 회차 기록을 남기지 않는다. 14:11에 점검하면 13:18에 다 받아둔
    # 상태여도 14시가 빈 것으로 보인다 — 매일 거짓 경보가 뜨면 진짜 구멍을 흘려보낸다.
    end = now.astimezone(KST).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    while cur <= end:
        if cur.weekday() < 5 and KR_MARKET[0] <= cur.hour < KR_MARKET[1]:
            if not seen.get((cur.date(), cur.hour)):
                holes.append(f"{cur:%m-%d %H}시")
        cur += timedelta(hours=1)
    if not holes:
        return _check("한국 장중 커버리지", OK, "지난 24시간 장중에 빈 시간 없음")

    # **단발 구멍과 연속 구멍을 가른다** (2026-09-28).
    # 수집기는 종목별 주기를 보고 "이번 시간엔 받을 게 없다"면 회차를 남기지 않는다.
    # 그런 한 시간은 손실이 아니다 — 다음 회차의 lookback이 덮는다.
    # **영구 손실은 연속으로 멈췄을 때 생긴다.** 1,000건 한도에 잘리려면 몇 시간이
    # 쌓여야 한다. 단발까지 심각으로 올리면 매일 울리고, 그러면 진짜를 흘려보낸다.
    longest = run = 1
    for a, b in zip(holes, holes[1:]):
        run = run + 1 if _adjacent_hour(a, b) else 1
        longest = max(longest, run)
    state = CRIT if longest >= 2 else WARN
    tail = ("과거 뉴스는 다시 살 수 없다" if state is CRIT
            else "단발이라 다음 회차 lookback이 덮는다")
    return _check("한국 장중 커버리지", state,
                  f"{len(holes)}시간 비어 있다 (최장 연속 {longest}) — "
                  f"{', '.join(holes[:8])} ({tail})")


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
    # **심각이다** (2026-10-07 정정). 경고로 두었더니 주간 베이스라인이 한 주
    # 통째로 죽은 것을 아무도 안 봤다. 놓친 주의 표본은 되살릴 수 없다 —
    # as_of가 고정 요일에 묶여 있어 다음 주에 두 번 낼 수 없다.
    state = CRIT if days > BASELINE_MAX_DAYS else OK
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


def check_benchmark(price_store):
    """지수가 종목보다 뒤처지지 않았는가.

    **왜 따로 보는가** (2026-09-28). FinanceDataReader의 한국 지수 계열(KS200·KS11·KQ11)이
    09-18부터 0행을 주기 시작했다. 개별 종목은 같은 벤더에서 멀쩡히 들어왔다.
    전체 최신 봉만 보면 종목 날짜에 가려 안 보인다 — 실제로 못 봤다.

    지수가 없으면 초과수익을 못 구하고, 초과수익이 없으면 그 시장 채점이 통째로 막힌다.
    **다만 가격은 뉴스와 달리 백필되므로 영구 손실이 아니다 → 경고다.**
    이 항목이 OK로 돌아오는 순간이 벤더 복구 시점이고, 그때 백필하면 된다.
    """
    from src.data.price_sources import BENCHMARKS
    u = load_universe()
    lagging = []
    for market, (index_symbol, index_name) in BENCHMARKS.items():
        index_last = price_store.latest_date(index_symbol)
        stock_last = max((d for d in (price_store.latest_date(h.symbol)
                                      for h in u.market(market)) if d), default=None)
        if stock_last is None:
            continue
        if index_last is None:
            lagging.append(f"{index_name} 봉이 없다"); continue
        behind = (stock_last - index_last).days
        # 1일은 벤더 게시 시차로 흔히 생긴다. 그 이상이면 계열이 멈춘 것이다.
        if behind > 1:
            lagging.append(f"{index_name} {index_last} (종목보다 {behind}일 뒤 · {market} 채점 불가)")
    if not lagging:
        return _check("벤치마크 지수", OK, "종목과 같은 날짜까지 들어와 있다")
    return _check("벤치마크 지수", WARN, " · ".join(lagging))


def check_disk(repo):
    out = subprocess.run(["df", "-g", str(repo)], capture_output=True, text=True).stdout
    free = int(out.strip().split("\n")[-1].split()[3])
    state = CRIT if free < DISK_CRIT_GB else (WARN if free < DISK_WARN_GB else OK)
    return _check("디스크", state, f"여유 {free}GB")


def check_scheduler(repo):
    """launchd 작업이 등록돼 있고, 종료 코드가 0이고, **멈춰 있지 않은가.**

    **종료코드 0은 "지금 잘 돌고 있다"가 아니다** (2026-10-07에 배웠다).
    09-30 성공 직후 잉여 발화가 하나 더 떴고 그것이 `nslookup`에서 멈췄다.
    launchd는 그 작업을 `state = running`으로 보므로 **새 인스턴스를 띄우지 않는다** —
    주간 베이스라인이 한 주 통째로 죽었다. 그런데 `last exit code`는 **이전**
    인스턴스가 남긴 0이라 이 점검은 끝까지 "모두 정상"이라고 말했다.

    그래서 지금 떠 있는 프로세스가 **얼마나 오래 떠 있는지**를 따로 본다.
    """
    stuck, problems = [], []
    for job in ("collect", "prices", "baseline"):
        label = f"com.ys.mastrade.{job}"
        out = subprocess.run(["launchctl", "print", f"gui/{__import__('os').getuid()}/{label}"],
                             capture_output=True, text=True)
        if out.returncode != 0:
            problems.append(f"{job} 미등록"); continue

        field = lambda key: next(
            (l.split("=", 1)[1].strip() for l in out.stdout.splitlines()
             if l.strip().startswith(key)), "")
        code = field("last exit code")
        pid = field("pid")

        if pid.isdigit():
            ps = subprocess.run(["ps", "-o", "etime=", "-p", pid],
                                capture_output=True, text=True)
            secs = _etime_seconds(ps.stdout) if ps.returncode == 0 else None
            if secs is not None and secs > STUCK_JOB_SECONDS:
                stuck.append(f"{job} {secs // 3600}시간째 실행 중 (pid {pid}) — 멈춘 듯하다")
        # **실행 중인 작업에는 `last exit code`가 아직 없다.** 그걸 "?"로 읽어
        # 문제로 세면 정상 실행 중에 매번 경고가 뜬다 (2026-10-07 실측).
        if code and code not in ("0", "(never exited)"):
            problems.append(f"{job} 종료코드 {code}")

    if stuck:
        return _check("스케줄러", CRIT, " · ".join(stuck + problems))
    if problems:
        return _check("스케줄러", WARN, " · ".join(problems))
    return _check("스케줄러", OK, "collect · prices · baseline 모두 정상")


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
        check_benchmark(price_store),
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
