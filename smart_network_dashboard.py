#!/usr/bin/env python3
"""
Smart Network — Connectivity & Storage Diagnostic Dashboard
Local-only macOS diagnostic backend.
Version 1.0 — Read-only discovery and network diagnostics.
"""

import json
import io
import csv
import hashlib
import tempfile
import uuid
import os
import re
import socket
import shutil
import subprocess
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE = Path(__file__).resolve().parent
HTML_FILE = BASE / "index.html"
SCREENSHOT_DIR = BASE / "Screenshots"
LOG_DIR = BASE / "Logs"
HOST = "127.0.0.1"
PORT = 8765

# Historical project references; these are targets, not assumed live status.
TARGETS = {
    "Windows Wi-Fi": "192.168.2.162",
    "Windows Ethernet": "169.254.1.126",
}
FTP_PORT = 21
SMB_PORT = 445


def run_command(args, timeout=5):
    try:
        p = subprocess.run(
            args, capture_output=True, text=True,
            timeout=timeout, check=False
        )
        return p.stdout.strip(), p.stderr.strip(), p.returncode
    except Exception as exc:
        return "", str(exc), 1


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def get_hardware_ports():
    output, _, _ = run_command(["networksetup", "-listallhardwareports"])
    result = {}
    current = None
    for line in output.splitlines():
        if line.startswith("Hardware Port:"):
            current = line.split(":", 1)[1].strip()
        elif line.startswith("Device:") and current:
            result[current] = line.split(":", 1)[1].strip()
            current = None
    return result



def discover_usb_hardware():
    output, error, code = run_command(
        [
            "system_profiler", "-json",
            "SPUSBDataType",
            "SPThunderboltDataType",
            "SPDisplaysDataType",
        ],
        timeout=15
    )

    result = {
        "available": False,
        "usb_devices": [],
        "thunderbolt_devices": [],
        "displays": [],
        "note": "Device inventory only; physical port health is not verified."
    }

    if not output:
        result["note"] = error or "Hardware inventory unavailable"
        return result

    try:
        import json
        data = json.loads(output)

        def collect(node):
            found = []
            if isinstance(node, dict):
                name = (
                    node.get("_name")
                    or node.get("device_name")
                    or node.get("spdisplays_name")
                )
                if isinstance(name, str) and name.strip():
                    found.append(name.strip())
                for value in node.values():
                    found.extend(collect(value))
            elif isinstance(node, list):
                for value in node:
                    found.extend(collect(value))
            return found

        def unique(values):
            return list(dict.fromkeys(values))

        result["usb_devices"] = unique(
            collect(data.get("SPUSBDataType", []))
        )
        result["thunderbolt_devices"] = unique(
            collect(data.get("SPThunderboltDataType", []))
        )
        result["displays"] = unique(
            collect(data.get("SPDisplaysDataType", []))
        )
        result["available"] = code == 0

    except Exception as exc:
        result["note"] = "Hardware inventory parse error: " + str(exc)

    return result


def get_interface_info(name, device):
    output, _, _ = run_command(["ifconfig", device])
    ip_match = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)", output)
    status = "UP" if re.search(r"<[^>]*\bUP\b", output) else "DOWN"
    running = bool(re.search(r"<[^>]*\bRUNNING\b", output))
    media_match = re.search(r"\bmedia:\s*(.+)", output)
    mac_match = re.search(r"\bether\s+([0-9a-f:]{17})", output, re.I)
    return {
        "name": name,
        "device": device,
        "ip": ip_match.group(1) if ip_match else None,
        "status": status,
        "running": running,
        "media": media_match.group(1).strip() if media_match else None,
        "mac": mac_match.group(1) if mac_match else None,
    }


def route_interface(address):
    output, _, code = run_command(
        ["route", "-n", "get", address], timeout=3
    )
    match = re.search(r"^\s*interface:\s*(\S+)", output, re.M)
    return match.group(1) if code == 0 and match else None


def ping_test(address):
    start = time.perf_counter()
    output, error, code = run_command(
        ["ping", "-c", "3", "-W", "1200", address], timeout=6
    )
    elapsed = round((time.perf_counter() - start) * 1000, 1)
    loss = None
    avg = None
    loss_match = re.search(r"([\d.]+)% packet loss", output)
    avg_match = re.search(r"=\s*[\d.]+/([\d.]+)/", output)
    if loss_match:
        loss = float(loss_match.group(1))
    if avg_match:
        avg = float(avg_match.group(1))
    return {
        "ok": code == 0 and loss is not None and loss < 100,
        "packet_loss_percent": loss,
        "average_ms": avg,
        "elapsed_ms": elapsed,
        "detail": (output or error)[-500:],
    }


def tcp_test(address, port, timeout=2.5):
    start = time.perf_counter()
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return {
                "ok": True,
                "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
                "detail": "TCP connection accepted",
            }
    except Exception as exc:
        return {
            "ok": False,
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
            "detail": str(exc),
        }


def ftp_banner(address):
    try:
        with socket.create_connection((address, FTP_PORT), timeout=3) as sock:
            sock.settimeout(3)
            data = sock.recv(512).decode("utf-8", "replace").strip()
            return {"ok": data.startswith("220"), "banner": data[:250]}
    except Exception as exc:
        return {"ok": False, "banner": str(exc)}


def discover_storage():
    output, _, _ = run_command(["mount"], timeout=4)
    volumes = []
    volume_root = Path("/Volumes")
    if volume_root.exists():
        for item in sorted(volume_root.iterdir(), key=lambda p: p.name.lower()):
            try:
                if item.is_dir():
                    stat = item.stat()
                    try:
                        usage = shutil.disk_usage(item)
                        capacity = {
                            "total_bytes": usage.total,
                            "used_bytes": usage.used,
                            "free_bytes": usage.free,
                            "used_percent": round((usage.used / usage.total) * 100, 1) if usage.total else None,
                        }
                    except OSError:
                        capacity = None
                    volumes.append({
                        "name": item.name,
                        "path": str(item),
                        "readable": os.access(item, os.R_OK),
                        "writable": os.access(item, os.W_OK),
                        "capacity": capacity,
                    })
            except Exception:
                volumes.append({
                    "name": item.name, "path": str(item),
                    "readable": False, "writable": False, "capacity": None,
                })

    smb_lines = [
        line.strip() for line in output.splitlines()
        if "smbfs" in line.lower() or "smb://" in line.lower()
    ]
    return {
        "volumes": volumes,
        "smb_mounts": smb_lines,
        "note": "Mounted volumes only; Windows drive letters are not inferred.",
    }



def inspect_mac_smb_server():
    port = tcp_test("127.0.0.1", SMB_PORT)
    output, error, code = run_command(["sharing", "-l"])

    expected = {
        "Documents": "/Users/x/Documents",
        "Investment-2025": "/Users/x/Documents/Investment-2025",
        "SIEM_Inbox": "/Users/x/SIEM_Inbox",
    }
    discovered = {}

    blocks = re.split(r"(?m)(?=^name:\s*)", output)
    for block in blocks:
        match = re.match(r"name:\s*(.+)\npath:\s*(.+)", block)
        if not match:
            continue

        name = match.group(1).strip()
        path = match.group(2).strip()
        shared = bool(re.search(r"(?m)^\s*shared:\s*1\s*$", block))
        read_only = bool(re.search(r"(?m)^\s*read-only:\s*1\s*$", block))

        folder_exists = os.path.isdir(path)
        readable = folder_exists and os.access(path, os.R_OK)
        writable = folder_exists and os.access(path, os.W_OK)

        discovered[name] = {
            "path": path,
            "shared": shared,
            "read_only": read_only,
            "folder_exists": folder_exists,
            "readable": readable,
            "writable": writable,
            "verified": bool(shared and folder_exists and readable),
        }

    shares = {}
    for name, expected_path in expected.items():
        item = discovered.get(name)
        if item:
            shares[name] = item
            if item["path"] != expected_path:
                shares[name]["expected_path_match"] = False
            else:
                shares[name]["expected_path_match"] = True
        else:
            shares[name] = {
                "path": None,
                "shared": False,
                "read_only": None,
                "folder_exists": False,
                "readable": False,
                "writable": False,
                "verified": False,
                "expected_path_match": False,
            }

    return {
        "tcp_445": port,
        "share_list_command_ok": code == 0,
        "share_list_error": error.strip() or None,
        "shares": shares,
        "windows_user_session": {
            "status": "PENDING",
            "note": "No live Windows user-session agent is connected."
        }
    }


