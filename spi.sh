log() {
    local level="$1"; shift
    local msg="$*"
    local ts
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    echo "[$ts] [$$] [$level] $msg"
}

test_spi_flash() {
    MTD_DEV="/dev/mtdblock0"
    TEST_FILE="/tmp/mtd0_test_data.bin"
    READ_FILE="/tmp/mtd0_read_back.bin"
    TEST_SIZE=4096

    log "INFO" "==> SPI Flash (MTD0) R/W self-test"

    if [ ! -e "$MTD_DEV" ]; then
        log "ERROR" "Device $MTD_DEV does not exist!"
        return 1
    fi

    log "INFO" "Generating ${TEST_SIZE} bytes of random data..."
    dd if=/dev/urandom of="$TEST_FILE" bs=$TEST_SIZE count=1 2>/dev/null
    if [ $? -ne 0 ]; then
        log "ERROR" "Failed to generate test data!"
        rm -f "$TEST_FILE" "$READ_FILE"
        return 2
    fi


    log "INFO" "Writing test data to $MTD_DEV ..."
    dd if="$TEST_FILE" of="$MTD_DEV" bs=$TEST_SIZE count=1 2>/dev/null
    if [ $? -ne 0 ]; then
        log "ERROR" "Write failed!"
        rm -f "$TEST_FILE" "$READ_FILE"
        return 3
    fi
    sync

    log "INFO" "Reading back ${TEST_SIZE} bytes from $MTD_DEV..."
    dd if="$MTD_DEV" of="$READ_FILE" bs=$TEST_SIZE count=1 2>/dev/null
    if [ $? -ne 0 ]; then
        log "ERROR" "Read failed!"
        rm -f "$TEST_FILE" "$READ_FILE"
        return 4
    fi

    log "INFO" "Comparing written and read content..."
    if cmp -s "$TEST_FILE" "$READ_FILE"; then
        log "INFO" "Data matches, test passed"
        rc=0
    else
        log "ERROR" "Data mismatch, test failed"
        rc=5
    fi

    rm -f "$TEST_FILE" "$READ_FILE"
    return $rc
}

test_spi_flash
