#!/bin/sh

log() {
    local level="$1"; shift
    local msg="$*"
    local ts
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    echo "[$ts] [$$] [$level] $msg"
}

TARGET_VID="1d6b"
TARGET_PID="0104"
TYPEC_PORT="/sys/class/typec/port0"
ORIENTATION_CHANGE=0
TEST_RESULT=0
USB3_DETECTED=0

# Helper function: wait for device to appear, timeout after 10 seconds
wait_for_device_insert() {
    local timeout_count=20  # 0.5s * 20 = 10s
    local count=0
    while [ $count -lt $timeout_count ]; do
        if lsusb | grep -q "$TARGET_VID:$TARGET_PID"; then
            return 0
        fi
        sleep 0.5
        count=$((count+1))
    done
    return 1
}

# Helper function: wait for device to disappear, timeout after 10 seconds
wait_for_device_remove() {
    local timeout_count=20  # 0.5s * 20 = 10s
    local count=0
    while [ $count -lt $timeout_count ]; do
        if ! lsusb | grep -q "$TARGET_VID:$TARGET_PID"; then
            return 0
        fi
        sleep 0.5
        count=$((count+1))
    done
    return 1
}

# Helper function: check if device is USB 3.0
check_usb3_device() {
    # Method 1: Check USB version using lsusb -v
    local device_info=$(lsusb -d "$TARGET_VID:$TARGET_PID" -v 2>/dev/null | grep -E "bcdUSB|USB Version")
    if echo "$device_info" | grep -q "3\." || echo "$device_info" | grep -q "30\."; then
        return 0
    fi
    
    # Method 2: Check device speed using lsusb -t
    local speed_info=$(lsusb -t 2>/dev/null | grep -B5 -A5 "$TARGET_VID:$TARGET_PID" | grep -E "5000M|10000M|20000M")
    if [ -n "$speed_info" ]; then
        return 0
    fi
    
    # Method 3: Check in /sys/bus/usb/devices for SuperSpeed
    for device_path in /sys/bus/usb/devices/*/; do
        if [ -f "$device_path/idVendor" ] && [ -f "$device_path/idProduct" ]; then
            local vid=$(cat "$device_path/idVendor" 2>/dev/null)
            local pid=$(cat "$device_path/idProduct" 2>/dev/null)
            if [ "$vid" = "$TARGET_VID" ] && [ "$pid" = "$TARGET_PID" ]; then
                local speed=$(cat "$device_path/speed" 2>/dev/null)
                if [ "$speed" = "5000" ] || [ "$speed" = "10000" ] || [ "$speed" = "20000" ]; then
                    return 0
                fi
            fi
        fi
    done
    
    return 1
}

# Check if Type-C port exists
if [ ! -d "$TYPEC_PORT" ]; then
    log "ERROR" "Type-C port not found!"
    log "ERROR" "Please ensure Type-C support is enabled in the system"
    exit 1
fi

log "INFO" "Starting Type-C port test..."
log "INFO" "Please insert the Type-C device (first orientation)"

# Wait for device insertion (detect target device via lsusb, timeout 10s)
if ! wait_for_device_insert; then
    log "ERROR" "Target device NOT detected within 10 seconds: $TARGET_VID:$TARGET_PID (first orientation)"
    exit 1
else
    # Get orientation on first insertion
    ORIENTATION1=$(cat $TYPEC_PORT/orientation 2>/dev/null)
    log "INFO" "First insertion orientation: $ORIENTATION1"

    # Check device on first insertion
    if lsusb | grep -q "$TARGET_VID:$TARGET_PID"; then
        log "INFO" "Target device detected: $TARGET_VID:$TARGET_PID (first orientation)"
        
        # Check if device is USB 3.0
        if check_usb3_device; then
            log "INFO" "USB 3.0 device detected (first orientation)"
            USB3_DETECTED=1
        else
            log "ERROR" "Device is not USB 3.0 (first orientation)"
            TEST_RESULT=1
        fi
    else
        log "ERROR" "Target device NOT detected: $TARGET_VID:$TARGET_PID (first orientation)"
        TEST_RESULT=1
    fi
fi

log "INFO" "Please remove the device, then re-insert with reversed orientation in 10 seconds ..."

# Wait for device removal (detect disappearance via lsusb, timeout 10s)
if ! wait_for_device_remove; then
    log "ERROR" "Device was not removed within 10 seconds!"
    exit 1
fi

log "INFO" "Device removed. Please insert the device in 10 seconds (second orientation)"

# Wait for device re-insertion (detect reappearance via lsusb, timeout 10s)
if ! wait_for_device_insert; then
    log "ERROR" "Target device NOT detected within 10 seconds: $TARGET_VID:$TARGET_PID (second orientation)"
    exit 1
else
    # Get orientation on second insertion
    ORIENTATION2=$(cat $TYPEC_PORT/orientation 2>/dev/null)
    log "INFO" "Second insertion orientation: $ORIENTATION2"
fi

# Check orientation change (only if both orientations are available)
if [ -n "$ORIENTATION1" ] && [ -n "$ORIENTATION2" ] && [ "$ORIENTATION1" != "N/A" ] && [ "$ORIENTATION2" != "N/A" ]; then
    if [ "$ORIENTATION1" != "$ORIENTATION2" ]; then
        log "INFO" "Orientation change detected: $ORIENTATION1 → $ORIENTATION2"
        ORIENTATION_CHANGE=1
    else
        log "ERROR" "Orientation did not change"
        TEST_RESULT=1
    fi
else
    log "ERROR" "Could not determine orientation change due to missing orientation data"
    TEST_RESULT=1
fi

# Check device on second insertion (only if device is present)
if lsusb | grep -q "$TARGET_VID:$TARGET_PID"; then
    log "INFO" "Target device detected: $TARGET_VID:$TARGET_PID (second orientation)"
    
    # Check if device is still USB 3.0 on second insertion
    if check_usb3_device; then
        log "INFO" "USB 3.0 device detected (second orientation)"
        if [ $USB3_DETECTED -eq 0 ]; then
            USB3_DETECTED=1
        fi
    else
        log "ERROR" "Device is not USB 3.0 (second orientation)"
        TEST_RESULT=1
    fi
else
    log "ERROR" "Target device NOT detected: $TARGET_VID:$TARGET_PID (second orientation)"
    TEST_RESULT=1
fi

# Output final result
log "INFO" "Test Summary:"
log "INFO" "First orientation: $ORIENTATION1"
log "INFO" "Second orientation: $ORIENTATION2"
log "INFO" "Orientation changed: $( [ $ORIENTATION_CHANGE -eq 1 ] && echo 'Yes' || echo 'No' )"
log "INFO" "USB 3.0 detected: $( [ $USB3_DETECTED -eq 1 ] && echo 'Yes' || echo 'No' )"
log "INFO" "Device detection: $( [ $TEST_RESULT -eq 0 ] && echo 'Success' || echo 'Failure' )"

if [ $ORIENTATION_CHANGE -eq 1 ] && [ $TEST_RESULT -eq 0 ] && [ $USB3_DETECTED -eq 1 ]; then
    log "INFO" "All tests passed!"
    exit 0
else
    log "ERROR" "Test failed!"
    exit 1
fi