def build_status():
    ports = get_hardware_ports()
    usb_hardware = discover_usb_hardware()
    interfaces = {}
    for label, device in ports.items():
        if device in ("en0", "en4") or "wi-fi" in label.lower() or "ethernet" in label.lower():
            interfaces[label] = get_interface_info(label, device)

    wifi = next(
        (v for k, v in interfaces.items()
         if "wi-fi" in k.lower() or "airport" in k.lower()
         or v["device"] == "en0"),
        None
    )
    ethernet = next(
        (v for k, v in interfaces.items()
         if "ethernet" in k.lower() or v["device"] == "en4"),
        None
    )

    gateway_out, _, _ = run_command(["route", "-n", "get", "default"])
    gateway_match = re.search(r"^\s*gateway:\s*(\S+)", gateway_out, re.M)
    gateway = gateway_match.group(1) if gateway_match else None

    internet = ping_test("1.1.1.1")
    paths = {}
    for label, address in TARGETS.items():
        route = route_interface(address)
        paths[label] = {
            "address": address,
            "route_interface": route,
            "ping": ping_test(address),
            "ftp_tcp": tcp_test(address, FTP_PORT),
            "ftp_banner": ftp_banner(address),
            "smb_tcp": tcp_test(address, SMB_PORT),
        }

    router_ping = ping_test(gateway) if gateway else {
        "ok": False, "note": "Default gateway not detected"
    }

    router_arp_out, _, _ = run_command(
        ["arp", "-n", gateway] if gateway else ["true"]
    )
    router_mac_match = re.search(
        r"\bat\s+([0-9a-fA-F:]{11,17})",
        router_arp_out
    )
    router_mac = None
    if router_mac_match:
        parts = router_mac_match.group(1).split(":")
        if len(parts) == 6:
            try:
                router_mac = ":".join(
                    f"{int(part, 16):02x}" for part in parts
                )
            except ValueError:
                router_mac = None

    hardware_out, _, _ = run_command(
        ["system_profiler", "SPHardwareDataType"]
    )

    def hardware_value(pattern):
        match = re.search(pattern, hardware_out, re.M | re.I)
        return match.group(1).strip() if match else None

    mac_hardware = {
        "model": hardware_value(r"^\s*Model Name:\s*(.+)$"),
        "model_identifier": hardware_value(r"^\s*Model Identifier:\s*(.+)$"),
        "chip": hardware_value(r"^\s*Chip:\s*(.+)$")
                or hardware_value(r"^\s*Processor Name:\s*(.+)$"),
        "memory": hardware_value(r"^\s*Memory:\s*(.+)$"),
    }

    return {
        "timestamp": now(),
        "host": socket.gethostname(),
        "platform": "macOS",
        "interfaces": interfaces,
        "wifi": wifi,
        "ethernet": ethernet,
        "default_gateway": gateway,
        "router": {
            "gateway": gateway,
            "ping": router_ping,
            "mac_address": router_mac,
            "arp_details": router_arp_out.strip() or None,
            "management_metrics": "Not Available without authorized router management access",
        },
        "mac_hardware": mac_hardware,
        "usb_hardware": usb_hardware,
        "internet_ping": internet,
        "mac_smb_server": inspect_mac_smb_server(),
        "smb_recovery_monitor": get_smb_monitor_status(),
        "targets": paths,
        "storage": discover_storage(),
        "safety": {
            "mode": "READ-ONLY",
            "file_transactions_enabled": False,
            "automatic_cleanup_enabled": False,
        },
    }


def log_event(message):
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / "dashboard.log"
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{now()} | {message}\n")




# F_STORAGE_TEST_ENGINE — dedicated, isolated F: connectivity transaction.
F_TEST_MOUNT = Path("/Volumes/Windows_F_Transcend")
F_TEST_SIZE = 256 * 1024

def _f_test_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()



# STAGE2_DAEMON_PROCESS_HEALTH_V2
def inspect_smb_daemon():
    output, error, code = run_command(
        ["launchctl", "print", "system/com.apple.smbd"],
        timeout=5
    )

    state_match = re.search(
        r"^\s*state = (\S+)",
        output,
        re.M
    )
    pid_match = re.search(
        r"^\s*pid = (\d+)",
        output,
        re.M
    )

    service_state = state_match.group(1) if state_match else None
    pid = pid_match.group(1) if pid_match else None

    process_state = None
    process_ok = False

    if pid:
        ps_output, ps_error, ps_code = run_command(
            ["ps", "-o", "state=", "-p", pid],
            timeout=3
        )
        process_state = ps_output.strip() or None

        process_ok = bool(
            ps_code == 0
            and process_state
            and not process_state.startswith(("T", "Z"))
        )

    service_ok = bool(
        code == 0 and service_state == "running"
    )

    return {
        "service_ok": service_ok,
        "process_ok": process_ok,
        "ok": service_ok and process_ok,
        "pid": pid,
        "service_state": service_state,
        "process_state": process_state,
        "detail": (output or error)[-1000:]
    }

# REAL_MAC_SMB_RECOVERY_V1
_SMB_RECOVERY_LAST_ATTEMPT = 0.0
_SMB_RECOVERY_LOCK = threading.Lock()
_SMB_RECOVERY_COOLDOWN = 300
_SMB_RECOVERY_HELPER = "/usr/local/sbin/smart-network-smb-recovery"


def run_safe_recovery():
    """
    Stage 2: real, restricted Mac SMB service recovery.
    Recovery is attempted only when the local SMB service or TCP 445
    is unhealthy. No FTP, network switching, route changes, or mounts.
    """
    global _SMB_RECOVERY_LAST_ATTEMPT

    started = now()
    attempts = []
    recovery = {
        "performed": [],
        "not_performed": [],
        "status": "NOT_REQUIRED",
        "helper_exit_code": None,
        "detail": None
    }

    def record(label, ok, result, historical=False):
        attempts.append({
            "check": label,
            "ok": bool(ok),
            "result": result,
            "historical": historical
        })

    def check_service():
        detail = inspect_smb_daemon()
        return bool(detail["ok"]), detail

    def check_local_port():
        result = tcp_test("127.0.0.1", SMB_PORT)
        return bool(result.get("ok")), result

    initial_service_ok, initial_service = check_service()
    initial_port_ok, initial_port = check_local_port()

    record(
        "Initial macOS SMB Service",
        initial_service_ok,
        initial_service,
        historical=True
    )
    record(
        "Initial Mac SMB TCP 445",
        initial_port_ok,
        initial_port,
        historical=True
    )

    if not initial_service_ok or not initial_port_ok:
        with _SMB_RECOVERY_LOCK:
            elapsed = time.monotonic() - _SMB_RECOVERY_LAST_ATTEMPT

            if elapsed >= _SMB_RECOVERY_COOLDOWN:
                _SMB_RECOVERY_LAST_ATTEMPT = time.monotonic()

                try:
                    process = subprocess.run(
                        [
                            "/usr/bin/sudo", "-n",
                            _SMB_RECOVERY_HELPER, "restart"
                        ],
                        capture_output=True,
                        text=True,
                        timeout=25,
                        check=False
                    )
                    helper_ok = process.returncode == 0
                    recovery["helper_exit_code"] = process.returncode
                    recovery["detail"] = (
                        (process.stdout or "") +
                        (process.stderr or "")
                    )[-2000:]

                    if helper_ok:
                        recovery["performed"].append(
                            "Restarted macOS SMB service using restricted helper"
                        )
                        recovery["status"] = "RESTART_COMMAND_SUCCEEDED"
                    else:
                        recovery["status"] = "HELPER_FAILED"

                    record(
                        "SMB Recovery Helper",
                        helper_ok,
                        {
                            "exit_code": process.returncode,
                            "detail": recovery["detail"]
                        }
                    )

                except Exception as exc:
                    recovery["status"] = "HELPER_ERROR"
                    recovery["detail"] = str(exc)
                    record(
                        "SMB Recovery Helper",
                        False,
                        {"error": str(exc)}
                    )
            else:
                recovery["status"] = "COOLDOWN_ACTIVE"
                recovery["not_performed"].append(
                    "Restart skipped because the five-minute cooldown is active"
                )
    else:
        recovery["not_performed"].append(
            "No restart needed; initial SMB service and TCP 445 were healthy"
        )

    # Verify actual state after the recovery decision.
    final_service_ok, final_service = check_service()
    final_port_ok, final_port = check_local_port()

    record("Final macOS SMB Service", final_service_ok, final_service)
    record("Final Mac SMB TCP 445", final_port_ok, final_port)

    try:
        smb = inspect_mac_smb_server()
        shares_ok = True

        for name, item in smb.get("shares", {}).items():
            share_ok = bool(
                item.get("shared")
                and item.get("folder_exists")
                and item.get("readable")
                and item.get("expected_path_match")
            )
            shares_ok = shares_ok and share_ok
            record("Final SMB Share " + name, share_ok, item)

    except Exception as exc:
        smb = {}
        shares_ok = False
        record("Final SMB Share Inspection", False, {"error": str(exc)})

    # Preserve the existing Windows path diagnostics.
    for label, address in TARGETS.items():
        route = route_interface(address)
        record(
            label + " Route",
            bool(route),
            {"address": address, "interface": route}
        )

        ping = ping_test(address)
        record(label + " Ping", ping.get("ok"), ping)

        remote_smb = tcp_test(address, SMB_PORT)
        record(
            label + " SMB TCP 445",
            remote_smb.get("ok"),
            remote_smb
        )

    # Historical initial failures do not make a successfully recovered
    # final state appear failed. They remain visible in the attempt log.
    current_attempts = [
        item for item in attempts if not item.get("historical")
    ]
    passed = sum(1 for item in current_attempts if item["ok"])
    failed = len(current_attempts) - passed

    # Mac SMB recovery health is independent of Windows path diagnostics.
    local_ok = bool(
        final_service_ok
        and final_port_ok
        and shares_ok
    )

    remote_attempts = [
        item for item in current_attempts
        if item["check"].startswith("Windows ")
    ]
    remote_paths_ok = all(item["ok"] for item in remote_attempts)

    if recovery["performed"] and local_ok:
        recovery["status"] = "RECOVERED_AND_VERIFIED"
    elif recovery["performed"] and not local_ok:
        recovery["status"] = "RECOVERY_ATTEMPTED_BUT_NOT_VERIFIED"
    elif not local_ok and recovery["status"] == "NOT_REQUIRED":
        recovery["status"] = "LOCAL_SMB_HEALTH_FAILED"

    recovery["cooldown_seconds"] = _SMB_RECOVERY_COOLDOWN

    recovery["not_performed"].extend([
        "No Wi-Fi or Ethernet switching",
        "No route changes or interface reset",
        "No SMB mount or credential changes",
        "No Windows Explorer mapping changes",
        "No FTP or FileZilla operations"
    ])

    result = {
        "ok": local_ok,
        "remote_path_diagnostics_ok": remote_paths_ok,
        "mode": "REAL_MAC_SMB_RECOVERY_V1",
        "started_at": started,
        "finished_at": now(),
        "attempt_count": len(attempts),
        "passed": passed,
        "failed": failed,
        "historical_initial_failures": sum(
            1 for item in attempts
            if item.get("historical") and not item["ok"]
        ),
        "attempts": attempts,
        "recovery_actions": recovery,
        "final_health": {
            "mac_smb_service": final_service.get("service_ok", False),
            "mac_smb_process": final_service.get("process_ok", False),
            "mac_smb_pid": final_service.get("pid"),
            "mac_smb_process_state": final_service.get("process_state"),
            "mac_smb_tcp_445": final_port_ok,
            "mac_smb_shares": shares_ok
        },
        "windows_user_session": {
            "status": "PENDING",
            "note": "Live Windows Explorer session verification requires Stage 3."
        }
    }

    log_event(
        "Stage 2 real SMB recovery | "
        f"Recovery={recovery['status']} | "
        f"Passed={passed} | Failed={failed} | "
        f"Final={'PASS' if local_ok else 'FAILED'}"
    )

    return result



