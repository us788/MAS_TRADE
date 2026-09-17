#!/bin/bash
#
# launchd 스케줄러 등록. 작업 두 개를 관리한다.
#
#   collect    매시 5분      뉴스 수집 (scripts/run_collect.sh)
#   baseline   매주 수 07:00  베이스라인 시그널 (scripts/run_baseline.sh)
#   health     매일 08:30    파이프라인 점검 (scripts/run_health.sh) — 이상 시 알림
#
# 저장소를 옮긴 뒤에는 이 스크립트를 다시 돌리기만 하면 된다. plist에 박힌 경로를
# 지금 위치로 다시 써서 재등록한다.
#
#   scripts/install_scheduler.sh                 둘 다 등록 (또는 재등록)
#   scripts/install_scheduler.sh --status        상태
#   scripts/install_scheduler.sh --run-now collect    즉시 1회 (검증용)
#   scripts/install_scheduler.sh --uninstall [job]    해제
#
# cron이 아니라 launchd를 쓰는 이유는 docs/journal/2026-09-16.md.
#
# **StartCalendarInterval은 시스템 현지 시간 기준이다.** 이 맥이 KST이므로
# Hour=7은 07:00 KST다. 맥의 시간대를 바꾸면 스케줄도 따라 움직인다.

set -uo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
DOMAIN="gui/$(id -u)"
JOBS=(collect baseline health)

# 작업별 정의 ------------------------------------------------------------
label_of()   { echo "com.ys.mastrade.$1"; }
script_of()  { case "$1" in collect) echo "run_collect.sh";; baseline) echo "run_baseline.sh";; health) echo "run_health.sh";; esac; }
runatload_of() {
    # 베이스라인은 돈이 나간다. 로그인마다 부르지 않는다.
    # 잠든 사이 밀린 실행은 StartCalendarInterval이 깨어날 때 한 번 돌려 주므로
    # RunAtLoad 없이도 따라잡기는 된다.
    # health는 깨어날 때 한 번 더 확인해 주는 편이 낫다 — 비용이 0이다.
    case "$1" in collect) echo "true";; baseline) echo "false";; health) echo "true";; esac
}
schedule_of() {
    case "$1" in
        collect)  printf '        <key>Minute</key>\n        <integer>5</integer>\n' ;;
        baseline) printf '        <key>Weekday</key>\n        <integer>3</integer>\n'
                  printf '        <key>Hour</key>\n        <integer>7</integer>\n'
                  printf '        <key>Minute</key>\n        <integer>0</integer>\n' ;;
        health)   printf '        <key>Hour</key>\n        <integer>8</integer>\n'
                  printf '        <key>Minute</key>\n        <integer>30</integer>\n' ;;
    esac
}
# 네트워크 연결이 바뀔 때도 발화시킬 것인가.
# **노트북은 정해진 시각에 켜져 있지 않다.** 시계만 믿으면 그날 몫을 통째로 거른다.
# 뚜껑을 열어 Wi-Fi가 붙는 순간을 주 트리거로 쓰고 시계는 보조로 남긴다.
# 수집(collect)은 매시 도는 것이 설계이므로 붙이지 않는다 — 네트워크는 하루에도
# 수십 번 바뀌고, 매시 주기가 이미 그 역할을 한다.
network_trigger_of() {
    case "$1" in collect) echo "false";; baseline) echo "true";; health) echo "true";; esac
}

describe_of() {
    case "$1" in
        collect)  echo "매시 5분 (+ 로그인 시)" ;;
        baseline) echo "매주 수요일 07:00 KST + 네트워크 연결 시 (주 1회만 실제 실행)" ;;
        health)   echo "매일 08:30 KST + 네트워크 연결 시 (하루 1회만 실제 점검)" ;;
    esac
}

xml_escape() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
plist_path() { echo "$HOME/Library/LaunchAgents/$(label_of "$1").plist"; }

valid_job() {
    local j
    for j in "${JOBS[@]}"; do [ "$j" = "$1" ] && return 0; done
    echo "알 수 없는 작업: $1 (${JOBS[*]})" >&2
    return 1
}

# ----------------------------------------------------------------------

do_uninstall() {
    local job label plist
    for job in "$@"; do
        label="$(label_of "$job")"; plist="$(plist_path "$job")"
        launchctl bootout "$DOMAIN/$label" 2>/dev/null || launchctl unload "$plist" 2>/dev/null
        rm -f "$plist"
        echo "해제: $label"
    done
}

do_status() {
    local job label plist rc=0
    for job in "${JOBS[@]}"; do
        label="$(label_of "$job")"; plist="$(plist_path "$job")"
        echo "── $job ($(describe_of "$job"))"
        echo "   라벨   $label"
        if [ ! -f "$plist" ]; then
            echo "   상태   미설치"; rc=1; continue
        fi
        echo "   경로   $(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:1' "$plist" 2>/dev/null)"
        if launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
            echo "   상태   등록됨"
            launchctl print "$DOMAIN/$label" 2>/dev/null \
                | grep -E "last exit code =|runs =" | sed 's/^[[:space:]]*/          /'
        else
            echo "   상태   plist는 있으나 launchd에 등록되지 않았다 — 인자 없이 다시 돌린다"
            rc=1
        fi
    done
    return $rc
}

