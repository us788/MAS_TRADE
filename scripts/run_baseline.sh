#!/bin/bash
#
# 베이스라인 시그널 생성 래퍼 — launchd가 주 1회 이 스크립트를 부른다.
#
# 뉴스 수집(run_collect.sh)과 다른 점이 둘 있다.
#
# 1. **주 1회다.** 기획서 9.1절이 고정 요일·시각을 요구한다 — 요일이 흔들리면
#    구간 간 비교가 깨진다. `--weekly`가 as_of를 직전 수요일 07:00 KST로 스냅하므로
#    늦게 깨어나도 요일은 유지된다.
# 2. **돈이 나간다.** 30종목 1회에 약 $0.5다. 중복 실행은 비용이자 표본 오염이므로
#    `--weekly`가 이미 돌린 시점을 건너뛴다.
#
# 자기 위치에서 저장소 루트를 찾는다. 저장소를 옮겨도 깨지지 않는다.

set -uo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON="$REPO/.venv/bin/python"
LOG_DIR="$REPO/logs"
LOG="$LOG_DIR/baseline.log"
LOCK="$LOG_DIR/.baseline.lock"

MAX_LOG_BYTES=$((10 * 1024 * 1024))
LOG_KEEP=3
DISK_ABORT_GB=1
# LLM 호출이 30건 × 최대 수십 초라 한 판이 30분을 넘을 수 있다. 수집보다 길게 잡는다.
LOCK_STALE_SECONDS=7200

mkdir -p "$LOG_DIR"

log() {
    printf '%s  %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >> "$LOG"
}

rotate_log() {
    [ -f "$LOG" ] || return 0
    local size i
    size=$(stat -f%z "$LOG" 2>/dev/null || echo 0)
    [ "$size" -lt "$MAX_LOG_BYTES" ] && return 0
    for (( i = LOG_KEEP - 1; i >= 1; i-- )); do
        [ -f "$LOG.$i" ] && mv -f "$LOG.$i" "$LOG.$((i + 1))"
    done
    mv -f "$LOG" "$LOG.1"
}

acquire_lock() {
    if mkdir "$LOCK" 2>/dev/null; then
        return 0
    fi
    local now mtime age
    now=$(date +%s)
    mtime=$(stat -f%m "$LOCK" 2>/dev/null || echo "$now")
    age=$(( now - mtime ))
    if [ "$age" -gt "$LOCK_STALE_SECONDS" ]; then
        log "WARN  락이 ${age}초째 잡혀 있다 — 죽은 것으로 보고 해제한다"
        rm -rf "$LOCK"
        mkdir "$LOCK" 2>/dev/null && return 0
    fi
    return 1
}


# **DNS가 준비될 때까지 기다린다** (2026-09-17 장애 대응).
# 이번 수집 실패의 실제 원인은 벤더가 아니라 DarkWake 직후 DNS가 아직 안 붙은 것이었다
# (NameResolutionError). 네트워크 변경 트리거는 Wi-Fi가 **끊길 때도** 발화하므로
# 그냥 실행하면 실패만 기록된다.
#
# 최대 30초까지 기다렸다가 그래도 안 되면 조용히 건너뛴다. 건너뛰면 이번 회차
# 수집 기록이 남지 않으므로 **다음 회차의 lookback이 자동으로 늘어 구멍을 메운다** —
# 실패로 기록하는 것보다 깨끗하다.
wait_for_dns() {
    local host="$1" i
    for i in 1 2 3 4 5 6; do
        /usr/bin/nslookup -timeout=3 "$host" >/dev/null 2>&1 && return 0
        sleep 5
    done
    return 1
}

rotate_log
log "───── 베이스라인 시작 (pid $$)  repo=$REPO"

if [ ! -x "$PYTHON" ]; then
    log "FAIL  venv python이 없다: $PYTHON"
    exit 1
fi
if [ ! -f "$REPO/.env" ]; then
    log "FAIL  .env가 없다 — DEEPSEEK_API_KEY 없이는 호출할 수 없다"
    exit 1
fi

FREE=$(df -g "$REPO" 2>/dev/null | tail -1 | awk '{print $4}')
if [ -n "${FREE:-}" ] && [ "$FREE" -lt "$DISK_ABORT_GB" ]; then
    log "ABORT 디스크 여유 ${FREE}GB — 쓰기가 깨질 수 있어 실행하지 않는다"
    exit 1
fi

if ! acquire_lock; then
    log "SKIP  이전 실행이 아직 돌고 있다"
    exit 0
fi
trap 'rm -rf "$LOCK"' EXIT

if ! wait_for_dns "api.deepseek.com"; then
    log "SKIP  DNS가 준비되지 않았다 (api.deepseek.com) — 이번 회차를 건너뛴다"
    log "      다음 회차의 lookback이 늘어 구멍을 메운다"
    exit 0
fi

cd "$REPO" || { log "FAIL  cd 실패: $REPO"; exit 1; }

# **caffeinate로 감싸는 이유** (2026-09-17 진단).
# launchd는 잠든 맥을 DarkWake로 깨워 작업을 발화시키지만, **DarkWake는 1분도 안 돼
# 다시 잠든다.** 그 사이 Wi-Fi가 끊겨 DNS가 죽고 벤더 호출이 전부 실패한다.
#
#   18:04:56  DarkWake (wifi)
#   18:05:00  수집 시작
#   18:05:41  Entering Sleep (Maintenance Sleep)   <- 41초 만에
#   18:11:28  수집 실패 (8종목 중 7종목 NameResolutionError)
#
# `caffeinate -im <명령>`은 그 명령이 **끝날 때까지만** 어서션을 잡는다.
#   -i  유휴 잠자기 방지    -m  디스크 잠자기 방지(SQLite 쓰기 중)
# 평소 전력에는 영향이 없다. 배터리에서는 시스템 잠자기(-s)를 막을 수 없으므로
# 뚜껑을 닫으면 여전히 취약하다 — 그건 상시 전원 기기로 옮겨야 풀린다.


/usr/bin/caffeinate -im "$PYTHON" scripts/run_baseline.py --weekly >> "$LOG" 2>&1
STATUS=$?

if [ "$STATUS" -eq 0 ]; then
    log "───── 완료 (성공)"
else
    log "───── 완료 (실패 status=$STATUS)"
    log "      위에 Permission denied가 있으면 TCC 문제다 (저장소 위치 확인)."
    log "      401/403이면 DEEPSEEK_API_KEY를, 429면 한도를 확인한다."
fi

exit "$STATUS"