def run_f_storage_test():
    started_at = now()
    test_id = uuid.uuid4().hex
    remote_dir = F_TEST_MOUNT / f".smartnetwork_f_test_{test_id}"
    remote_file = remote_dir / "connectivity_test_256KB.bin"

    result = {
        "ok": False,
        "test_id": test_id,
        "target": str(F_TEST_MOUNT),
        "size_bytes": F_TEST_SIZE,
        "started_at": started_at,
        "upload_seconds": None,
        "download_seconds": None,
        "upload_speed_mbps": None,
        "download_speed_mbps": None,
        "original_sha256": None,
        "returned_sha256": None,
        "size_verified": False,
        "sha256_verified": False,
        "integrity": "NOT VERIFIED",
        "cleanup_ok": False,
        "cleanup": "NOT COMPLETED",
        "error": None,
    }

    created_dir = False
    created_remote_file = False
    integrity_ok = False

    try:
        if not F_TEST_MOUNT.is_dir() or not os.path.ismount(F_TEST_MOUNT):
            raise RuntimeError("F: Transcend is not mounted at the verified path.")

        if not os.access(F_TEST_MOUNT, os.R_OK | os.W_OK):
            raise RuntimeError("F: read/write permission is unavailable.")

        with tempfile.TemporaryDirectory(prefix="smartnetwork_f_test_") as local:
            local_dir = Path(local)
            source_file = local_dir / "source.bin"
            returned_file = local_dir / "returned.bin"

            source_file.write_bytes(os.urandom(F_TEST_SIZE))
            result["original_sha256"] = _f_test_sha256(source_file)

            remote_dir.mkdir()
            created_dir = True

            # Mark ownership before copying so a partial file is also cleaned.
            created_remote_file = True
            upload_start = time.perf_counter()
            shutil.copyfile(source_file, remote_file)
            result["upload_seconds"] = time.perf_counter() - upload_start

            download_start = time.perf_counter()
            shutil.copyfile(remote_file, returned_file)
            result["download_seconds"] = time.perf_counter() - download_start

            result["returned_sha256"] = _f_test_sha256(returned_file)
            result["returned_size_bytes"] = returned_file.stat().st_size

            result["size_verified"] = (
                source_file.stat().st_size == result["returned_size_bytes"]
            )
            result["sha256_verified"] = (
                result["original_sha256"] == result["returned_sha256"]
            )

            integrity_ok = (
                result["size_verified"] and result["sha256_verified"]
            )
            result["integrity"] = "PASS" if integrity_ok else "FAIL"

            if not integrity_ok:
                raise RuntimeError("File size or SHA-256 verification failed.")

            result["upload_speed_mbps"] = round(
                F_TEST_SIZE / result["upload_seconds"] / (1024 * 1024), 3
            ) if result["upload_seconds"] > 0 else None

            result["download_speed_mbps"] = round(
                F_TEST_SIZE / result["download_seconds"] / (1024 * 1024), 3
            ) if result["download_seconds"] > 0 else None

    except Exception as exc:
        result["error"] = str(exc)

    finally:
        cleanup_errors = []

        # Delete only the uniquely named file created by this test.
        if created_remote_file and remote_file.exists():
            try:
                remote_file.unlink()
            except Exception as exc:
                cleanup_errors.append(f"Test file: {exc}")

        # Remove only this test's empty directory. Never recurse.
        if created_dir and remote_dir.exists():
            try:
                remote_dir.rmdir()
            except Exception as exc:
                cleanup_errors.append(f"Test folder: {exc}")

        result["cleanup_ok"] = not cleanup_errors
        result["cleanup"] = (
            "PASS" if not cleanup_errors
            else "WARNING: " + "; ".join(cleanup_errors)
        )

        result["ok"] = bool(integrity_ok and result["cleanup_ok"])
        result["finished_at"] = now()

        if result["ok"]:
            log_event(
                f"F: storage test PASS | ID={test_id} | "
                f"Size={F_TEST_SIZE} | SHA-256 verified | Cleanup PASS"
            )
        else:
            log_event(
                f"F: storage test FAIL | ID={test_id} | "
                f"Integrity={result['integrity']} | "
                f"Cleanup={result['cleanup']} | Error={result['error']}"
            )

    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log_event(fmt % args)

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/smb-monitor":
            try:
                self.send_json(get_smb_monitor_status())
            except Exception as exc:
                self.send_json({"error": str(exc), "timestamp": now()}, 500)
            return

        if path == "/api/live":
            try:
                self.send_json(build_status())
            except Exception as exc:
                self.send_json({"error": str(exc), "timestamp": now()}, 500)
            return

        # FINAL_BATCH_ACTIVITY_ROUTE — read-only event log and exports
        if path == "/api/activity":
            log_file = LOG_DIR / "dashboard.log"
            entries = []
            if log_file.exists():
                try:
                    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
                    entries = [{"message": line} for line in lines]
                except Exception as exc:
                    self.send_json({"entries": [], "error": str(exc)}, 500)
                    return
            self.send_json({
                "entries": entries,
                "count": len(entries),
                "source": "Logs/dashboard.log",
                "note": "Backend events only; not a completed diagnostic transaction."
            })
            return

        if path == "/api/report.csv":
            log_file = LOG_DIR / "dashboard.log"
            lines = []
            if log_file.exists():
                try:
                    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
                except Exception:
                    lines = []
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["Timestamp", "Backend Event"])
            for line in lines:
                writer.writerow([now(), line])
            body = output.getvalue().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="SmartNetwork_Event_Report.csv"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/report.txt":
            log_file = LOG_DIR / "dashboard.log"
            lines = []
            if log_file.exists():
                try:
                    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
                except Exception:
                    lines = []
            report = [
                "SMART NETWORK — EVENT REPORT",
                "Generated: " + now(),
                "Records: " + str(len(lines)),
                "",
                "NOTE: This report contains backend log events only.",
                "It does not represent completed FTP transfers, storage write/read-back, SHA-256 verification, or failover tests.",
                "",
                "EVENTS",
                "-------",
            ] + (lines if lines else ["No backend events recorded."])
            body = ("\n".join(report) + "\n").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="SmartNetwork_Event_Report.txt"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path in ("/", "/index.html"):
            if not HTML_FILE.exists():
                self.send_error(404, "index.html not found")
                return
            html = HTML_FILE.read_text(encoding="utf-8")

            # Dynamic brand, world clock and weather panel.
            html = html.replace(
                '<div class="logo">S</div>',
                '<div class="logo sn-logo">S</div>',
                1
            )
            html = html.replace(
                '  <div class="nav">',
                BRAND_PANEL + '\n  <div class="nav">',
                1
            )

            # Live KPI targets.
            html = html.replace(
                '<div class="kpi-value accent-cyan kpi-pending">PENDING</div>',
                '<div id="kpi-health" class="kpi-value accent-cyan kpi-pending">PENDING</div>',
                1
            )
            html = html.replace(
                '<div class="kpi-value accent-blue kpi-pending">PENDING</div>',
                '<div id="kpi-links" class="kpi-value accent-blue kpi-pending">PENDING</div>',
                1
            )
            html = html.replace(
                '<div class="kpi-value accent-green kpi-pending">PENDING</div>',
                '<div id="kpi-storage" class="kpi-value accent-green kpi-pending">PENDING</div>',
                1
            )
            html = html.replace(
                '<div class="kpi-value accent-orange kpi-pending">PENDING</div>',
                '<div id="kpi-failed" class="kpi-value accent-orange kpi-pending">PENDING</div>',
                1
            )

            html = html.replace(
                '<div class="footer">SMART NETWORK · CONNECTIVITY & STORAGE DIAGNOSTIC DASHBOARD · UI SKELETON</div>',
                '',
                1
            )
            # Hardware and Live Telemetry panels belong to Overview only.
            html = html.replace(
                '</section>\n\n<section class="page" id="network">',
                HARDWARE_PANEL + LIVE_PANEL + '\n</section>\n\n<section class="page" id="network">',
                1
            )
            footer = (
                '<div class="footer" style="text-align:center;padding:22px 12px;line-height:1.9">'
                'SMART NETWORK · CONNECTIVITY & STORAGE DIAGNOSTIC DASHBOARD<br>'
                'Developed by Saiful Quadir<br>'
                '© 2026 · All Rights Reserved<br>'
                'Smart Network Dashboard · Version 1.1'
                '</div>'
            )
            # Dashboard footer only; overview panels are injected within Overview.
            html = html.replace("</main>", footer + "\n</main>", 1)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/recovery/safe":
            try:
                result = run_safe_recovery()
                self.send_json(result, 200)
            except Exception as exc:
                log_event(f"Stage 2 safe recovery error: {exc}")
                self.send_json({
                    "ok": False,
                    "error": str(exc),
                    "timestamp": now()
                }, 500)
            return

        if path == "/api/storage/test-f":
            try:
                result = run_f_storage_test()
                self.send_json(result, 200 if result["ok"] else 500)
            except Exception as exc:
                self.send_json({
                    "ok": False,
                    "error": str(exc),
                    "timestamp": now()
                }, 500)
            return

        if path != "/api/screenshot":
            self.send_error(404)
            return
        SCREENSHOT_DIR.mkdir(exist_ok=True)
        filename = "SmartNetwork_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".png"
        destination = SCREENSHOT_DIR / filename
        _, error, code = run_command(
            ["screencapture", "-x", str(destination)], timeout=10
        )
        if code == 0 and destination.exists():
            log_event(f"Screenshot captured: {filename}")
            self.send_json({"ok": True, "file": str(destination)})
        else:
            self.send_json({"ok": False, "error": error or "Screenshot failed"}, 500)



