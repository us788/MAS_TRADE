#!/bin/bash
#
# 뉴스 수집 실행 래퍼 — launchd가 매시 이 스크립트를 부른다.
#
# cron이 아니라 launchd인 이유는 docs/journal/2026-09-16.md에 있다. 요약하면
# 노트북은 잠든다. cron은 잠든 사이의 실행을 그냥 건너뛰고 따라잡지 않는다.
# launchd의 StartCalendarInterval은 깨어날 때 밀린 것을 한 번 실행한다.
#
# 이 스크립트는 자기 위치에서 저장소 루트를 찾는다. 저장소를 옮겨도 깨지지 않는다.
# 옮긴 뒤에는 scripts/install_scheduler.sh를 다시 돌려 plist의 경로만 갱신하면 된다.
#
# 환경변수
#   SNAPSHOT_RETENTION_DAYS   설정하면 그보다 오래된 원본 스냅샷을 지운다.
#                             기본은 미설정 = 지우지 않는다. 파괴적 동작은 명시적으로만.

set -uo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON="$REPO/.venv/bin/python"
LOG_DIR="$REPO/logs"
LOG="$LOG_DIR/collect.log"
LOCK="$LOG_DIR/.collect.lock"

MAX_LOG_BYTES=$((10 * 1024 * 1024))   # 넘으면 회전
LOG_KEEP=3                            # collect.log.1 .. .3
DISK_WARN_GB=10
DISK_ABORT_GB=1
LOCK_STALE_SECONDS=1800               # 30분 넘게 잡혀 있으면 죽은 락으로 본다

mkdir -p "$LOG_DIR"

log() {
    printf '%s  %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >> "$LOG"
}

# 로그가 무한히 자라지 않게 한다. 수집은 매시 돌고 출력은 30줄씩 쌓인다.
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

# mkdir은 원자적이다. 앞 수집이 멈춰 있을 때 두 개가 같은 DB에 붙지 않게 한다.
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

free_gb() {
    df -g "$REPO" 2>/dev/null | tail -1 | awk '{print $4}'
}

rotate_log
log "───── 수집 시작 (pid $$)  repo=$REPO"

if [ ! -x "$PYTHON" ]; then
    log "FAIL  venv python이 없다: $PYTHON"
    log "      저장소를 옮겼다면 venv를 다시 만들어야 한다 (shebang에 절대경로가 박혀 있다):"
    log "      rm -rf .venv && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

if [ ! -f "$REPO/.env" ]; then
    log "FAIL  .env가 없다 — 키가 없으면 모든 수집이 실패한다 (gitignore 대상이라 clone에 안 딸려온다)"
    exit 1
fi

FREE=$(free_gb)
if [ -n "${FREE:-}" ]; then
    if [ "$FREE" -lt "$DISK_ABORT_GB" ]; then
        log "ABORT 디스크 여유 ${FREE}GB — 쓰기가 깨질 수 있어 수집하지 않는다"
        exit 1
    elif [ "$FREE" -lt "$DISK_WARN_GB" ]; then
        log "WARN  디스크 여유 ${FREE}GB — 정리가 필요하다"
    fi
fi

if ! acquire_lock; then
    log "SKIP  이전 수집이 아직 돌고 있다 — 이번 주기는 건너뛴다"
    exit 0
fi
trap 'rm -rf "$LOCK"' EXIT

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


/usr/bin/caffeinate -im "$PYTHON" scripts/collect_news.py >> "$LOG" 2>&1
STATUS=$?

if [ -n "${SNAPSHOT_RETENTION_DAYS:-}" ]; then
    PRUNED=$(find "$REPO/data/snapshots" -type f -mtime +"$SNAPSHOT_RETENTION_DAYS" -delete -print 2>/dev/null | wc -l | tr -d ' ')
    [ "${PRUNED:-0}" -gt 0 ] && log "스냅샷 ${PRUNED}개 삭제 (${SNAPSHOT_RETENTION_DAYS}일 경과)"
fi

SNAP=$(du -sh "$REPO/data/snapshots" 2>/dev/null | cut -f1)
FREE=$(free_gb)

if [ "$STATUS" -eq 0 ]; then
    log "───── 완료 (성공)  디스크 여유 ${FREE:-?}GB  스냅샷 ${SNAP:-?}"
else
    log "───── 완료 (실패 status=$STATUS)  디스크 여유 ${FREE:-?}GB"
    log "      위에 Permission denied가 있으면 TCC 문제다 — 저장소가 Desktop/Documents/Downloads"
    log "      아래에 있으면 launchd가 접근하지 못한다. 보호 폴더 밖으로 옮긴다."
fi

exit "$STATUS"
