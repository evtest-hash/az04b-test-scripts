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


EMMC_MANUFACTURERS = {
    0x13: "Micron",
    0x15: "Samsung",
    0x27: "Apacer",
    0x5D: "Swissbit",
    0x70: "Kingston",
    0x90: "Hynix",
    0x9F: "Kingston",
}

DEFAULT_TEST_DIR = "/root/rockchip_tester_emmc_fio"
DEFAULT_SIZE_MB = 512


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


def parse_manfid(text: str) -> int | None:
    value = str(text).strip().lower()
    if not value:
        return None
    try:
        return int(value, 16) if value.startswith("0x") else int(value, 10)
    except ValueError:
        return None


def describe_emmc_manufacturer(manfid_text: str) -> dict[str, str]:
    manfid_value = parse_manfid(manfid_text)
    normalized_id = manfid_text.strip() if manfid_text.strip() else ""
    if manfid_value is None:
        return {
            "manufacturer_id": normalized_id,
            "manufacturer_name": "unknown",
            "manufacturer_confidence": "low",
        }

    if not normalized_id:
        normalized_id = f"0x{manfid_value:02x}"
    manufacturer_name = EMMC_MANUFACTURERS.get(manfid_value, "unknown")
    return {
        "manufacturer_id": normalized_id,
        "manufacturer_name": manufacturer_name,
        "manufacturer_confidence": "high" if manufacturer_name != "unknown" else "low",
    }


def parse_root_mount(mounts_text: str) -> tuple[str, str]:
    for raw_line in mounts_text.splitlines():
        parts = raw_line.split()
        if len(parts) >= 3 and parts[1] == "/":
            return parts[0], parts[2]
    return "", ""


def extract_cmdline_root(cmdline_text: str) -> str:
    match = re.search(r"(?:^|\s)root=([^\s]+)", cmdline_text)
    return match.group(1).strip() if match else ""


def resolve_device_by_partition_attr(attr: str, value: str) -> str:
    if not value:
        return ""
    for uevent_path in sorted(Path("/sys/class/block").glob("*/uevent")):
        attrs = parse_key_value_lines(read_text(str(uevent_path)))
        if attrs.get("DEVTYPE") == "partition" and attrs.get(attr) == value:
            return "/dev/" + attrs.get("DEVNAME", uevent_path.parent.name)
    return ""


def parent_block_device(device_path: str) -> str:
    name = os.path.basename(device_path.strip())
    if not name:
        return ""
    if re.fullmatch(r"mmcblk\d+p\d+", name) or re.fullmatch(r"nvme\d+n\d+p\d+", name):
        return re.sub(r"p\d+$", "", name)
    return re.sub(r"\d+$", "", name)


def resolve_root_partition_device(root_mount: str, cmdline_text: str) -> str:
    try:
        st = os.stat("/")
        resolved = os.path.realpath(
            f"/sys/dev/block/{os.major(st.st_dev)}:{os.minor(st.st_dev)}"
        )
        name = os.path.basename(resolved)
        if re.fullmatch(r"(?:mmcblk|nvme|sd)\S*\d", name):
            return f"/dev/{name}"
    except OSError:
        pass

    if root_mount.startswith("/dev/") and root_mount != "/dev/root":
        return root_mount

    root_arg = extract_cmdline_root(cmdline_text)
    if root_arg.startswith("PARTUUID="):
        return resolve_device_by_partition_attr("PARTUUID", root_arg.split("=", 1)[1])
    if root_arg.startswith("PARTLABEL="):
        return resolve_device_by_partition_attr("PARTNAME", root_arg.split("=", 1)[1])
    if root_arg.startswith("/dev/"):
        return root_arg
    return ""


def detect_extcsd_path(block_device: str) -> tuple[str, str]:
    resolved = os.path.realpath(f"/sys/block/{block_device}/device")
    card_node = os.path.basename(resolved)
    if ":" not in card_node:
        return "", ""
    host_name = card_node.split(":", 1)[0]
    return host_name, f"/sys/kernel/debug/{host_name}/{card_node}/ext_csd"