BRAND_PANEL = r"""
<style>
/* Smart Network brand and sidebar enhancement */
.logo.sn-logo{
  color:#07172b!important;
  background:linear-gradient(135deg,#62f4ff,#31aaff 55%,#8a7cff)!important;
  border:1px solid #8df7ff;
  box-shadow:0 0 8px #38dfff,0 0 20px #20bfff99,0 0 34px #397bff55;
  animation:snLogoPulse 2.8s ease-in-out infinite;
}
@keyframes snLogoPulse{
  0%,100%{box-shadow:0 0 7px #38dfff,0 0 15px #20bfff66;filter:brightness(1)}
  50%{box-shadow:0 0 13px #38dfff,0 0 30px #20bfffcc,0 0 42px #397bff88;filter:brightness(1.22)}
}
.sidebar .brand strong{color:#e4faff!important;text-shadow:0 0 9px #39dfff55}
.sidebar .brand small{color:#61dfff!important}
.nav-label{color:#55cfff!important;letter-spacing:1.5px}
.nav button{color:#b9dcf4!important;transition:all .2s ease}
.nav button:hover,.nav button.active{
  color:#65f0ff!important;
  background:linear-gradient(90deg,#16385a,#1b3150)!important;
  text-shadow:0 0 9px #42dfff99;
}
.nav button.active{box-shadow:inset 3px 0 #4deaff,0 0 12px #1b9dff22!important}
.sidebar-foot{color:#8fc9e8!important;line-height:1.8}
#sn-world{margin:12px 0 16px;padding:12px 10px;border:1px solid #24476a;
border-radius:12px;background:linear-gradient(145deg,#122541,#0d1b31);
box-shadow:inset 0 0 18px #1c6caa18}
#sn-world .sn-world-title{font-size:10px;letter-spacing:1.5px;color:#54ddff;font-weight:800;margin-bottom:9px}
#sn-world .sn-city{font-size:10px;color:#8dbbdc;letter-spacing:1px}
#sn-world .sn-time{font-size:20px;font-weight:750;color:#e2faff;text-shadow:0 0 10px #36cfff55;font-variant-numeric:tabular-nums}
#sn-world .sn-date{font-size:10px;color:#8eb6d2;margin-bottom:8px}
#sn-world .sn-weather{font-size:11px;color:#bdeaff;display:flex;justify-content:space-between;gap:6px}
#sn-world .sn-weather strong{color:#6ceaff}
.footer{color:#91bcd8!important;line-height:1.9}
@media(max-width:720px){#sn-world{max-width:420px}}
</style>
<div id="sn-world">
 <div class="sn-world-title">WORLD CLOCK & WEATHER</div>
 <div class="sn-city">TORONTO · CANADA</div>
 <div class="sn-time" id="sn-toronto-time">--:--:--</div>
 <div class="sn-date" id="sn-toronto-date">Loading…</div>
 <div class="sn-weather"><span>Weather</span><strong id="sn-toronto-weather">Loading…</strong></div>
 <div style="height:1px;background:#284565;margin:10px 0"></div>
 <div class="sn-city">DHAKA · BANGLADESH</div>
 <div class="sn-time" id="sn-dhaka-time">--:--:--</div>
 <div class="sn-date" id="sn-dhaka-date">Loading…</div>
 <div class="sn-weather"><span>Weather</span><strong id="sn-dhaka-weather">Loading…</strong></div>
 <div style="font-size:9px;color:#7399b8;margin-top:9px">Live local clocks · Weather via Open-Meteo</div>
</div>
<script>
(()=>{
 const fmt=(zone,options)=>new Intl.DateTimeFormat('en-CA',{timeZone:zone,...options});
 function clock(){
  for(const [city,zone] of [['toronto','America/Toronto'],['dhaka','Asia/Dhaka']]){
   const t=document.getElementById('sn-'+city+'-time');
   const d=document.getElementById('sn-'+city+'-date');
   if(t)t.textContent=fmt(zone,{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:true}).format(new Date());
   if(d)d.textContent=fmt(zone,{weekday:'short',year:'numeric',month:'short',day:'numeric'}).format(new Date());
  }
 }
 async function weather(city,lat,lon){
  const el=document.getElementById('sn-'+city+'-weather');
  try{
   const url=`https://api.open-meteo.com/v1/forecast?latitude=${lat}&longitude=${lon}&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m&timezone=auto`;
   const r=await fetch(url); if(!r.ok)throw new Error('Weather unavailable');
   const d=await r.json(),c=d.current;
   const labels={0:'Clear',1:'Mostly clear',2:'Partly cloudy',3:'Cloudy',45:'Fog',48:'Fog',51:'Drizzle',53:'Drizzle',55:'Drizzle',61:'Rain',63:'Rain',65:'Heavy rain',71:'Snow',73:'Snow',75:'Heavy snow',80:'Showers',81:'Showers',82:'Heavy showers',95:'Thunderstorm',96:'Thunderstorm',99:'Thunderstorm'};
   el.textContent=`${Math.round(c.temperature_2m)}°C · ${labels[c.weather_code]||'Current conditions'}`;
  }catch(e){el.textContent='Unavailable';}
 }
 clock();setInterval(clock,1000);
 weather('toronto',43.6532,-79.3832);
 weather('dhaka',23.8103,90.4125);
 setInterval(()=>{weather('toronto',43.6532,-79.3832);weather('dhaka',23.8103,90.4125)},600000);
})();
</script>
"""


