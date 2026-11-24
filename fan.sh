log() {
    local level="$1"; shift
    local msg="$*"
    local ts
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    echo "[$ts] [$$] [$level] $msg"
}

test_fan() {
    log "INFO" "==> Checking fan..."
    local FAN_PATH_GLOB="/sys/devices/platform/pwm-fan/hwmon/hwmon*"
    local FAN_PWM="pwm1"
    local FAN_RPM="fan1_input"
    local status=0

    local FAN_PATH
    FAN_PATH=$(ls -d $FAN_PATH_GLOB 2>/dev/null | head -n1)
    if [ -z "$FAN_PATH" ]; then
        log "ERROR" "Fan control path not found"
        return 1
    fi

    set_fan_speed() {
        local speed=$1
        echo "$speed" > "$FAN_PATH/$FAN_PWM" 2>/dev/null
        if [ $? -eq 0 ]; then
            return 0
        else
            return 1
        fi
    }

    get_fan_speed() {
        if [ ! -f "$FAN_PATH/$FAN_RPM" ]; then
            echo "0"
            return 1
        fi
        local speed
        speed=$(cat "$FAN_PATH/$FAN_RPM" 2>/dev/null)
        echo "$speed"
        return 0
    }

    check_fan_speed() {
        local current_speed=$1
        local min_speed=$2
        local max_speed=$3
        if [ "$current_speed" -ge "$min_speed" ] && [ "$current_speed" -le "$max_speed" ]; then
            return 0
        else
            return 1
        fi
    }

    for ((i=0; i<50; i++)); do
        set_fan_speed 255
        sleep 0.1
    done

    current_speed=$(get_fan_speed)
    check_fan_speed "$current_speed" 1000 5000
    [ $? -ne 0 ] && status=1

    if [ $status -eq 0 ]; then
        log "INFO" "Fan test passed"
        set_fan_speed 0
        return 0
    else
        log "ERROR" "Fan test failed"
        set_fan_speed 0
        return 1
    fi
}

test_fan