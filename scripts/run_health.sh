#!/bin/bash
#
# 일일 점검 래퍼 — launchd가 매일 아침 이 스크립트를 부른다.
#
# **launchd의 가장 큰 약점은 조용히 죽는 것이다.** 로그를 안 보면 며칠 뒤에야 안다.
# 장 시작 전에 돌려서, 문제가 있으면 그날 장이 열리기 전에 손쓸 수 있게 한다.
# 한국 장중에 수집이 멈추면 그 구간은 1,000건 한도에 잘려 영구 손실이다.
#
# 이상이 있으면 macOS 알림을 띄운다 — 로그만 남기면 아무도 안 본다.

#
# **발화 조건이 둘이다** (2026-09-17).
#   1) 매일 08:30 KST — 노트북이 그 시각에 켜져 있으면
#   2) 네트워크 연결이 바뀔 때 — 뚜껑을 열어 Wi-Fi가 붙는 순간
#
# 노트북은 정해진 시각에 켜져 있지 않다. 시계만 믿으면 그날 점검을 통째로 거른다.
# 그래서 "열고 네트워크가 붙으면" 쪽을 주 트리거로 두고, 시계는 보조로 남긴다.
#
# 대신 **하루 한 번만 실제로 점검한다.** 네트워크 변경은 하루에도 수십 번 일어난다
# (Wi-Fi 재접속, VPN, 슬립 복귀). 스탬프 파일로 그날 이미 돌았는지 본다.
# `--force`를 주면 가드를 무시한다.

set -uo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON="$REPO/.venv/bin/python"
LOG="$REPO/logs/health.log"
STAMP="$REPO/logs/.health.lastrun"
MAX_LOG_BYTES=$((5 * 1024 * 1024))

mkdir -p "$REPO/logs"

# 하루 1회 가드. 날짜는 KST 기준이다 — 장이 도는 시간대가 기준이어야 한다.
TODAY="$(TZ=Asia/Seoul date '+%Y-%m-%d')"
if [ "${1:-}" != "--force" ] && [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$TODAY" ]; then
    exit 0
fi

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
STATUS=$?

# 점검이 끝났으면 오늘 몫은 했다. 결과가 나빠도 스탬프를 찍는다 —
# 안 찍으면 네트워크가 바뀔 때마다 같은 알림이 반복된다.
printf '%s' "$TODAY" > "$STAMP"

exit "$STATUS"
