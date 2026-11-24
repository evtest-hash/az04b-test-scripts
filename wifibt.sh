#!/bin/bash
LC_ALL=C

WIFI_SCAN_ATTEMPTS=3
WIFI_SCAN_TIMEOUT=15
WIFI_RETRY_DELAY=1
BLE_SCAN_SECONDS=5
BLE_SCAN_TIMEOUT=10

log() {
    local level="$1"; shift
    local msg="$*"
    local ts
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    echo "[$ts] [$$] [$level] $msg"
}

wifi_present() {
    ip link show dev wlan0 >/dev/null 2>&1 || ifconfig -a 2>/dev/null | grep -q "wlan0"
}

wifi_up() {
    ip link set dev wlan0 up >/dev/null 2>&1 || ifconfig wlan0 up >/dev/null 2>&1
}

wifi_down() {
    ip link set dev wlan0 down >/dev/null 2>&1 || ifconfig wlan0 down >/dev/null 2>&1
}

wifi_scan_once() {
    local out err rc
    err=$(mktemp)
    out=$(timeout "$WIFI_SCAN_TIMEOUT" iw dev wlan0 scan 2>"$err")
    rc=$?
    if [ "$rc" -ne 0 ]; then
        log WARN "WiFi scan command failed (rc=$rc): $(tr '\n' ' ' <"$err")"
        rm -f "$err"
        return 1
    fi
    rm -f "$err"
    printf '%s\n' "$out" | sed -n 's/^[[:space:]]*SSID: //p' | grep -v '^$' | sort -u
}

wifi_scan() {
    local attempt=1 ssids count
    while [ "$attempt" -le "$WIFI_SCAN_ATTEMPTS" ]; do
        wifi_up
        ssids=$(wifi_scan_once) || ssids=""
        count=$(printf '%s\n' "$ssids" | grep -c .)
        if [ "$count" -gt 0 ]; then
            log INFO "Found $count WiFi hotspots"
            log INFO "WiFi Hotspot List:"
            printf '%s\n' "$ssids" | while read -r essid; do
                [ -n "$essid" ] && log INFO "  - $essid"
            done
            return 0
        fi
        log WARN "WiFi scan attempt $attempt/$WIFI_SCAN_ATTEMPTS found nothing"
        attempt=$((attempt + 1))
        [ "$attempt" -le "$WIFI_SCAN_ATTEMPTS" ] || break
        wifi_down
        sleep "$WIFI_RETRY_DELAY"
        wifi_up
        sleep "$WIFI_RETRY_DELAY"
    done
    log ERROR "No WiFi hotspots found after $WIFI_SCAN_ATTEMPTS attempts"
    return 1
}

bt_present() {
    [ -e /sys/class/bluetooth/hci0 ] && command -v bluetoothctl >/dev/null 2>&1
}

ble_scan() {
    local f rc devices count
    f=$(mktemp)
    timeout "$BLE_SCAN_TIMEOUT" bluetoothctl --timeout "$BLE_SCAN_SECONDS" scan le >"$f" 2>&1
    rc=$?
    if [ "$rc" -ne 0 ]; then
        log ERROR "Bluetooth LE scan command failed (rc=$rc): $(tr '\n' ' ' <"$f")"
        rm -f "$f"
        return 1
    fi
    devices=$(sed 's/\x1b\[[0-9;]*m//g' "$f" | grep -o 'Device [0-9A-F:]\{17\}' | sort -u)
    rm -f "$f"
    count=$(printf '%s\n' "$devices" | grep -c .)
    if [ "$count" -eq 0 ]; then
        log ERROR "No Bluetooth LE devices found"
        return 1
    fi
    log INFO "Found $count Bluetooth LE devices"
    log INFO "Bluetooth LE Device List:"
    printf '%s\n' "$devices" | while read -r dev; do
        [ -n "$dev" ] && log INFO "  - $dev"
    done
    return 0
}

main() {
    local rc=0
    log INFO "==> Checking WiFi and Bluetooth..."

    if ! wifi_present; then
        log ERROR "WiFi device wlan0 not detected"
        rc=1
    else
        wifi_scan || rc=1
    fi

    if ! bt_present; then
        log ERROR "Bluetooth device hci0 or bluetoothctl not detected"
        rc=1
    else
        bluetoothctl power on >/dev/null 2>&1 || true
        ble_scan || rc=1
    fi

    if [ "$rc" -eq 0 ]; then
        log INFO "WiFi and Bluetooth check completed, devices are working"
    else
        log ERROR "WiFi and Bluetooth check FAILED"
    fi
    return "$rc"
}

main
exit $?
