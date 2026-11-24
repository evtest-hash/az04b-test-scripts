#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MEMINFO_RE = re.compile(r"^([A-Za-z0-9()_]+):\s+(\d+)\s+kB$", re.MULTILINE)
SAT_COMPLETED_RE = re.compile(
    r"Completed:\s+(?P<mb>[0-9.]+)M in (?P<seconds>[0-9.]+)s (?P<rate>[0-9.]+)MB/s, with "
    r"(?P<hardware_incidents>\d+) hardware incidents, (?P<errors>\d+) errors"
)
SAT_MEMORY_COPY_RE = re.compile(
    r"Memory Copy:\s+(?P<mb>[0-9.]+)M at (?P<rate>[0-9.]+)MB/s")
SAT_STATUS_RE = re.compile(r"Status:\s+(?P<status>PASS|FAIL)")

LPDDR_MANUFACTURERS = {
    0x01: "Samsung",
    0x05: "Nanya",
    0x06: "SK hynix",
    0x08: "Winbond",
    0x09: "ESMT",
    0x13: "CXMT",
    0x1A: "Xi'an UniIC",
    0x1C: "JSC",
    0xE5: "Dosilicon",
    0xF8: "Fidelix",
    0xF9: "Ultra Memory",
    0xFD: "AP Memory",
    0xFF: "Micron",
}