install_job() {
    local job="$1" label plist wrapper repo_xml
    label="$(label_of "$job")"; plist="$(plist_path "$job")"
    wrapper="$REPO/scripts/$(script_of "$job")"
    if [ ! -f "$wrapper" ]; then
        echo "래퍼가 없다: $wrapper" >&2; return 1
    fi
    chmod +x "$wrapper"
    repo_xml="$(xml_escape "$REPO")"

    {
        cat <<PLIST_HEAD
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$label</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$repo_xml/scripts/$(script_of "$job")</string>
    </array>

    <!-- 자고 있었으면 깨어날 때 한 번 실행된다(밀린 건 하나로 합쳐진다).
         cron에는 없는 동작이고, 노트북에서 launchd를 고른 이유가 이것이다.
         시스템 현지 시간(KST) 기준이다. -->
    <key>StartCalendarInterval</key>
    <dict>
PLIST_HEAD
        schedule_of "$job"
        cat <<PLIST_TAIL
    </dict>

    <key>RunAtLoad</key>
    <$(runatload_of "$job")/>
$(if [ "$(network_trigger_of "$job")" = "true" ]; then cat <<'EVT'

    <!-- 네트워크 연결이 바뀔 때도 발화한다 (뚜껑을 열어 Wi-Fi가 붙는 순간).
         노트북은 정해진 시각에 켜져 있지 않으므로 시계만으로는 그날 몫을 거른다.
         Wi-Fi가 **끊길 때도** 발화하므로 래퍼가 DNS 준비를 확인하고,
         하루(주) 1회 가드가 중복 실행을 막는다. -->
    <key>LaunchEvents</key>
    <dict>
        <key>com.apple.notifyd.matching</key>
        <dict>
            <key>network-change</key>
            <dict>
                <key>Notification</key>
                <string>com.apple.system.config.network_change</string>
            </dict>
        </dict>
    </dict>
EVT
fi)

    <!-- 래퍼가 자체 로그를 쓴다. 여기 찍히는 건 래퍼가 시작조차 못 했을 때다. -->
    <key>StandardOutPath</key>
    <string>$repo_xml/logs/launchd.$job.out.log</string>
    <key>StandardErrorPath</key>
    <string>$repo_xml/logs/launchd.$job.err.log</string>

    <key>WorkingDirectory</key>
    <string>$repo_xml</string>

    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
PLIST_TAIL
    } > "$plist"

    if ! plutil -lint "$plist" >/dev/null; then
        echo "plist가 올바르지 않다: $plist" >&2; return 1
    fi
    launchctl bootout "$DOMAIN/$label" 2>/dev/null
    if ! launchctl bootstrap "$DOMAIN" "$plist" 2>/dev/null; then
        launchctl load -w "$plist" 2>/dev/null || {
            echo "등록 실패: $label" >&2; return 1; }
    fi
    launchctl enable "$DOMAIN/$label" 2>/dev/null
    echo "등록: $label  —  $(describe_of "$job")"
    return 0
}

case "${1:-}" in
    --uninstall)
        shift
        if [ $# -eq 0 ]; then do_uninstall "${JOBS[@]}"; else valid_job "$1" && do_uninstall "$1"; fi
        exit $? ;;
    --status) do_status; exit $? ;;
    --run-now)
        shift
        [ $# -eq 1 ] || { echo "사용법: --run-now {${JOBS[*]}}" >&2; exit 2; }
        valid_job "$1" || exit 2
        echo "즉시 1회 실행 (launchd 경유 — 권한 조건이 예약 실행과 같다)"
        launchctl kickstart -p "$DOMAIN/$(label_of "$1")" || {
            echo "실패 — 먼저 인자 없이 돌려 등록한다" >&2; exit 1; }
        exit 0 ;;
    "") ;;
    *) echo "알 수 없는 인자: $1" >&2; exit 2 ;;
esac

mkdir -p "$HOME/Library/LaunchAgents" "$REPO/logs"
fail=0
for job in "${JOBS[@]}"; do install_job "$job" || fail=1; done

echo
echo "저장소   $REPO"
echo "로그     $REPO/logs/collect.log · $REPO/logs/baseline.log"
echo
echo "검증"
echo "  scripts/install_scheduler.sh --status"
echo "  scripts/install_scheduler.sh --run-now collect"
echo "  tail -f logs/collect.log"
echo
echo "logs/*.log가 아예 안 생기면 launchd가 한 번도 실행하지 못한 것이고,"
echo "Permission denied가 찍히면 TCC 문제다 (저장소를 Desktop 밖으로 옮긴다)."
exit $fail