HARDWARE_PANEL = r"""
<section id="hardware-telemetry" class="section" style="margin-top:24px">
  <div class="section-head">
    <div>
      <div class="eyebrow">LIVE HARDWARE DIAGNOSTICS</div>
      <h2>Router & Hardware Status</h2>
    </div>
    <span class="status-pill">LIVE</span>
  </div>
  <div id="hardware-grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px">
    <div class="card">Loading hardware diagnostics…</div>
  </div>
  <style>
    #hardware-telemetry .hw-card{border:1px solid rgba(130,160,190,.2);border-radius:12px;padding:16px}
    #hardware-telemetry .hw-title{font-weight:700;margin-bottom:10px}
    #hardware-telemetry .hw-line{font-size:12px;line-height:1.8;overflow-wrap:anywhere}
    #hardware-telemetry .hw-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px;animation:hwPulse 1.5s infinite}
    #hardware-telemetry .hw-green{background:#20d68a;box-shadow:0 0 9px #20d68a}
    #hardware-telemetry .hw-yellow{background:#f4bd4f;box-shadow:0 0 9px #f4bd4f}
    #hardware-telemetry .hw-red{background:#ff5e68;box-shadow:0 0 9px #ff5e68}
    @keyframes hwPulse{50%{opacity:.45}}
  </style>
  <script>
  (function(){
    const grid=document.getElementById('hardware-grid');
    const esc=v=>String(v??'Not Available').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const card=(title,ok,lines,unknown=false)=>{
      const state=unknown?'hw-yellow':(ok?'hw-green':'hw-red');
      const label=unknown?'PENDING / UNKNOWN':(ok?'OK':'FAILED');
      return `<div class="hw-card"><div class="hw-title"><span class="hw-dot ${state}"></span>${esc(title)} · ${label}</div>${lines.map(x=>`<div class="hw-line">${esc(x)}</div>`).join('')}</div>`;
    };
    async function refresh(){
      try{
        const r=await fetch('/api/live',{cache:'no-store'});
        if(!r.ok) throw new Error('API HTTP '+r.status);
        const d=await r.json();
        const router=d.router||{}, mac=d.mac_hardware||{};
        const wifi=d.wifi||{}, eth=d.ethernet||{};
        const items=[
          card('Router / Default Gateway',!!(router.ping&&router.ping.ok),[
            'Gateway: '+(router.gateway||'Not detected'),
            'Ping: '+(router.ping&&router.ping.ok?'Reachable':'Not reachable / not verified'),
            'MAC: '+(router.mac_address||'Not available in ARP cache'),
            'CPU / Temperature / Firmware: Not available without router management access'
          ],!router.gateway||typeof router.ping?.ok!=='boolean'),
          card('Wi-Fi Adapter',wifi.status==='UP',[
            'Interface: '+(wifi.device||'Not available'),
            'IP: '+(wifi.ip||'Not assigned'),
            'Status: '+(wifi.status||'Unknown'),
            'MAC: '+(wifi.mac||'Not available')
          ],!wifi.device||!['UP','DOWN'].includes(wifi.status)),
          card('USB Ethernet Adapter',eth.status==='UP',[
            'Interface: '+(eth.device||'Not available'),
            'IP: '+(eth.ip||'Not assigned'),
            'Status: '+(eth.status||'Unknown'),
            'MAC: '+(eth.mac||'Not available'),
            'Media: '+(eth.media||'Not reported')
          ],!eth.device||!['UP','DOWN'].includes(eth.status)),
          card('Mac System Hardware',!!(mac.model||mac.chip),[
            'Model: '+(mac.model||'Not available'),
            'Model Identifier: '+(mac.model_identifier||'Not available'),
            'Chip / Processor: '+(mac.chip||'Not available'),
            'Memory: '+(mac.memory||'Not available')
          ],!mac.model&&!mac.chip)
        ];
        const usb=d.usb_hardware||{};
        items.push(card('USB / Dock / External Displays',
          !!usb.available,[
            'USB devices: '+((usb.usb_devices||[]).join(', ')||'None detected / unavailable'),
            'Thunderbolt devices: '+((usb.thunderbolt_devices||[]).join(', ')||'None detected / unavailable'),
            'External displays reported: '+((usb.displays||[]).filter(x=>!/^Color LCD$/i.test(x)&&!/^spdisplays_/i.test(x)&&!/^kHW_/i.test(x)&&!/^[0-9]{2}T[0-9]{2}$/i.test(x)).join(', ')||'None identified by macOS inventory'),
            usb.note||'Inventory only · Physical USB/Thunderbolt port health is not verified'
          ],true));
        grid.innerHTML=items.join('');
      }catch(e){
        grid.textContent='Hardware diagnostics unavailable: '+e.message;
      }
    }
    refresh();
    setInterval(refresh,10000);
  })();
  </script>
</section>
"""