DEFAULT_SECONDS = 10


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def run_command(args: list[str], timeout: float | None = None) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            args,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "command": " ".join(args),
            "returncode": -1,
            "timed_out": True,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
        }
    except OSError as exc:
        return {
            "command": " ".join(args),
            "returncode": -1,
            "timed_out": False,
            "stdout": "",
            "stderr": str(exc),
        }
    return {
        "command": " ".join(args),
        "returncode": completed.returncode,
        "timed_out": False,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def parse_meminfo(text: str) -> dict[str, int]:
    return {match.group(1): int(match.group(2)) for match in MEMINFO_RE.finditer(text)}


def round_down_to_step(value: int, step: int) -> int:
    if value <= 0 or step <= 0:
        return 0
    return value - (value % step)


def calculate_ddr_speed_size_mb(meminfo: dict[str, int]) -> int:
    mem_total_mb = meminfo.get("MemTotal", 0) // 1024
    mem_available_mb = meminfo.get("MemAvailable", 0) // 1024
    size_mb = min(int(mem_total_mb * 0.25),
                  max(mem_available_mb - 512, 0), 1024)
    size_mb = round_down_to_step(size_mb, 128)
    return max(size_mb, 256)


def parse_manfid(text: str) -> int | None:
    value = str(text).strip().lower()
    if not value:
        return None
    try:
        return int(value, 16) if value.startswith("0x") else int(value, 10)
    except ValueError:
        return None


def parse_dram_id_registers(text: str) -> dict[str, str]:
    registers = {"mr5": "", "mr6": "", "mr7": ""}
    for key in registers:
        match = re.search(rf"{key.upper()}=(0x[0-9a-fA-F]+)", text)
        if match:
            registers[key] = match.group(1).lower()
    return registers


def describe_lpddr_manufacturer(dram_type: str, dram_id_text: str) -> dict[str, str]:
    registers = parse_dram_id_registers(dram_id_text)
    mr5_text = registers["mr5"]
    if not str(dram_type).strip().upper().startswith("LPDDR"):
        return {
            "dram_mr5": mr5_text,
            "dram_mr6": registers["mr6"],
            "dram_mr7": registers["mr7"],
            "dram_manufacturer_id": mr5_text,
            "dram_manufacturer_name": "unknown",
            "dram_manufacturer_confidence": "low",
        }

    mr5_value = parse_manfid(mr5_text)
    manufacturer_name = LPDDR_MANUFACTURERS.get(
        mr5_value, "unknown") if mr5_value is not None else "unknown"
    return {
        "dram_mr5": mr5_text,
        "dram_mr6": registers["mr6"],
        "dram_mr7": registers["mr7"],
        "dram_manufacturer_id": mr5_text,
        "dram_manufacturer_name": manufacturer_name,
        "dram_manufacturer_confidence": "high" if manufacturer_name != "unknown" else "low",
    }


def _first_int(text: str) -> int:
    match = re.search(r"(\d+)", text)
    return int(match.group(1)) if match else 0


def _new_channel(index: int) -> dict[str, Any]:
    return {
        "index": index,
        "cs_count": 0,
        "bus_width_bits": 0,
        "column": 0,
        "bank": 0,
        "cs0_row": 0,
        "cs1_row": 0,
        "die_bus_width_bits": 0,
        "total_size_mb": 0,
    }


def parse_dmcinfo(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "dram_type": "",
        "dram_id": "",
        "channels": [],
        "channel_count": 0,
        "total_size_mb": 0,
        "total_bus_width_bits": 0,
        "devfreq_state": "",
        "devfreq_enabled": False,
        "governor": "",
        "cur_freq_hz": 0,
        "dram_freq_hz": 0,
        "reported_freq_hz": 0,
    }
    current_channel: dict[str, Any] | None = None
    in_capacity_section = False
    channel_field_map = {
        "CS Count": "cs_count",
        "Bus Width": "bus_width_bits",
        "Column": "column",
        "Bank": "bank",
        "CS0_Row": "cs0_row",
        "CS1_Row": "cs1_row",
        "DieBusWidth": "die_bus_width_bits",
        "TotalSize": "total_size_mb",
    }

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("DramType:"):
            result["dram_type"] = line.split(":", 1)[1].strip()
            continue
        if line.startswith("Dram ID:"):
            dram_id = line.split(":", 1)[1].strip()
            result["dram_id"] = "" if dram_id == "None" else dram_id
            continue
        if line == "DramCapacity:":
            in_capacity_section = True
            current_channel = None
            continue
        if line.startswith("Channel ["):
            match = re.search(r"Channel \[(\d+)\]", line)
            current_channel = _new_channel(
                int(match.group(1)) if match else len(result["channels"]))
            result["channels"].append(current_channel)
            continue
        if line.startswith("devfreq/dmc:"):
            in_capacity_section = False
            current_channel = None
            state = line.split(":", 1)[1].strip()
            result["devfreq_state"] = state
            result["devfreq_enabled"] = state.lower() == "enable"
            continue
        if line.startswith("governor:"):
            current_channel = None
            result["governor"] = line.split(":", 1)[1].strip()
            continue
        if line.startswith("cur_freq:"):
            current_channel = None
            result["cur_freq_hz"] = _first_int(line.split(":", 1)[1].strip())
            result["reported_freq_hz"] = result["cur_freq_hz"]
            continue
        if line.startswith("DramFreq:"):
            current_channel = None
            result["dram_freq_hz"] = _first_int(line.split(":", 1)[1].strip())
            result["reported_freq_hz"] = result["dram_freq_hz"]
            continue
        if line.startswith("NOTE:"):
            current_channel = None
            continue
        if not in_capacity_section or ":" not in line:
            continue

        key, value = (part.strip() for part in line.split(":", 1))
        field_name = channel_field_map.get(key)
        if not field_name:
            continue
        if current_channel is None:
            current_channel = _new_channel(len(result["channels"]))
            result["channels"].append(current_channel)
        current_channel[field_name] = _first_int(value)

    result["channel_count"] = len(result["channels"])
    result["total_size_mb"] = sum(channel["total_size_mb"]
                                  for channel in result["channels"])
    result["total_bus_width_bits"] = sum(
        channel["bus_width_bits"] for channel in result["channels"])
    return result


def parse_stressapptest_output(text: str) -> dict[str, Any]:
    clean = text.replace("\r", "\n")
    completed_match = SAT_COMPLETED_RE.search(clean)
    memory_copy_match = SAT_MEMORY_COPY_RE.search(clean)
    status_match = SAT_STATUS_RE.search(clean)
    summary_lines = [
        line.strip()
        for line in clean.splitlines()
        if "Completed:" in line or "Memory Copy:" in line or "Status:" in line
    ]
    if not completed_match and not memory_copy_match and not status_match:
        return {}

    metrics: dict[str, Any] = {
        "completed_mb": 0.0,
        "completed_seconds": 0.0,
        "completed_mb_per_s": 0.0,
        "memory_copy_mb": 0.0,
        "copy_mb_per_s": 0.0,
        "hardware_incidents": 0,
        "errors": 0,
        "status_text": status_match.group("status") if status_match else "",
        "raw_summary": "\n".join(summary_lines),
    }
    if completed_match:
        metrics["completed_mb"] = float(completed_match.group("mb"))
        metrics["completed_seconds"] = float(completed_match.group("seconds"))
        metrics["completed_mb_per_s"] = float(completed_match.group("rate"))
        metrics["hardware_incidents"] = int(
            completed_match.group("hardware_incidents"))
        metrics["errors"] = int(completed_match.group("errors"))
    if memory_copy_match:
        metrics["memory_copy_mb"] = float(memory_copy_match.group("mb"))
        metrics["copy_mb_per_s"] = float(memory_copy_match.group("rate"))
    return metrics


def collect_ddr_info() -> dict[str, Any]:
    dmcinfo_text = read_text("/proc/dmcdbg/dmcinfo")
    dmcinfo = parse_dmcinfo(dmcinfo_text)
    manufacturer = describe_lpddr_manufacturer(
        dmcinfo.get("dram_type", ""),
        dmcinfo.get("dram_id", ""),
    )
    metrics = {
        "ddr_type": dmcinfo.get("dram_type", ""),
        "dmcinfo_available": bool(dmcinfo_text.strip()),
        "dmcinfo_raw": dmcinfo_text.strip(),
        "dram_id": dmcinfo.get("dram_id", ""),
        **manufacturer,
        "channels": dmcinfo.get("channels", []),
        "channel_count": dmcinfo.get("channel_count", 0),
        "total_bus_width_bits": dmcinfo.get("total_bus_width_bits", 0),
        "dmcinfo_total_size_mb": dmcinfo.get("total_size_mb", 0),
        "dmc_devfreq_state": dmcinfo.get("devfreq_state", ""),
        "dmcinfo_devfreq_enabled": dmcinfo.get("devfreq_enabled", False),
        "dmc_cur_freq_hz": dmcinfo.get("cur_freq_hz", 0),
        "dmcinfo_dram_freq_hz": dmcinfo.get("dram_freq_hz", 0),
        "dmcinfo_reported_freq_hz": dmcinfo.get("reported_freq_hz", 0),
        "dmc_governor": dmcinfo.get("governor", ""),
    }
    if not dmcinfo_text.strip():
        return {
            "status": "fail",
            "reason": "Unable to read /proc/dmcdbg/dmcinfo on the board.",
            "metrics": metrics,
        }
    return {"status": "pass", "reason": "", "metrics": metrics}


def run_ddr_speed_test(seconds: int, info_metrics: dict[str, Any] | None = None) -> dict[str, Any]:
    info_metrics = info_metrics or collect_ddr_info()["metrics"]
    meminfo = parse_meminfo(read_text("/proc/meminfo"))
    size_mb = calculate_ddr_speed_size_mb(meminfo)
    channel_count = info_metrics.get("channel_count", 0) or 4
    cpu_count = os.cpu_count() or 1
    copy_threads = max(1, min(channel_count, cpu_count, 4))
    metrics = {
        "ddr_type": info_metrics.get("ddr_type", ""),
        "dram_manufacturer_name": info_metrics.get("dram_manufacturer_name", "unknown"),
        "channel_count": channel_count,
        "total_bus_width_bits": info_metrics.get("total_bus_width_bits", 0),
        "duration_seconds": seconds,
        "size_mb": size_mb,
        "copy_threads": copy_threads,
        "memory_copy_mb": 0.0,
        "copy_mb_per_s": 0.0,
        "completed_mb": 0.0,
        "completed_mb_per_s": 0.0,
        "hardware_incidents": 0,
        "errors": 0,
        "raw_summary": "",
        "command": "",
    }

    if shutil.which("stressapptest") is None:
        return {
            "status": "fail",
            "reason": "stressapptest is not available on the board.",
            "metrics": metrics,
        }

    args = [
        "stressapptest",
        "-s",
        str(seconds),
        "-M",
        str(size_mb),
        "-m",
        str(copy_threads),
        "--printsec",
        "1",
        "-W",
        "-A",
    ]
    result = run_command(args, timeout=seconds + 30)
    metrics["command"] = result["command"]
    parsed = parse_stressapptest_output(result["stdout"] + result["stderr"])
    metrics.update(
        {
            "memory_copy_mb": parsed.get("memory_copy_mb", 0.0),
            "copy_mb_per_s": parsed.get("copy_mb_per_s", 0.0),
            "completed_mb": parsed.get("completed_mb", 0.0),
            "completed_mb_per_s": parsed.get("completed_mb_per_s", 0.0),
            "hardware_incidents": parsed.get("hardware_incidents", 0),
            "errors": parsed.get("errors", 0),
            "raw_summary": parsed.get("raw_summary", ""),
        }
    )

    if result["timed_out"]:
        return {"status": "fail", "reason": "stressapptest timed out.", "metrics": metrics}
    if result["returncode"] != 0:
        return {
            "status": "fail",
            "reason": f"stressapptest exited with code {result['returncode']}.",
            "metrics": metrics,
        }
    if parsed and (parsed.get("status_text") == "PASS" or parsed.get("completed_mb_per_s", 0) > 0):
        return {"status": "pass", "reason": "", "metrics": metrics}
    return {
        "status": "fail",
        "reason": "Unable to parse DDR throughput from stressapptest output.",
        "metrics": metrics,
    }


def collect_platform_info() -> dict[str, str]:
    kernel_result = run_command(["uname", "-a"])
    return {
        "kernel": kernel_result["stdout"].strip(),
    }


def run_board_ddr_test(seconds: int) -> dict[str, Any]:
    info = collect_ddr_info()
    speed = run_ddr_speed_test(seconds, info["metrics"])
    overall_status = "pass" if info["status"] == "pass" and speed["status"] == "pass" else "fail"
    return {
        "timestamp": utc_now_iso(),
        "platform": collect_platform_info(),
        "info": info,
        "speed": speed,
        "overall_status": overall_status,
    }


def values_to_strings(obj: Any) -> Any:
    """递归将 dict/list 中所有 value 转为 str，key 不变（用于 JSON 输出）。"""
    if isinstance(obj, dict):
        return {k: values_to_strings(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [values_to_strings(item) for item in obj]
    return str(obj)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read DDR info and run a board-side DDR bandwidth test.")
    parser.add_argument("--seconds", type=int, default=DEFAULT_SECONDS,
                        help="stressapptest duration in seconds.")
    parser.add_argument("--json-out", default="",
                        help="Optional path to save the JSON result.")
    return parser


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()
    report = values_to_strings(run_board_ddr_test(args.seconds))
    output = json.dumps(report, ensure_ascii=False, indent=2)
    print(output)
    if args.json_out:
        Path(args.json_out).write_text(output + "\n", encoding="utf-8")
    return 0 if report["overall_status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
