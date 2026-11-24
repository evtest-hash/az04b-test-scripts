#!/bin/bash

log() {
    local level="$1"; shift
    local msg="$*"
    local ts
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    echo "[$ts] [$$] [$level] $msg"
}

TEST_FILE="/test_data.bin"
DATA_SIZE=32M
BLOCK_SIZE=1M

trap 'rm -f "$TEST_FILE"' EXIT

log INFO "Writing ${DATA_SIZE} random data to $TEST_FILE and verifying..."
if ! fio --name=emmc --filename="$TEST_FILE" --size="$DATA_SIZE" --bs="$BLOCK_SIZE" \
        --rw=write --direct=1 --end_fsync=1 \
        --verify=crc32c --do_verify=1 --verify_fatal=1; then
    log ERROR "eMMC data write/verify failed"
    exit 1
fi

log INFO "eMMC data verification passed"
rm -f "$TEST_FILE"
log INFO "eMMC test completed"

exit 0
