#!/bin/bash
#
# 일일 점검 래퍼 — launchd가 매일 아침 이 스크립트를 부른다.
#
# **launchd의 가장 큰 약점은 조용히 죽는 것이다.** 로그를 안 보면 며칠 뒤에야 안다.
# 장 시작 전에 돌려서, 문제가 있으면 그날 장이 열리기 전에 손쓸 수 있게 한다.
# 한국 장중에 수집이 멈추면 그 구간은 1,000건 한도에 잘려 영구 손실이다.
#
# 이상이 있으면 macOS 알림을 띄운다 — 로그만 남기면 아무도 안 본다.

set -uo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON="$REPO/.venv/bin/python"
LOG="$REPO/logs/health.log"
MAX_LOG_BYTES=$((5 * 1024 * 1024))

mkdir -p "$REPO/logs"

if [ -f "$LOG" ] && [ "$(stat -f%z "$LOG" 2>/dev/null || echo 0)" -ge "$MAX_LOG_BYTES" ]; then
    mv -f "$LOG" "$LOG.1"
fi

{
    printf '\n===== %s =====\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
} >> "$LOG"

if [ ! -x "$PYTHON" ]; then
    echo "FAIL  venv python이 없다: $PYTHON" >> "$LOG"
    /usr/bin/osascript -e 'display notification "venv가 깨졌습니다" with title "MAS_TRADE 점검: 심각"' 2>/dev/null
    exit 2
fi

cd "$REPO" || exit 1

# 점검 자체는 빠르지만 DB를 읽는 동안 디스크가 잠들면 곤란하다.
/usr/bin/caffeinate -im "$PYTHON" scripts/health_check.py --notify >> "$LOG" 2>&1
exit $?