def parse_mmc_ios(ios_text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in ios_text.splitlines():
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        result[key.strip()] = value.strip()
    return result


def extract_active_mode_from_ios(ios_text: str) -> str:
    ios = parse_mmc_ios(ios_text)
    timing = ios.get("timing spec", "")
    match = re.search(r"\((.+)\)", timing)
    return match.group(1).strip() if match else timing


def parse_key_value_lines(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        if "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        values[key.strip()] = value.strip().strip('"')
    return values


def resolve_mount_for_path(path: str, mounts_text: str) -> dict[str, str]:
    real_path = os.path.realpath(path)
    best_mountpoint = ""
    best_source = ""
    best_filesystem = ""
    for raw_line in mounts_text.splitlines():
        parts = raw_line.split()
        if len(parts) < 3:
            continue
        source, mountpoint, filesystem = parts[0], parts[1], parts[2]
        if mountpoint == "/":
            prefix_match = True
        else:
            prefix_match = real_path == mountpoint or real_path.startswith(mountpoint.rstrip("/") + "/")
        if prefix_match and len(mountpoint) > len(best_mountpoint):
            best_mountpoint = mountpoint
            best_source = source
            best_filesystem = filesystem
    return {
        "mountpoint": best_mountpoint,
        "source": best_source,
        "filesystem": best_filesystem,
    }


def decode_extcsd(hex_text: str) -> dict[str, Any]:
    cleaned = "".join(hex_text.split())
    if not cleaned:
        return {}
    try:
        raw = bytes.fromhex(cleaned)
    except ValueError:
        return {}
    if len(raw) != 512:
        return {}

    def le(start: int, end: int) -> int:
        value = 0
        for index, item in enumerate(raw[start : end + 1]):
            value |= item << (index * 8)
        return value

    device_type = raw[196]
    modes = []
    mode_map = [
        (0x01, "HS26"),
        (0x02, "HS52"),
        (0x04, "DDR52_1V8_3V"),
        (0x08, "DDR52_1V2"),
        (0x10, "HS200_1V8"),
        (0x20, "HS200_1V2"),
        (0x40, "HS400_1V8"),
        (0x80, "HS400_1V2"),
    ]
    for bit, label in mode_map:
        if device_type & bit:
            modes.append(label)

    return {
        "ext_csd_rev": raw[192],
        "device_type_raw": device_type,
        "device_type_modes": modes,
        "cmdq_support": raw[308],
        "cache_size_bytes": le(249, 252),
        "sector_count": le(212, 215),
        "boot_size_mult": raw[226],
        "rpmb_size_mult": raw[168],
        "pre_eol_info": raw[267],
        "life_time_a": raw[268],
        "life_time_b": raw[269],
        "ffu_features": raw[492],
        "bkops_support": raw[502],
        "hpi_features": raw[503],
    }


def collect_platform_info(root_metrics: dict[str, Any]) -> dict[str, Any]:
    kernel_result = run_command(["uname", "-a"])
    return {
        "kernel": kernel_result["stdout"].strip(),
        "root_mount": root_metrics.get("root_mount", ""),
        "root_filesystem": root_metrics.get("root_filesystem", ""),
        "root_partition_device": root_metrics.get("root_partition_device", ""),
        "block_device": root_metrics.get("block_device", ""),
    }


def collect_emmc_info() -> dict[str, Any]:
    mounts_text = read_text("/proc/mounts")
    cmdline_text = read_text("/proc/cmdline")
    root_mount, root_filesystem = parse_root_mount(mounts_text)
    root_partition_device = resolve_root_partition_device(root_mount, cmdline_text)
    block_device = parent_block_device(root_partition_device)

    metrics: dict[str, Any] = {
        "root_mount": root_mount,
        "root_filesystem": root_filesystem,
        "root_partition_device": root_partition_device,
        "block_device": block_device,
        "type": "",
        "name": "",
        "manfid": "",
        "manufacturer_id": "",
        "manufacturer_name": "unknown",
        "manufacturer_confidence": "low",
        "rev": "",
        "cid": "",
        "life_time": "",
        "pre_eol_info": "",
        "active_mode": "",
        "ext_csd": {},
    }
    if not block_device or not root_partition_device:
        return {
            "status": "fail",
            "reason": "Unable to resolve root eMMC block device from the current root filesystem.",
            "metrics": metrics,
        }
    if not block_device.startswith("mmcblk"):
        return {
            "status": "fail",
            "reason": "Root filesystem is not backed by an eMMC mmcblk device.",
            "metrics": metrics,
        }

    fields = [
        "type",
        "name",
        "manfid",
        "rev",
        "cid",
        "life_time",
        "pre_eol_info",
    ]
    for field in fields:
        metrics[field] = read_text(f"/sys/block/{block_device}/device/{field}").strip()
    metrics.update(describe_emmc_manufacturer(metrics["manfid"]))

    host_name, extcsd_path = detect_extcsd_path(block_device)
    metrics["host_name"] = host_name
    ios_text = read_text(f"/sys/kernel/debug/{host_name}/ios") if host_name else ""
    metrics["active_mode"] = extract_active_mode_from_ios(ios_text)
    metrics["mmc_ios"] = parse_mmc_ios(ios_text)
    metrics["ext_csd"] = decode_extcsd(read_text(extcsd_path))

    blkid_result = run_command(["blkid", root_partition_device])
    metrics["rootfs_blkid"] = blkid_result["stdout"].strip() if blkid_result["returncode"] == 0 else ""
    if not metrics["root_filesystem"] and metrics["rootfs_blkid"]:
        metrics["root_filesystem"] = parse_key_value_lines(metrics["rootfs_blkid"]).get("TYPE", "")

    return {"status": "pass", "reason": "", "metrics": metrics}


def percentile_from_map(values: dict[str, Any], target: float) -> float:
    if not isinstance(values, dict) or not values:
        return 0.0
    best_value = 0.0
    best_distance = None
    for key, value in values.items():
        try:
            distance = abs(float(key) - target)
            numeric_value = float(value)
        except (TypeError, ValueError):
            continue
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_value = numeric_value
    return best_value


def extract_latency_ns(stats: dict[str, Any]) -> tuple[float, float, float]:
    for key, multiplier in (
        ("lat_ns", 1.0),
        ("lat_usec", 1_000.0),
        ("lat_msec", 1_000_000.0),
        ("lat_sec", 1_000_000_000.0),
    ):
        bucket = stats.get(key)
        if not isinstance(bucket, dict) or not bucket:
            continue
        mean = float(bucket.get("mean", 0.0)) * multiplier
        percentiles = bucket.get("percentile", {})
        p95 = percentile_from_map(percentiles, 95.0) * multiplier
        p99 = percentile_from_map(percentiles, 99.0) * multiplier
        return mean, p95, p99
    return 0.0, 0.0, 0.0


def parse_fio_job_output(text: str, operation: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    jobs = payload.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        return {}
    job = jobs[0]
    op_metrics = job.get(operation)
    if not isinstance(op_metrics, dict) or not op_metrics:
        return {}
    lat_ns_mean, lat_ns_p95, lat_ns_p99 = extract_latency_ns(op_metrics)
    return {
        "io_bytes": int(op_metrics.get("io_bytes", 0)),
        "bw_bytes": int(op_metrics.get("bw_bytes", 0)),
        "bw_kib_per_s": float(op_metrics.get("bw", 0.0)),
        "iops": float(op_metrics.get("iops", 0.0)),
        "runtime_ms": int(op_metrics.get("runtime", 0)),
        "lat_ns_mean": round(lat_ns_mean, 3),
        "lat_ns_p95": round(lat_ns_p95, 3),
        "lat_ns_p99": round(lat_ns_p99, 3),
    }


def ensure_directory(path: str) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


def cleanup_test_file(path: str) -> None:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
    run_command(["sync"], timeout=10)


def run_emmc_speed_test(test_dir: str, size_mb: int, info_metrics: dict[str, Any] | None = None) -> dict[str, Any]:
    info_metrics = info_metrics or collect_emmc_info()["metrics"]
    block_device = info_metrics.get("block_device", "")
    root_partition_device = info_metrics.get("root_partition_device", "")
    metrics: dict[str, Any] = {
        "block_device": block_device,
        "root_partition_device": root_partition_device,
        "test_dir": test_dir,
        "test_size_mb": size_mb,
        "fio_version": "",
        "write": {},
        "read": {},
        "command_write": "",
        "command_read": "",
    }
    if not block_device or not root_partition_device:
        return {
            "status": "fail",
            "reason": "Unable to resolve root eMMC block device from the current root filesystem.",
            "metrics": metrics,
        }
    if not block_device.startswith("mmcblk"):
        return {
            "status": "fail",
            "reason": "Root filesystem is not backed by an eMMC mmcblk device.",
            "metrics": metrics,
        }
    if shutil.which("fio") is None:
        return {"status": "fail", "reason": "fio is not available on the board.", "metrics": metrics}

    mounts_text = read_text("/proc/mounts")
    mount_info = resolve_mount_for_path(test_dir, mounts_text)
    metrics["test_dir_mountpoint"] = mount_info["mountpoint"]
    metrics["test_dir_source"] = mount_info["source"]
    metrics["test_dir_filesystem"] = mount_info["filesystem"]
    if mount_info["mountpoint"] != "/":
        return {
            "status": "fail",
            "reason": "The eMMC test directory must be located on the root filesystem.",
            "metrics": metrics,
        }

    ensure_directory(test_dir)
    file_path = os.path.join(test_dir, "fio.bin")
    version_result = run_command(["fio", "--version"], timeout=10)
    metrics["fio_version"] = (version_result["stdout"] or version_result["stderr"]).strip()

    write_args = [
        "fio",
        "--name=seqwrite",
        f"--filename={file_path}",
        "--rw=write",
        "--bs=1M",
        f"--size={size_mb}M",
        "--ioengine=sync",
        "--direct=1",
        "--invalidate=1",
        "--numjobs=1",
        "--fsync_on_close=1",
        "--output-format=json",
    ]
    read_args = [
        "fio",
        "--name=seqread",
        f"--filename={file_path}",
        "--rw=read",
        "--bs=1M",
        f"--size={size_mb}M",
        "--ioengine=sync",
        "--direct=1",
        "--invalidate=1",
        "--numjobs=1",
        "--output-format=json",
    ]
    metrics["command_write"] = " ".join(write_args)
    metrics["command_read"] = " ".join(read_args)

    write_timeout = max(size_mb // 8, 60)
    read_timeout = max(size_mb // 8, 60)
    write_result = run_command(write_args, timeout=write_timeout)
    try:
        if write_result["timed_out"]:
            return {"status": "fail", "reason": "fio write test timed out.", "metrics": metrics}
        if write_result["returncode"] != 0:
            return {
                "status": "fail",
                "reason": f"fio write test exited with code {write_result['returncode']}.",
                "metrics": metrics,
            }
        metrics["write"] = parse_fio_job_output(write_result["stdout"], "write")
        read_result = run_command(read_args, timeout=read_timeout)
        if read_result["timed_out"]:
            return {"status": "fail", "reason": "fio read test timed out.", "metrics": metrics}
        if read_result["returncode"] != 0:
            return {
                "status": "fail",
                "reason": f"fio read test exited with code {read_result['returncode']}.",
                "metrics": metrics,
            }
        metrics["read"] = parse_fio_job_output(read_result["stdout"], "read")
    finally:
        cleanup_test_file(file_path)

    if metrics["write"] and metrics["read"]:
        return {"status": "pass", "reason": "", "metrics": metrics}
    return {
        "status": "fail",
        "reason": "Unable to parse eMMC throughput from fio JSON output.",
        "metrics": metrics,
    }


def run_board_emmc_test(test_dir: str, size_mb: int) -> dict[str, Any]:
    info = collect_emmc_info()
    speed = run_emmc_speed_test(test_dir, size_mb, info["metrics"])
    overall_status = "pass" if info["status"] == "pass" and speed["status"] == "pass" else "fail"
    return {
        "timestamp": utc_now_iso(),
        "platform": collect_platform_info(info["metrics"]),
        "info": info,
        "speed": speed,
        "overall_status": overall_status,
    }


def values_to_strings(obj: Any) -> Any:
    """递归将 dict/list 中所有 value 转为 str，key 不变。"""
    if isinstance(obj, dict):
        return {k: values_to_strings(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [values_to_strings(item) for item in obj]
    return str(obj)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read eMMC info and run a board-side eMMC speed test with fio.")
    parser.add_argument(
        "--test-dir",
        default=DEFAULT_TEST_DIR,
        help="Directory on the root filesystem used for the fio test file.",
    )
    parser.add_argument("--size-mb", type=int, default=DEFAULT_SIZE_MB, help="fio sequential test file size in MiB.")
    parser.add_argument("--json-out", default="", help="Optional path to save the JSON result.")
    return parser


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()
    report = run_board_emmc_test(args.test_dir, args.size_mb)
    report = values_to_strings(report)
    output = json.dumps(report, ensure_ascii=False, indent=2)
    print(output)
    if args.json_out:
        Path(args.json_out).write_text(output + "\n", encoding="utf-8")
    return 0 if report["overall_status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
