#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ram.py — Production RAM test.

Allocates a large region, fills it with fixed data patterns, then reads it back
and verifies.

OOM-safe by *proactive gating*, not by trusting mlock/mmap to fail gracefully:
under a no-swap kernel, faulting in a big region drives the page allocator into
the OOM killer (it kills the process mid-fault instead of returning ENOMEM), so
a "lock then back off on ENOMEM" scheme never gets the ENOMEM and dies. Instead
we reserve address space (private anon), then commit it page-by-page in small
chunks, re-reading MemAvailable before each chunk and stopping while a safe
reserve of free memory still remains. mlock happens only after pages are already
resident, so it can no longer allocate — and thus can no longer OOM.

Patterns (each is a uniform byte, expanded across the whole region):
  Solid Ones    0xFF  catches stuck-at-0 / data lines shorted to ground
  Solid Zeroes  0x00  catches stuck-at-1 / data lines shorted to power
  Checkerboard  0xAA  catches adjacent-bit shorts and pattern sensitivity
  Inverse       0x55  complementary check of the above

Exit code: 0 = pass, 1 = fail.
"""

import ctypes
import ctypes.util
import datetime
import mmap
import os
import sys

_MB = 1024 ** 2

# (name, uniform byte value); 32-bit word label = byte * 0x01010101
PATTERNS = [
    ("Solid Ones",   0xFF),
    ("Solid Zeroes", 0x00),
    ("Checkerboard", 0xAA),
    ("Inverse",      0x55),
]

BLOCK_SIZE = _MB                 # read/write granularity
RESERVE_FLOOR = 192 * _MB        # aim to leave at least this free (target ceiling)
MIN_FREE = 96 * _MB              # hard stop: never let MemAvailable dip below this
COMMIT_STEP = 8 * _MB            # commit/gating granularity while faulting pages in
MIN_TEST_SIZE = 16 * _MB         # don't bother testing below this
MEMINFO_FALLBACK = 4 * 1024 ** 3  # if /proc/meminfo is unreadable

_SIZE_UNITS = {'B': 0, 'K': 10, 'M': 20, 'G': 30}


def log(level, msg):
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] [{os.getpid()}] [{level}] {msg}")


def get_available_memory():
    """Free memory in bytes (falls back to MEMINFO_FALLBACK on error)."""
    avail = MEMINFO_FALLBACK
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    avail = int(line.split()[1]) * 1024  # kB -> bytes
                    break
    except Exception as e:
        log("ERROR", f"Failed to get available memory: {e}")
    return avail


def compute_initial_target():
    """Ceiling to aim for: available memory minus the reserve we promise to leave
    free. The incremental commit + MIN_FREE gate enforce the real limit."""
    return max(get_available_memory() - RESERVE_FLOOR, MIN_TEST_SIZE)


def parse_size(arg):
    """Parse <size>[B|K|M|G]; a bare number is megabytes."""
    arg = arg.strip()
    suffix = arg[-1].upper()
    try:
        if suffix in _SIZE_UNITS:
            return int(arg[:-1]) << _SIZE_UNITS[suffix]
        return int(arg) << 20
    except ValueError:
        raise ValueError(f"Invalid size: {arg!r}")


# --- mlock / munlock via libc -----------------------------------------------
_libc = None


def _get_libc():
    global _libc
    if _libc is None:
        name = ctypes.util.find_library("c") or "libc.so.6"
        lib = ctypes.CDLL(name, use_errno=True)
        lib.mlock.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        lib.mlock.restype = ctypes.c_int
        lib.munlock.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        lib.munlock.restype = ctypes.c_int
        _libc = lib
    return _libc


def _buffer_address(mem):
    return ctypes.addressof(ctypes.c_char.from_buffer(mem))


def try_mlock(mem, size):
    """Return (locked, errno, msg). EPERM = no privilege; ENOMEM = out of memory."""
    try:
        if _get_libc().mlock(_buffer_address(mem), size) == 0:
            return True, None, None
        err = ctypes.get_errno()
        return False, err, os.strerror(err)
    except Exception as e:
        return False, None, str(e)


def do_munlock(mem, size):
    try:
        _get_libc().munlock(_buffer_address(mem), size)
    except Exception:
        pass


def commit_region(target):
    """
    Reserve `target` bytes of private anonymous address space, then commit them
    page-by-page in COMMIT_STEP chunks. Before each chunk, re-read MemAvailable
    and stop early if committing it would push free memory below MIN_FREE.

    This proactively avoids the OOM killer: memory is consumed here, gated by a
    live free-memory check, so we voluntarily stop with headroom to spare instead
    of racing the allocator into an OOM-kill. mmap only reserves address space
    (pages are faulted lazily), so it is the touch loop below — not mmap — that
    commits physical RAM.

    Returns (mem, committed) where committed may be < target if we stopped to
    protect the system; (None, 0) if even the reservation failed.
    """
    try:
        mem = mmap.mmap(-1, target, flags=mmap.MAP_PRIVATE)
    except (MemoryError, OverflowError, ValueError, OSError) as e:
        log("ERROR", f"Reserve {target // _MB}MB failed: {e}")
        return None, 0

    committed = 0
    while committed < target:
        step = min(COMMIT_STEP, target - committed)
        avail = get_available_memory()
        if avail - step < MIN_FREE:
            log("INFO", f"Stopping at {committed // _MB}MB to keep >= "
                        f"{MIN_FREE // _MB}MB free (MemAvailable={avail // _MB}MB)")
            break
        # Touch one byte per page to fault the chunk into physical RAM now,
        # while we still hold the headroom we just checked for.
        for off in range(committed, committed + step, mmap.PAGESIZE):
            mem[off] = 0
        committed += step

    return mem, committed


def test_fill(mem, size, byte_val, block_size=BLOCK_SIZE):
    """Fill the region with byte_val, read it back, compare. Returns (offset,
    got_byte) on the first mismatch, or None on success."""
    tile = bytes([byte_val]) * block_size

    for offset in range(0, size, block_size):
        end = offset + block_size
        if end <= size:
            mem[offset:end] = tile
        else:
            mem[offset:size] = tile[:size - offset]

    for offset in range(0, size, block_size):
        end = offset + block_size
        read_back = mem[offset:end]
        expected = tile if end <= size else tile[:size - offset]  # slice only the tail
        if read_back != expected:
            for i, (got, want) in enumerate(zip(read_back, expected)):
                if got != want:
                    return offset + i, got
            return offset, read_back[0] if read_back else None
    return None


def allocate_and_test_memory(target, block_size=BLOCK_SIZE, allow_backoff=True):
    """Acquire memory (proactively gated so it can't OOM), then run every
    pattern. `allow_backoff` is kept for CLI compatibility: both paths commit
    incrementally under a free-memory guard; with an explicit size we also warn
    if the request had to be capped to stay safe."""
    log("INFO", f"Target up to {target / _MB:.2f} MB "
                f"(commit incrementally, keep >= {MIN_FREE // _MB}MB free)")

    mem, size = commit_region(target)
    if mem is None or size < MIN_TEST_SIZE:
        log("ERROR", "Could not safely allocate the requested test memory")
        if mem is not None:
            mem.close()
        return False
    if not allow_backoff and size < target:
        log("WARN", f"Requested {target // _MB}MB capped to {size // _MB}MB "
                    "to avoid OOM")

    # Pages are already resident, so mlock can neither allocate nor OOM here;
    # it just pins them for the duration of the test.
    locked, err, msg = try_mlock(mem, size)
    if not locked:
        log("WARN", f"mlock skipped ({msg or 'failed'}); pages already resident, "
                    "continuing")
    log("INFO", f"Memory ready: {size / _MB:.2f} MB, "
                f"{'locked' if locked else 'unlocked'}")

    try:
        all_pass = True
        for name, byte_val in PATTERNS:
            word = byte_val * 0x01010101
            log("INFO", f"Testing pattern {name} (0x{word:08X})...")
            result = test_fill(mem, size, byte_val, block_size)
            if result is None:
                log("INFO", f"{name}: ok")
            else:
                offset, got = result
                log("ERROR",
                    f"FAILURE: 0x{got:02x} != 0x{byte_val:02x} "
                    f"at offset 0x{offset:08x} ({name})")
                all_pass = False
                # keep going so every failing pattern is reported; break to stop early

        if all_pass:
            log("INFO", "All pattern tests passed")
        return all_pass
    finally:
        if locked:
            do_munlock(mem, size)
        mem.close()


def main():
    if len(sys.argv) > 1:
        try:
            size = parse_size(sys.argv[1])
        except ValueError as e:
            log("ERROR", str(e))
            sys.exit(1)
        success = allocate_and_test_memory(size, allow_backoff=False)
    else:
        success = allocate_and_test_memory(compute_initial_target(),
                                           allow_backoff=True)

    if success:
        log("PASS", "Memory test succeeded")
    else:
        log("FAIL", "Memory test failed")
        sys.exit(1)


if __name__ == "__main__":
    main()