LIVE_PANEL = r"""
<style>
#sn-live{margin:18px 0 24px;padding:18px;border:1px solid #385078;border-radius:16px;
background:linear-gradient(120deg,#172744,#152039 55%,#202b48);color:#edf3ff}
#sn-live .sn-head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
#sn-live h2{margin:0 0 5px;font-size:19px}
#sn-live .sn-muted{color:#a9bad5;font-size:12px}
#sn-live .sn-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px;margin-top:14px}
#sn-live .sn-card{background:#0e192d;border:1px solid #314362;border-radius:12px;padding:12px;min-width:0}
#sn-live .sn-card b{display:block;margin-bottom:7px}
#sn-live .sn-value{font-size:13px;line-height:1.7;overflow-wrap:anywhere}
#sn-live .sn-good{color:#64e6ae} #sn-live .sn-bad{color:#ff8585} #sn-live .sn-warn{color:#ffd27a}
#sn-live button{background:#3478ef;color:white;border:0;border-radius:8px;padding:9px 12px;cursor:pointer;font-weight:600}
#sn-live button:disabled{opacity:.5}
</style>
<div id="sn-live">
 <div class="sn-head">
  <div><h2>LIVE SYSTEM TELEMETRY</h2>
   <div class="sn-muted">Real-time macOS discovery · Read-only diagnostic mode</div></div>
  <div style="display:flex;gap:8px;flex-wrap:wrap">
   <button id="sn-refresh">Refresh Now</button>
   <button id="sn-shot">Capture Screenshot</button>
  </div>
 </div>
 <div id="sn-updated" class="sn-muted" style="margin-top:9px">Collecting live system data…</div>
 <div id="sn-alerts" class="sn-muted" style="margin-top:10px;padding:10px;border:1px solid #53627a;border-radius:9px">Establishing monitoring baseline…</div>
 <div id="sn-grid" class="sn-grid"></div>
 <div id="sn-note" class="sn-muted" style="margin-top:12px">
  Drive write/read tests are intentionally disabled until share paths and permissions are verified.
 </div>
</div>
<script>
(()=>{
 const grid=document.getElementById('sn-grid');
 const updated=document.getElementById('sn-updated');
 const esc=s=>String(s??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const card=(title,lines,cls='')=>`<div class="sn-card"><b>${esc(title)}</b><div class="sn-value ${cls}">${lines.map(esc).join('<br>')}</div></div>`;
 const state=x=>x===true?'ONLINE':(x===false?'OFFLINE / FAILED':'PENDING / UNKNOWN');
 let previousStates=null;
 const alertHistory=[];
 let fTestResult=null;
 let fTestRunning=false;

 const fTestResultMarkup=()=>{
  if(!fTestResult) return '<div class="empty">F: test not run yet. No transaction result is assumed.</div>';
  const d=fTestResult;
  const row=(label,value)=>`<div class="setting-row"><span>${esc(label)}</span><span>${esc(value??'—')}</span></div>`;
  return `<div class="card" style="margin-top:12px">
   ${row('Overall result',d.ok?'PASS':'FAIL')}
   ${row('Integrity',d.integrity)}
   ${row('File size',d.size_bytes+' bytes')}
   ${row('Returned size',d.returned_size_bytes??'—')}
   ${row('Upload speed',d.upload_speed_mbps==null?'—':d.upload_speed_mbps+' MiB/s')}
   ${row('Download speed',d.download_speed_mbps==null?'—':d.download_speed_mbps+' MiB/s')}
   ${row('Upload duration',d.upload_seconds==null?'—':d.upload_seconds+' sec')}
   ${row('Download duration',d.download_seconds==null?'—':d.download_seconds+' sec')}
   ${row('Original SHA-256',d.original_sha256)}
   ${row('Returned SHA-256',d.returned_sha256)}
   ${row('Size verification',d.size_verified?'PASS':'FAIL')}
   ${row('SHA-256 verification',d.sha256_verified?'PASS':'FAIL')}
   ${row('Test cleanup',d.cleanup)}
   ${row('Test ID',d.test_id)}
   ${d.error?row('Error',d.error):''}
  </div>`;
 };
 async function refresh(){
  updated.textContent='Refreshing live diagnostics…';
  document.getElementById('sn-refresh').disabled=true;
  try{
   const r=await fetch('/api/live',{cache:'no-store'}); const d=await r.json();
   const e=d.ethernet||{};
   const vols=d.storage?.volumes||[];
   if(!r.ok) throw new Error(d.error||'Backend error');

   const targetList=Object.values(d.targets||{});
   const activeLinks=[d.wifi,d.ethernet].filter(x=>x&&x.status==='UP').length;
   const smbMounts=d.storage?.smb_mounts||[];
   const mounted=smbMounts.length;

   const volumeNames=new Set(vols.map(v=>v.name));
   const paths=Object.values(d.targets||{});
   const interfaceState=x=>x?.status==='UP'?true:(x?.status==='DOWN'?false:null);
   const testState=x=>typeof x?.ok==='boolean'?x.ok:null;
   const allTestState=(list,key)=>{
    if(!list.length)return null;
    const values=list.map(p=>testState(p[key]));
    if(values.some(v=>v===null))return null;
    return values.every(Boolean);
   };
   const currentStates={
    'Mac Wi-Fi':interfaceState(d.wifi),
    'Mac Ethernet':interfaceState(d.ethernet),
    'Windows Wi-Fi path':testState(paths[0]?.ping),
    'Windows Ethernet path':testState(paths[1]?.ping),
    'FTP control service':allTestState(paths,'ftp_tcp'),
    'SMB service':allTestState(paths,'smb_tcp'),
    'Windows C share':volumeNames.has('Windows_C_Drive')?true:null,
    'Windows D share':volumeNames.has('Windows_D_ADATA')?true:null,
    'Windows F share':volumeNames.has('Windows_F_Transcend')?true:null,
    'Windows G share':volumeNames.has('Windows_G_Transcend')?true:null
   };

   const hadBaseline=previousStates!==null;
   if(hadBaseline){
    for(const [name,ok] of Object.entries(currentStates)){
     if(previousStates[name]===true&&ok===false)
      alertHistory.unshift('DISCONNECTED: '+name);
     if(previousStates[name]===false&&ok===true)
      alertHistory.unshift('RECOVERED: '+name);
    }
    alertHistory.splice(10);
   }
   previousStates=currentStates;

   const alertBox=document.getElementById('sn-alerts');
   if(alertBox){
    alertBox.textContent=alertHistory.length?
      'RECENT CONNECTION EVENTS · '+alertHistory.join(' | '):
      'Monitoring active · '+(hadBaseline?'No new verified connection changes':'Initial baseline established')+' · Session-only alerts; not permanently stored.';
    alertBox.style.color=alertHistory.some(x=>x.startsWith('DISCONNECTED'))?'#ff8585':
      (alertHistory.length?'#ffd27a':'#64e6ae');
   }

   const pingState=x=>{
    const loss=x?.packet_loss_percent;
    if(typeof loss==='number')return loss===0;
    return testState(x);
   };
   const pingWarning=x=>{
    const loss=x?.packet_loss_percent;
    return typeof loss==='number'&&loss>0&&loss<100;
   };
   const pingChecks=[d.internet_ping,...targetList.map(x=>x.ping)];
   const checks=[
    ...pingChecks.map(pingState),
    ...targetList.map(x=>testState(x.ftp_tcp)),
    ...targetList.map(x=>testState(x.smb_tcp))
   ];
   const knownChecks=checks.filter(x=>x!==null);
   const passed=knownChecks.filter(x=>x===true).length;
   const unsuccessful=knownChecks.filter(x=>x===false).length;
   const warnings=pingChecks.filter(pingWarning).length;
   const hardFailures=unsuccessful-warnings;
   const unknown=checks.length-knownChecks.length;
   const health=checks.length?Math.round(passed/checks.length*100):0;
   const setKpi=(id,value)=>{const el=document.getElementById(id);if(el)el.textContent=value;};
   const updateKpiColor=(id,value,state)=>{
    const el=document.getElementById(id);
    if(!el)return;
    el.textContent=value;
    el.className='kpi-value'+(state==='pending'?' kpi-pending':'');
    el.style.color=state==='good'?'#45dfa7':
      (state==='bad'?'#ff8585':'');
   };

   const healthState=!knownChecks.length?'pending':
    (unsuccessful>0?'bad':
     (unknown===0&&health===100?'good':'pending'));

   const linkStates=[d.wifi,d.ethernet]
    .map(x=>x?.status)
    .filter(x=>x==='UP'||x==='DOWN');

   const linksState=linkStates.includes('DOWN')?'bad':
    (linkStates.length===2&&activeLinks===2?'good':'pending');

   const failedState=!knownChecks.length?'pending':
    (unsuccessful>0?'bad':
     (unknown===0?'good':'pending'));

   updateKpiColor('kpi-health',
    knownChecks.length?health+'%':'PENDING',healthState);

   updateKpiColor('kpi-links',
    activeLinks+'/2',linksState);

   setKpi('kpi-storage',mounted+'/5');

   updateKpiColor('kpi-failed',
    knownChecks.length?String(unsuccessful):'PENDING',failedState);
   const notes=document.querySelectorAll('.kpi-note');
   if(notes.length>=4){
    notes[0].textContent=knownChecks.length?
     passed+'/'+checks.length+' PASS · '+warnings+' WARNING · '+hardFailures+' FAIL · '+unknown+' UNKNOWN':
     'Waiting for verified diagnostic results';
    notes[1].textContent='Active Wi-Fi / Ethernet interfaces';
    notes[2].textContent='Mounted Windows shares · E pending';
    notes[3].textContent='Warnings + failed current checks';
   }
   const paint=(id,label,ok,unknown=false)=>{
    const el=document.getElementById(id);
    if(!el)return;
    el.textContent=label;
    el.className='status '+(unknown?'pending':(ok?'good':'bad'));
 el.style.color='';
   };

   const w=d.wifi||{};
   paint('ov-wifi',w.status||'UNKNOWN',w.status==='UP',!w.status);
   paint('ov-ethernet',e.status||'UNKNOWN',e.status==='UP',!e.status);

   const ftpOk=paths.filter(p=>p.ftp_tcp?.ok).length;
   const smbOk=paths.filter(p=>p.smb_tcp?.ok).length;
   paint('ov-ftp',paths.length?ftpOk+'/'+paths.length+' PATHS OK':'UNKNOWN',
         paths.length>0&&ftpOk===paths.length,paths.length===0);
   paint('ov-smb',paths.length?smbOk+'/'+paths.length+' PATHS OK':'UNKNOWN',
         paths.length>0&&smbOk===paths.length,paths.length===0);

   const driveNames={
    'ov-drive-c':'Windows_C_Drive',
    'ov-drive-d':'Windows_D_ADATA',
    'ov-drive-f':'Windows_F_Transcend',
    'ov-drive-g':'Windows_G_Transcend'
   };
   for(const [id,name] of Object.entries(driveNames)){
    const found=volumeNames.has(name);
    paint(id,found?'MOUNTED':'NOT DETECTED · PENDING',found,!found);

    const volume=vols.find(v=>v.name===name);
    const cap=volume?.capacity;
    const card=document.getElementById(id)?.closest('.drive');
    const info=card?.querySelector('.capacity-info');
    const bar=card?.querySelector('.progress span');

    if(info&&bar){
      if(cap&&Number.isFinite(cap.total_bytes)&&cap.total_bytes>0&&
         Number.isFinite(cap.used_bytes)&&Number.isFinite(cap.free_bytes)&&
         Number.isFinite(cap.used_percent)){
        const gb=n=>(n/1024**3).toFixed(1)+' GB';
        const pct=Math.max(0,Math.min(100,cap.used_percent));
        const level=pct<70?'capacity-good':(pct<90?'capacity-warning':'capacity-critical');
        info.textContent='Free '+gb(cap.free_bytes)+' / Total '+gb(cap.total_bytes)+
          ' · Used '+gb(cap.used_bytes)+' ('+pct.toFixed(1)+'%)';
        bar.style.width=pct+'%';
        bar.className=level;
      }else{
        info.textContent='Capacity: Unavailable';
        bar.style.width='0%';
        bar.className='';
      }
    }
  }

   const recent=document.getElementById('recent');
   if(recent){
    recent.textContent='Live diagnostics active · Last refreshed: '+
      new Date(d.timestamp).toLocaleString()+
      ' · No historical event log is claimed.';
   }

   let out=[];
   out.push(card('Wi-Fi · '+(w.device||'Not detected'),[
    'Status: '+(w.status||'UNKNOWN'),
    'IPv4: '+(w.ip||'Not assigned'),
    'Gateway: '+(d.default_gateway||'Not detected'),
    'Internet ping: '+state(d.internet_ping?.ok),
    'Latency: '+(d.internet_ping?.average_ms??'—')+' ms'
   ],d.internet_ping?.ok?'sn-good':'sn-warn'));
   out.push(card('Ethernet · '+(e.device||'Not detected'),[
    'Status: '+(e.status||'UNKNOWN'),
    'IPv4: '+(e.ip||'Not assigned'),
    'Link: '+(e.media||'Not reported'),
    'MAC: '+(e.mac||'—')
   ],e.status==='UP'?'sn-good':'sn-warn'));
   for(const [name,p] of Object.entries(d.targets||{})){
    out.push(card(name+' · '+p.address,[
     'Route: '+(p.route_interface||'Not found'),
     'Ping: '+state(p.ping?.ok)+' · '+(p.ping?.average_ms??'—')+' ms',
     'FTP TCP 21: '+state(p.ftp_tcp?.ok),
     'FTP banner: '+(p.ftp_banner?.banner||'No response'),
     'SMB TCP 445: '+state(p.smb_tcp?.ok)
    ],p.ping?.ok?'sn-good':'sn-warn'));
   }
   out.push(card('Windows E Drive',[
    'Status: NOT DETECTED · PENDING',
    'The current mounted-volume inventory does not verify whether the server E Drive exists.',
    'No disconnected or failed status is assumed.'
   ],'sn-warn'));
   out.push(card('Mounted Storage · '+vols.length+' volumes',
    vols.length?vols.map(v=>v.name+' · '+(v.readable?'Readable':'Not readable')+' · OS writable flag: '+(v.writable?'Yes':'No')):['No mounted volumes detected']));
   out.push(card('Safety & Backend',[
    'Mode: '+d.safety.mode,
    'File transactions: DISABLED',
    'Automatic cleanup: DISABLED',
    'Host: '+d.host
   ],'sn-warn'));
   
    // Live Network Diagnostics page.
    const networkPage=document.getElementById('network');
    if(networkPage){
      const statusBadge=(label,ok)=>{
        const color=ok===true?'#20c987':(ok===false?'#ff626e':'#ffd27a');
        return `<span style="font-weight:700;color:${color}">${esc(label)}</span>`;
      };

      const serviceRows=Object.entries(d.targets||{}).map(([name,p])=>`
        <tr>
          <td>${esc(name)}</td>
          <td>${statusBadge(state(p.ping?.ok),p.ping?.ok)}</td>
          <td>${statusBadge(state(p.ftp_tcp?.ok),p.ftp_tcp?.ok)}</td>
          <td>${statusBadge(state(p.smb_tcp?.ok),p.smb_tcp?.ok)}</td>
        </tr>`).join('');

      networkPage.innerHTML=`
        <div class="section-head">
          <div><h2>Network Diagnostics</h2>
          <p>Live interface, route and service reachability</p></div>
          ${statusBadge('LIVE DATA',true)}
        </div>
        <div class="grid two">
          <div class="card">
            <h3>Wi-Fi Diagnostics</h3>
            <div class="setting-row"><span>Interface</span><strong>${esc(w.device||'Unknown')}</strong></div>
            <div class="setting-row"><span>Status</span>${statusBadge(w.status||'PENDING',w.status==='UP'?true:(w.status==='DOWN'?false:null))}</div>
            <div class="setting-row"><span>IP Address</span><span>${esc(w.ip||'Not assigned')}</span></div>
            <div class="setting-row"><span>Gateway</span><span>${esc(d.default_gateway||'Not detected')}</span></div>
            <div class="setting-row"><span>MAC Address</span><span>${esc(w.mac||'Not reported')}</span></div>
            <div class="setting-row"><span>Internet Ping</span>${statusBadge(state(d.internet_ping?.ok),d.internet_ping?.ok)}</div>
            <div class="setting-row"><span>Latency</span><span>${esc(d.internet_ping?.average_ms??'—')} ms</span></div>
          </div>
          <div class="card">
            <h3>Ethernet Diagnostics</h3>
            <div class="setting-row"><span>Interface</span><strong>${esc(e.device||'Unknown')}</strong></div>
            <div class="setting-row"><span>Status</span>${statusBadge(e.status||'PENDING',e.status==='UP'?true:(e.status==='DOWN'?false:null))}</div>
            <div class="setting-row"><span>IP Address</span><span>${esc(e.ip||'Not assigned')}</span></div>
            <div class="setting-row"><span>Adapter / Link</span><span>${esc(e.media||'Not reported')}</span></div>
            <div class="setting-row"><span>MAC Address</span><span>${esc(e.mac||'Not reported')}</span></div>
            <div class="setting-row"><span>Default Gateway</span><span>${esc(d.default_gateway||'Not detected')}</span></div>
          </div>
        </div>
        <div class="section-head"><div><h2>Windows Server Service Reachability</h2>
          <p>Current ping and TCP port checks for each network path</p></div></div>
        <div class="card">
          <div class="table-wrap"><table>
            <thead><tr><th>NETWORK PATH</th><th>PING</th><th>FTP TCP 21</th><th>SMB TCP 445</th></tr></thead>
            <tbody>${serviceRows||'<tr><td colspan="4">No target results available</td></tr>'}</tbody>
          </table></div>
        </div>
        <div class="section-head"><div><h2>Additional Tests</h2>
          <p>Not reported as successful until actual tests are implemented</p></div></div>
        <div class="card">
          <div class="setting-row"><span>FTP authenticated session / file transfer</span>${statusBadge('PENDING · NOT TESTED',null)}</div>
          <div class="setting-row"><span>Passive FTP data channel</span>${statusBadge('PENDING · NOT TESTED',null)}</div>
          <div class="setting-row"><span>Continuous stability / failover / recovery</span>${statusBadge('PENDING · NOT TESTED',null)}</div>
        </div>`;
    }

    // Live storage discovery only. No file writes or cleanup.
    const storagePage=document.getElementById('storage');
    if(storagePage){
      const driveTargets=[
        ['C','Windows_C_Drive','💻','Local Drive','Windows system drive'],
        ['D','Windows_D_ADATA','📱','Portable Drive','ADATA storage target'],
        ['E',null,'❔','Unknown','Server E Drive existence not independently verified'],
        ['F','Windows_F_Transcend','💾','Pen Drive','Transcend storage target'],
        ['G','Windows_G_Transcend','💾','Pen Drive','Transcend storage target']
      ];
      const driveCards=driveTargets.map(([letter,volumeName,icon,driveType,description])=>{
        const volume=volumeName?vols.find(v=>v.name===volumeName):null;
        const mounted=Boolean(volume);
        const label=mounted?'MOUNTED':'NOT DETECTED · PENDING';
        const cap=volume?.capacity;
        const capacityAvailable=cap&&Number.isFinite(cap.total_bytes)&&cap.total_bytes>0&&
          Number.isFinite(cap.used_bytes)&&Number.isFinite(cap.free_bytes)&&
          Number.isFinite(cap.used_percent);
        const capacityMarkup=capacityAvailable?(()=>{
          const gb=n=>(n/1024**3).toFixed(1)+' GB';
          const pct=Math.max(0,Math.min(100,cap.used_percent));
          const level=pct<70?'capacity-good':(pct<90?'capacity-warning':'capacity-critical');
          return `<div class="capacity-info" style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap">
            <span>Free: ${gb(cap.free_bytes)}</span><span>Total: ${gb(cap.total_bytes)}</span>
          </div>
          <div class="progress"><span class="${level}" style="width:${pct}%"></span></div>`;
        })():'<div class="capacity-info">Capacity: Unavailable · Pending</div><div class="progress"><span style="width:0%"></span></div>';
        const details=volume
          ? `Mount: ${esc(volume.path||volume.name)}<br>Readable: ${volume.readable?'Yes':'No'}<br>OS writable flag: ${volume.writable?'Yes':'No'}`
          : 'No verified mounted-volume match. No disconnected or failed status is assumed.';
        return `<div class="drive">
          <div class="drive-top"><h3>Drive ${letter}: · ${driveType}</h3><span style="font-size:23px" aria-label="${driveType}">${icon}</span></div>
          <p>${esc(description)}</p>
          <span class="status ${mounted?'good':'pending'}">${label}</span>
          ${capacityMarkup}
          <div class="panel-note" style="margin-top:10px;overflow-wrap:anywhere">${details}</div>
        </div>`;
      }).join('');

      const mountedRows=vols.map(v=>`
        <tr><td>${esc(v.name)}</td><td>${esc(v.path||'—')}</td>
        <td>${v.readable?'Readable':'Not readable'}</td>
        <td>${v.writable?'Yes':'No'}</td></tr>`).join('');

      storagePage.innerHTML=`
        <div class="section-head">
          <div><h2>Storage Transaction Center</h2>
          <p>Live mounted-volume discovery · No file transactions executed</p></div>
          <span class="status" style="color:#ffd27a">READ-ONLY · TRANSACTIONS DISABLED</span>
        </div>
        <div class="card">
          <div style="display:flex;align-items:center;gap:9px;margin-bottom:12px">
            <h3 style="margin:0">Transaction Workflow</h3>
            <button type="button"
              onclick="document.getElementById('transaction-info-modal').style.display='flex'"
              aria-label="Transaction Workflow information"
              title="About Transaction Workflow"
              style="width:23px;height:23px;min-width:23px;border-radius:50%;border:1px solid #55c9ed;background:#142d43;color:#70dafa;font-size:13px;font-weight:700;cursor:pointer;padding:0;display:inline-flex;align-items:center;justify-content:center">i</button>
          </div>

          <div id="transaction-info-modal"
            onclick="if(event.target===this)this.style.display='none'"
            style="display:none;position:fixed;inset:0;z-index:9999;background:rgba(3,10,22,.78);align-items:center;justify-content:center;padding:20px">
            <div role="dialog" aria-modal="true" aria-labelledby="transaction-info-title"
              style="position:relative;width:100%;max-width:560px;max-height:85vh;overflow-y:auto;background:#101d32;border:1px solid #31516d;border-radius:14px;padding:24px;color:#e8f2ff;box-shadow:0 18px 60px rgba(0,0,0,.5)">
              <button type="button"
                onclick="document.getElementById('transaction-info-modal').style.display='none'"
                aria-label="Close information"
                title="Close"
                style="position:absolute;top:12px;right:14px;width:30px;height:30px;border:0;border-radius:7px;background:#263950;color:#dcecff;font-size:23px;line-height:1;cursor:pointer">&times;</button>

              <h3 id="transaction-info-title" style="margin:0 38px 8px 0;color:#70dafa">Transaction Workflow</h3>
              <p style="color:#b9cde0;font-size:13px;line-height:1.6;margin:0 0 18px">
                This workflow is designed to verify end-to-end file transfer reliability between the Mac and a selected storage drive.
              </p>

              <div style="display:grid;gap:13px">
                <div>
                  <strong style="color:#ffffff">1. Create a unique test file</strong>
                  <p style="margin:4px 0 0;color:#b9cde0;font-size:12px;line-height:1.55">Create a uniquely named file specifically for the test.</p>
                </div>
                <div>
                  <strong style="color:#ffffff">2. Transfer the file</strong>
                  <p style="margin:4px 0 0;color:#b9cde0;font-size:12px;line-height:1.55">Send the test file to the selected, verified storage destination.</p>
                </div>
                <div>
                  <strong style="color:#ffffff">3. Read the file back</strong>
                  <p style="margin:4px 0 0;color:#b9cde0;font-size:12px;line-height:1.55">Retrieve the transferred file from the destination for verification.</p>
                </div>
                <div>
                  <strong style="color:#ffffff">4. Verify file integrity</strong>
                  <p style="margin:4px 0 0;color:#b9cde0;font-size:12px;line-height:1.55">Compare original and returned file sizes and SHA-256 hashes to check that the content is unchanged.</p>
                </div>
                <div>
                  <strong style="color:#ffffff">5. Clean up safely</strong>
                  <p style="margin:4px 0 0;color:#b9cde0;font-size:12px;line-height:1.55">Remove only the test files created by this application. Existing user files must never be deleted.</p>
                </div>
              </div>

              <div style="margin-top:19px;padding:12px;border:1px solid #806a37;border-radius:9px;background:rgba(255,210,122,.07)">
                <strong style="color:#ffd27a;font-size:12px">Current status: READ-ONLY</strong>
                <p style="margin:5px 0 0;color:#c8d5e4;font-size:12px;line-height:1.55">
                  Transaction execution is DISABLED and has NOT been tested. Automatic cleanup is also DISABLED. No files are created, transferred, or deleted by this workflow at present.
                </p>
              </div>
            </div>
          </div>
          <div class="setting-row"><span>Transaction execution</span><strong style="color:#ffd27a">DISABLED · NOT TESTED</strong></div>
          <div class="setting-row"><span>Automatic cleanup</span><strong>DISABLED</strong></div>
        </div>
        <div class="section-head"><div><h2>Target Drives</h2>
          <p>Mounted status is not proof of a successful write/read transaction</p></div></div>
        <div class="grid three">${driveCards}</div>
        <div class="section-head"><div><h2>Mounted Volume Inventory</h2>
          <p>${vols.length} volumes currently reported by macOS</p></div></div>
        <div class="card"><div class="table-wrap"><table>
          <thead><tr><th>VOLUME</th><th>MOUNT PATH</th><th>READ ACCESS</th><th>OS WRITABLE FLAG</th></tr></thead>
          <tbody>${mountedRows||'<tr><td colspan="4">No mounted volumes detected</td></tr>'}</tbody>
        </table></div></div>
        <div class="section-head"><div><h2>Transaction Results</h2>
          <p>Upload speed · Download speed · Duration · Integrity</p></div></div>
        <div class="card">
          <div class="setting-row">
            <span>Dedicated F: Transcend test · 256 KB</span>
            <button type="button" id="run-f-test" ${fTestRunning?'disabled':''}>${fTestRunning?'Testing…':'Run F: Test'}</button>
          </div>
          <div id="f-test-result">${fTestResultMarkup()}</div>
          <p class="panel-note">Only F: is tested. General file transactions and automatic cleanup remain disabled.</p>
        </div>`;
      const fTestButton=document.getElementById('run-f-test');
      if(fTestButton){
        fTestButton.addEventListener('click',async()=>{
          if(fTestRunning) return;
          fTestRunning=true;
          fTestButton.disabled=true;
          fTestButton.textContent='Testing…';
          fTestResult=null;
          const resultBox=document.getElementById('f-test-result');
          if(resultBox) resultBox.innerHTML='<div class="empty">Creating test file · Uploading · Reading back · Verifying SHA-256 · Cleaning up…</div>';
          try{
            const response=await fetch('/api/storage/test-f',{method:'POST',cache:'no-store'});
            const data=await response.json();
            fTestResult=data;
            if(!response.ok || !data.ok) throw new Error(data.error||data.cleanup||'F: test failed.');
          }catch(error){
            if(!fTestResult) fTestResult={ok:false,integrity:'NOT VERIFIED',cleanup:'NOT CONFIRMED',error:error.message};
            else if(!fTestResult.error) fTestResult.error=error.message;
          }finally{
            fTestRunning=false;
            const currentButton=document.getElementById('run-f-test');
            if(currentButton){currentButton.disabled=false;currentButton.textContent='Run F: Test';}
            const currentResult=document.getElementById('f-test-result');
            if(currentResult) currentResult.innerHTML=fTestResultMarkup();
          }
        });
      }
    }

    grid.innerHTML=out.join('');
   updated.textContent='Last updated: '+new Date(d.timestamp).toLocaleString()+' · Auto-refresh every 10 seconds';
  }catch(err){
   updated.textContent='Live data error: '+err.message;
   grid.innerHTML=card('Backend Status',['Unable to retrieve live data','Check the Python terminal for details'],'sn-bad');
  }finally{document.getElementById('sn-refresh').disabled=false;}
 }
 document.getElementById('sn-refresh').addEventListener('click',refresh);
 document.getElementById('sn-shot').addEventListener('click',async()=>{
  const b=document.getElementById('sn-shot'); b.disabled=true; b.textContent='Capturing…';
  try{
   const r=await fetch('/api/screenshot',{method:'POST'}); const d=await r.json();
   if(!r.ok||!d.ok) throw new Error(d.error||'Capture failed');
   b.textContent='Screenshot Saved ✓';
   document.getElementById('sn-note').textContent='Saved: '+d.file;
  }catch(e){b.textContent='Screenshot Failed';document.getElementById('sn-note').textContent=e.message;}
  setTimeout(()=>{b.textContent='Capture Screenshot';b.disabled=false;},2500);
 });
 refresh(); setInterval(refresh,10000);
})();
</script>
"""


