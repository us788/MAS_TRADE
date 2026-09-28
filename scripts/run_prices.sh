#!/bin/bash
#
# 일일 가격 수집 래퍼 — launchd가 매일 이 스크립트를 부른다.
#
# **왜 별도 작업인가** (2026-09-28).
# 09-17부터 11일간 가격이 한 봉도 안 들어왔다. 원인은 벤더도 네트워크도 아니고
# **어느 launchd 작업도 collect_prices.py를 부르지 않았다**는 것이었다.
# collect는 뉴스만, baseline은 시그널만 불렀다. 손으로 돌릴 때만 들어오던 셈이다.
#
# 피해가 두 갈래였다.
#   1) 채점 불가 — 시그널 53건이 전부 stale이었다
#   2) **시그널 오염** — 09-23 회차가 닷새 묵은 종가를 기준가로 썼다 (misaligned 23건)
#
# 2번은 run_baseline.sh가 시그널 직전에 가격을 받는 것으로 막았다. 이 작업은 1번용이다 —
# 채점은 매일 최신 봉이 있어야 따라간다.
#
# **뉴스와 달리 가격은 영구 손실이 아니다.** 벤더가 과거 봉을 계속 주므로 며칠 걸러도
# 다음 실행이 메운다. 그래서 하루 1회로 충분하고, 실패해도 조용히 넘어간다.
#
# 발화 조건은 health와 같다 — 매일 정해진 시각 + 네트워크 연결 변경(뚜껑 열림).
# 노트북은 정해진 시각에 켜져 있지 않다. 하루 1회 가드는 스탬프 파일로 건다.

set -uo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON="$REPO/.venv/bin/python"
LOG="$REPO/logs/prices.log"
STAMP="$REPO/logs/.prices.lastrun"
MAX_LOG_BYTES=$((5 * 1024 * 1024))
# 이보다 묵으면 이 작업이 제 몫을 못 한 것으로 본다. 주말+연휴를 감안한 값이다.
MAX_AGE_DAYS=5

mkdir -p "$REPO/logs"

# 하루 1회 가드. KST 기준이다 — 한국장 마감이 하루의 경계다.
TODAY="$(TZ=Asia/Seoul date '+%Y-%m-%d')"
if [ "${1:-}" != "--force" ] && [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$TODAY" ]; then
    exit 0
fi

if [ -f "$LOG" ] && [ "$(stat -f%z "$LOG" 2>/dev/null || echo 0)" -ge "$MAX_LOG_BYTES" ]; then
    mv -f "$LOG" "$LOG.1"
fi

log() {
    printf '%s  %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >> "$LOG"
}

# DarkWake 직후에는 DNS가 아직 안 붙어 있다 (2026-09-17 장애). 최대 30초 기다린다.
wait_for_dns() {
    local host="$1" i
    for i in 1 2 3 4 5 6; do
        /usr/bin/nslookup -timeout=3 "$host" >/dev/null 2>&1 && return 0
        sleep 5
    done
    return 1
}

log "───── 가격 수집 시작 (pid $$)"

if [ ! -x "$PYTHON" ]; then
    log "FAIL  venv python이 없다: $PYTHON"
    exit 1
fi

if ! wait_for_dns "query1.finance.yahoo.com"; then
    log "SKIP  DNS가 준비되지 않았다 — 다음 발화에서 다시 받는다 (가격은 영구 손실이 아니다)"
    exit 0
fi

cd "$REPO" || { log "FAIL  cd 실패: $REPO"; exit 1; }

# caffeinate로 감싸는 이유는 run_collect.sh와 같다 (DarkWake가 41초 만에 재취침한다).
/usr/bin/caffeinate -im "$PYTHON" scripts/collect_prices.py >> "$LOG" 2>&1 \
    || log "WARN  실패한 대상이 있다 (위 FAIL 줄) — 종목 신선도로 다시 판정한다"

# **수집기 종료코드를 그대로 쓰지 않는다** (2026-09-28).
# 지수 하나가 실패해도 종료코드는 1이다. 그걸 실패로 치면 스탬프를 못 찍어
# 네트워크가 바뀔 때마다 32종목을 다시 받는다 — 벤더 장애가 길어지면 하루 종일 그런다.
#
# 이 작업의 목적은 **종목 봉을 최신으로 유지하는 것**이다. 지수가 막힌 것은
# health_check의 "벤치마크 지수" 항목이 따로 보고한다. 역할을 겹치지 않게 둔다.
if "$PYTHON" scripts/collect_prices.py --check-fresh "$MAX_AGE_DAYS" >> "$LOG" 2>&1; then
    log "───── 완료 (종목 신선도 OK)"
    printf '%s' "$TODAY" > "$STAMP"
    exit 0
fi

log "───── 완료 (종목이 ${MAX_AGE_DAYS}일보다 묵었다) — 스탬프를 찍지 않는다. 오늘 안에 재시도된다"
exit 1
