#!/bin/bash

log() {
    local level="$1"; shift
    local msg="$*"
    local ts
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    echo "[$ts] [$$] [$level] $msg"
}

OUTPUT_GPIOS=(104 102 103 128)
INPUT_GPIOS=(118 56 57 59 58 112 111 38 136 135 134 138 139)

setup_gpio() {
    for gpio in "${OUTPUT_GPIOS[@]}" "${INPUT_GPIOS[@]}"; do
        if [ ! -d "/sys/class/gpio/gpio${gpio}" ]; then
            echo "${gpio}" > /sys/class/gpio/export 2>/dev/null
            sleep 0.05
            log "INFO" "Exported GPIO${gpio}"
        fi
    done
    for gpio in "${OUTPUT_GPIOS[@]}"; do
        echo out > "/sys/class/gpio/gpio${gpio}/direction" 2>/dev/null
        log "INFO" "Set GPIO${gpio} direction to out"
    done
    for gpio in "${INPUT_GPIOS[@]}"; do
        echo in > "/sys/class/gpio/gpio${gpio}/direction" 2>/dev/null
        log "INFO" "Set GPIO${gpio} direction to in"
    done
}

cleanup_gpio() {
    for gpio in "${OUTPUT_GPIOS[@]}" "${INPUT_GPIOS[@]}"; do
        if [ -d "/sys/class/gpio/gpio${gpio}" ]; then
            echo "${gpio}" > /sys/class/gpio/unexport 2>/dev/null
            log "INFO" "Unexported GPIO${gpio}"
        fi
    done
}

gpio_test_cycle() {
    local state=$1
    local fail_gpios=()
    for gpio in "${OUTPUT_GPIOS[@]}"; do
        echo "${state}" > "/sys/class/gpio/gpio${gpio}/value" 2>/dev/null
        log "INFO" "Set GPIO${gpio} output value to ${state}"
    done
    sleep 0.5
    for gpio in "${INPUT_GPIOS[@]}"; do
        if [ ! -e "/sys/class/gpio/gpio${gpio}/value" ]; then
            log "WARN" "GPIO in ${gpio}: not exported or unavailable"
            fail_gpios+=("${gpio}")
            continue
        fi
        val=$(cat "/sys/class/gpio/gpio${gpio}/value" 2>/dev/null)
        if [ "$val" != "$state" ]; then
            log "ERROR" "GPIO in ${gpio}: expected ${state}, actual ${val}"
            fail_gpios+=("${gpio}")
        else
            log "INFO" "GPIO in ${gpio}: expected ${state}, actual ${val}"
        fi
    done
    if [ ${#fail_gpios[@]} -ne 0 ]; then
        log "ERROR" "Failed GPIOs: ${fail_gpios[*]}"
        return 1
    fi
    return 0
}

gpio_test_gpio30_change() {
    local gpio=30
    local value_old value_new
    local changed=0
    local duration=3
    local interval=0.05
    local count=$((duration * 20))

    if [ ! -d "/sys/class/gpio/gpio${gpio}" ]; then
        echo "${gpio}" > /sys/class/gpio/export 2>/dev/null
        sleep 0.05
        echo in > "/sys/class/gpio/gpio${gpio}/direction" 2>/dev/null
        log "INFO" "Exported and set GPIO${gpio} direction to in"
    fi

    if [ ! -e "/sys/class/gpio/gpio${gpio}/value" ]; then
        log "WARN" "GPIO30 not exported or unavailable, skip change detection"
        return 1
    fi

    value_old=$(cat "/sys/class/gpio/gpio${gpio}/value" 2>/dev/null)
    for ((i=0; i<$count; i++)); do
        value_new=$(cat "/sys/class/gpio/gpio${gpio}/value" 2>/dev/null)
        if [ "$value_new" != "$value_old" ]; then
            changed=1
            break
        fi
        sleep $interval
    done

    if [ $changed -eq 1 ]; then
        log "INFO" "GPIO30 input level changed within 3 seconds"
        return 0
    else
        log "ERROR" "GPIO30 input level did not change within 3 seconds"
        return 1
    fi
}

test_uart_gpio_connectivity() {
    local pairs=(
        "46 47"
        "62 63"
    )
    local export_path="/sys/class/gpio/export"
    local unexport_path="/sys/class/gpio/unexport"
    local all_pass=0

    log "INFO" "==> UART GPIO connectivity test..."

    # 导出 GPIO 并设置方向
    for pair in "${pairs[@]}"; do
        set -- $pair
        echo $1 > $export_path 2>/dev/null
        echo $2 > $export_path 2>/dev/null
        echo out > /sys/class/gpio/gpio$1/direction 2>/dev/null
        echo in  > /sys/class/gpio/gpio$2/direction 2>/dev/null
        log "INFO" "Exported GPIO$1 (out), GPIO$2 (in)"
    done

    # 测试每对 GPIO
    for pair in "${pairs[@]}"; do
        set -- $pair
        local out_gpio=$1
        local in_gpio=$2
        log "INFO" "Testing GPIO${out_gpio} -> GPIO${in_gpio}"

        # 输出高电平
        echo 1 > /sys/class/gpio/gpio${out_gpio}/value 2>/dev/null
        sleep 0.1
        local val_high=$(cat /sys/class/gpio/gpio${in_gpio}/value 2>/dev/null)
        if [ "$val_high" = "1" ]; then
            log "INFO" "GPIO${in_gpio} read value (should be 1): $val_high"
        else
            log "ERROR" "GPIO${in_gpio} read value (should be 1): $val_high"
            all_pass=1
        fi

        # 输出低电平
        echo 0 > /sys/class/gpio/gpio${out_gpio}/value 2>/dev/null
        sleep 0.1
        local val_low=$(cat /sys/class/gpio/gpio${in_gpio}/value 2>/dev/null)
        if [ "$val_low" = "0" ]; then
            log "INFO" "GPIO${in_gpio} read value (should be 0): $val_low"
        else
            log "ERROR" "GPIO${in_gpio} read value (should be 0): $val_low"
            all_pass=1
        fi
    done

    # 清理
    for pair in "${pairs[@]}"; do
        set -- $pair
        echo $1 > $unexport_path 2>/dev/null
        echo $2 > $unexport_path 2>/dev/null
        log "INFO" "Unexported GPIO$1 and GPIO$2"
    done

    if [ $all_pass -eq 0 ]; then
        log "INFO" "UART GPIO connectivity test PASSED"
    else
        log "ERROR" "UART GPIO connectivity test FAILED"
    fi
    return $all_pass
}

test_gpio() {
    log "INFO" "==> GPIO signal test..."
    setup_gpio
    local all_pass=0

    for i in $(seq 1 3); do
        log "INFO" "Cycle ${i}/3: set output HIGH"
        gpio_test_cycle 1 || all_pass=1
        log "INFO" "Cycle ${i}/3: set output LOW"
        gpio_test_cycle 0 || all_pass=1
        log "INFO" ""
    done

    log "INFO" "Detecting GPIO30 input level change within 3 seconds..."
    gpio_test_gpio30_change || all_pass=1

    log "INFO" "Running UART GPIO connectivity test..."
    test_uart_gpio_connectivity || all_pass=1

    cleanup_gpio
    if [ $all_pass -eq 0 ]; then
        log "INFO" "GPIO test PASSED"
    else
        log "ERROR" "GPIO test FAILED"
    fi
    return $all_pass
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    test_gpio
fi