# STAGE2_AUTOMATIC_SMB_MONITOR_V1
_SMB_MONITOR_INTERVAL = 30
_SMB_MONITOR_LOCK = threading.Lock()
_SMB_MONITOR_LAST_TRIGGER = 0.0
_SMB_MONITOR_STATE = {
    "enabled": True,
    "interval_seconds": _SMB_MONITOR_INTERVAL,
    "last_check": None,
    "service_ok": None,
    "process_ok": None,
    "daemon_ok": None,
    "pid": None,
    "process_state": None,
    "tcp_445_ok": None,
    "last_recovery": None,
    "last_recovery_status": None,
    "last_recovery_ok": None,
    "error": None
}
_SMB_MONITOR_THREAD = None


def get_smb_monitor_status():
    with _SMB_MONITOR_LOCK:
        return dict(_SMB_MONITOR_STATE)


def _smb_monitor_check():
    daemon = inspect_smb_daemon()
    port_result = tcp_test("127.0.0.1", SMB_PORT)
    port_ok = bool(port_result.get("ok"))

    return {
        "service_ok": daemon["service_ok"],
        "process_ok": daemon["process_ok"],
        "daemon_ok": daemon["ok"],
        "pid": daemon["pid"],
        "process_state": daemon["process_state"],
        "tcp_445_ok": port_ok,
        "service_detail": daemon["detail"],
        "port_detail": port_result
    }


