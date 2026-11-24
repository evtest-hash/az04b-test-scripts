log() {
    local level="$1"; shift
    local msg="$*"
    local ts
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    echo "[$ts] [$$] [$level] $msg"
}

check_tick() {
    t1=$(hwclock -r -f /dev/rtc0 2>/dev/null)
    if [ -z "$t1" ]; then
        log "ERROR" "Failed to read RTC time"
        return 2
    fi

    sleep 2

    t2=$(hwclock -r -f /dev/rtc0 2>/dev/null)
    if [ -z "$t2" ]; then
        log "ERROR" "Failed to read RTC time"
        return 2
    fi
    log "INFO" "RTC time: $t1 -> $t2"

    t1_ts=$(date -d "$t1" +%s 2>/dev/null)
    t2_ts=$(date -d "$t2" +%s 2>/dev/null)
    if [ -z "$t1_ts" ] || [ -z "$t2_ts" ]; then
        log "ERROR" "Failed to convert time format, cannot compare"
        return 5
    fi

    diff_sec=$((t2_ts - t1_ts))
    if [ "$diff_sec" -ge 2 ] && [ "$diff_sec" -le 3 ]; then
        return 0
    fi
    log "WARN" "RTC advanced ${diff_sec}s over 2s"
    return 4
}

test_rtc() {
    log "INFO" "==> Checking RTC..."
    if [ ! -e "/dev/rtc0" ]; then
        log "ERROR" "RTC device /dev/rtc0 not found"
        return 1
    fi
    log "INFO" "Detected /dev/rtc0"

    log "INFO" "Setting RTC time to: $(date '+%Y-%m-%d %H:%M:%S')"
    if ! hwclock -w -f /dev/rtc0 2>/dev/null; then
        log "ERROR" "Failed to write RTC time"
        return 3
    fi

    t1=$(hwclock -r -f /dev/rtc0 2>/dev/null)
    if [ -z "$t1" ]; then
        log "ERROR" "Failed to read RTC time after write"
        return 2
    fi
    log "INFO" "RTC time after write: $t1"

    if ! check_tick; then
        log "WARN" "retry tick check once"
        if ! check_tick; then
            log "ERROR" "RTC time did not tick as expected, test failed"
            return 4
        fi
    fi

    log "INFO" "RTC time is ticking normally (about 2 seconds), test passed"
    return 0
}

test_rtc
