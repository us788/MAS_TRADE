#!/bin/bash
#
# launchd 스케줄러 등록 — 매시 5분에 scripts/run_collect.sh를 돌린다.
#
# 저장소를 옮긴 뒤에는 이 스크립트를 다시 돌리기만 하면 된다. plist에 박힌
# 경로를 지금 위치로 다시 써서 재등록한다.
#
#   scripts/install_scheduler.sh              등록 (또는 재등록)
#   scripts/install_scheduler.sh --status     현재 상태
#   scripts/install_scheduler.sh --run-now    즉시 1회 실행 (검증용)
#   scripts/install_scheduler.sh --uninstall  해제
#
# cron이 아니라 launchd를 쓰는 이유는 docs/journal/2026-09-16.md.

set -uo pipefail

LABEL="com.ys.mastrade.collect"
REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
TARGET="$DOMAIN/$LABEL"

xml_escape() {
    printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

do_uninstall() {
    launchctl bootout "$TARGET" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null
    rm -f "$PLIST"
    echo "해제했다: $LABEL"
    echo "  plist 삭제: $PLIST"
    echo "  수집은 이제 수동으로만 돈다."
}

do_status() {
    echo "라벨   $LABEL"
    echo "plist  $PLIST"
    if [ ! -f "$PLIST" ]; then
        echo "상태   미설치"
        return 1
    fi
    echo "경로   $(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:1' "$PLIST" 2>/dev/null)"
    if launchctl print "$TARGET" >/dev/null 2>&1; then
        echo "상태   등록됨"
        launchctl print "$TARGET" 2>/dev/null \
            | grep -E "state =|last exit code =|runs =" \
            | sed 's/^[[:space:]]*/       /'
    else
        echo "상태   plist는 있으나 launchd에 등록되지 않았다 — 이 스크립트를 인자 없이 다시 돌린다"
        return 1
    fi
}

case "${1:-}" in
    --uninstall) do_uninstall; exit 0 ;;
    --status)    do_status;    exit $? ;;
    --run-now)
        echo "즉시 1회 실행한다 (launchd를 통해 — 권한 조건이 실제 예약 실행과 같다)"
        launchctl kickstart -p "$TARGET" || {
            echo "실패 — 먼저 인자 없이 돌려 등록한다" >&2; exit 1; }
        exit 0 ;;
    "") ;;
    *) echo "알 수 없는 인자: $1" >&2; exit 2 ;;
esac

# ---- 등록 ----

WRAPPER="$REPO/scripts/run_collect.sh"
if [ ! -f "$WRAPPER" ]; then
    echo "래퍼가 없다: $WRAPPER" >&2
    exit 1
fi
chmod +x "$WRAPPER"

mkdir -p "$HOME/Library/LaunchAgents" "$REPO/logs"

REPO_XML="$(xml_escape "$REPO")"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$REPO_XML/scripts/run_collect.sh</string>
    </array>

    <!-- 매시 5분. 자고 있었으면 깨어날 때 한 번 실행된다(밀린 건 하나로 합쳐진다).
         cron에는 없는 동작이고, 노트북에서 launchd를 고른 이유가 이것이다. -->
    <key>StartCalendarInterval</key>
    <dict>
        <key>Minute</key>
        <integer>5</integer>
    </dict>

    <!-- 로그인/재부팅 직후에도 한 번 돈다. 어떤 종목을 실제로 돌릴지는
         collect_news.py가 마지막 수집 시각을 보고 정하므로 중복 호출이 되지 않는다. -->
    <key>RunAtLoad</key>
    <true/>

    <!-- 래퍼가 자체 로그를 쓴다. 여기 찍히는 건 래퍼가 시작조차 못 했을 때다. -->
    <key>StandardOutPath</key>
    <string>$REPO_XML/logs/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$REPO_XML/logs/launchd.err.log</string>

    <key>WorkingDirectory</key>
    <string>$REPO_XML</string>

    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
PLIST_EOF

if ! plutil -lint "$PLIST" >/dev/null; then
    echo "plist가 올바르지 않다: $PLIST" >&2
    exit 1
fi

launchctl bootout "$TARGET" 2>/dev/null
if ! launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null; then
    launchctl load -w "$PLIST" 2>/dev/null || {
        echo "등록 실패 — launchctl bootstrap/load 모두 거부됐다" >&2
        exit 1
    }
fi
launchctl enable "$TARGET" 2>/dev/null

echo "등록했다: $LABEL"
echo "  저장소   $REPO"
echo "  plist    $PLIST"
echo "  주기     매시 5분 (+ 로그인 시)"
echo "  로그     $REPO/logs/collect.log"
echo
echo "검증"
echo "  scripts/install_scheduler.sh --run-now     지금 1회 돌려본다"
echo "  tail -f logs/collect.log                   결과를 본다"
echo "  scripts/install_scheduler.sh --status      등록 상태와 마지막 종료 코드"
echo
echo "logs/collect.log가 아예 안 생기면 launchd가 한 번도 실행하지 못한 것이고,"
echo "Permission denied가 찍히면 TCC 문제다 (저장소를 Desktop 밖으로 옮긴다)."