def _smb_monitor_loop():
    global _SMB_MONITOR_LAST_TRIGGER

    log_event(
        "Stage 2 automatic SMB monitor started | "
        f"Interval={_SMB_MONITOR_INTERVAL}s"
    )

    while True:
        try:
            health = _smb_monitor_check()

            with _SMB_MONITOR_LOCK:
                _SMB_MONITOR_STATE.update({
                    "last_check": now(),
                    "service_ok": health["service_ok"],
                    "process_ok": health["process_ok"],
                    "daemon_ok": health["daemon_ok"],
                    "pid": health["pid"],
                    "process_state": health["process_state"],
                    "tcp_445_ok": health["tcp_445_ok"],
                    "error": None
                })

            if not health["daemon_ok"] or not health["tcp_445_ok"]:
                elapsed = time.monotonic() - _SMB_MONITOR_LAST_TRIGGER

                if elapsed >= _SMB_RECOVERY_COOLDOWN:
                    _SMB_MONITOR_LAST_TRIGGER = time.monotonic()

                    log_event(
                        "Stage 2 automatic monitor detected local SMB failure; "
                        "starting recovery workflow"
                    )

                    result = run_safe_recovery()
                    recovery = result.get("recovery_actions", {})

                    with _SMB_MONITOR_LOCK:
                        _SMB_MONITOR_STATE.update({
                            "last_recovery": now(),
                            "last_recovery_status": recovery.get("status"),
                            "last_recovery_ok": result.get("ok")
                        })

                    log_event(
                        "Stage 2 automatic monitor recovery result | "
                        f"Status={recovery.get('status')} | "
                        f"LocalOK={result.get('ok')}"
                    )

                else:
                    with _SMB_MONITOR_LOCK:
                        _SMB_MONITOR_STATE["error"] = (
                            "SMB unhealthy; recovery cooldown active"
                        )

        except Exception as exc:
            with _SMB_MONITOR_LOCK:
                _SMB_MONITOR_STATE.update({
                    "last_check": now(),
                    "error": str(exc)
                })
            log_event(f"Stage 2 automatic SMB monitor error: {exc}")

        time.sleep(_SMB_MONITOR_INTERVAL)


def start_smb_recovery_monitor():
    global _SMB_MONITOR_THREAD

    if _SMB_MONITOR_THREAD and _SMB_MONITOR_THREAD.is_alive():
        return

    _SMB_MONITOR_THREAD = threading.Thread(
        target=_smb_monitor_loop,
        name="SmartNetwork-SMB-Recovery-Monitor",
        daemon=True
    )
    _SMB_MONITOR_THREAD.start()


def main():
    if not HTML_FILE.exists():
        raise SystemExit(f"Missing UI file: {HTML_FILE}")
    start_smb_recovery_monitor()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    url = f"http://{HOST}:{PORT}"
    print("=" * 65)
    print("SMART NETWORK — DIAGNOSTIC COMMAND CENTER")
    print("Backend: READ-ONLY LIVE DIAGNOSTICS")
    print(f"Dashboard: {url}")
    print(f"Screenshots: {SCREENSHOT_DIR}")
    print("Press Ctrl+C to stop.")
    print("=" * 65)
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Smart Network Dashboard…")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
