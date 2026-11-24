#!/usr/bin/env python3
# Smart Disk Cleaner — local Mac backend
# Python standard library only. Localhost access only.

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HTML = ROOT / "SmartDiskCleaner_index.html"
HOST = "127.0.0.1"
PORT = 8790

STATE = {
    "analysis": None,
    "cleanup": None,
    "before": None
}
LOCK = threading.Lock()


def command(args, timeout=45):
    try:
        p = subprocess.run(
            args, capture_output=True, text=True,
            timeout=timeout, check=False
        )
        return {
            "returncode": p.returncode,
            "stdout": p.stdout.strip(),
            "stderr": p.stderr.strip()
        }
    except Exception as exc:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": str(exc)
        }


def human(n):
    n = max(0, int(n))
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(n)
    for unit in units:
        if value < 1024 or unit == "TB":
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"


def disk_snapshot():
    result = command(["df", "-k", "/"])
    lines = result["stdout"].splitlines()

    if len(lines) < 2:
        raise RuntimeError("macOS disk information could not be read.")

    fields = lines[-1].split()
    total = int(fields[1]) * 1024
    used = int(fields[2]) * 1024
    free = int(fields[3]) * 1024

    return {
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "total": human(total),
        "used": human(used),
        "free": human(free),
        "used_percent": round(used * 100 / total, 1) if total else 0
    }


def directory_size(path):
    path = Path(path)
    total = 0
    files = 0

    if not path.exists() or path.is_symlink():
        return {"bytes": 0, "files": 0, "exists": False}

    for current, dirs, names in os.walk(path, followlinks=False):
        dirs[:] = [
            d for d in dirs
            if not (Path(current) / d).is_symlink()
        ]
        for name in names:
            item = Path(current) / name
            try:
                if not item.is_symlink():
                    total += item.stat().st_size
                    files += 1
            except (OSError, PermissionError):
                pass

    return {"bytes": total, "files": files, "exists": True}


def pip_cache_path():
    result = command([sys.executable, "-m", "pip", "cache", "dir"])
    if result["returncode"] == 0 and result["stdout"]:
        return Path(result["stdout"].splitlines()[-1]).expanduser()
    return Path.home() / "Library" / "Caches" / "pip"


def chrome_cache_path():
    return Path.home() / "Library" / "Caches" / "Google" / "Chrome"


def chrome_is_running():
    result = command(["ps", "-axo", "comm="])
    names = result["stdout"].splitlines()
    return any(
        "Google Chrome" in Path(name.strip()).name
        for name in names
    )


def system_check():
    disk = disk_snapshot()
    os_info = command(["sw_vers"])
    hardware = command(["system_profiler", "SPHardwareDataType"], timeout=35)
    memory = command(["sysctl", "-n", "hw.memsize"])
    swap = command(["sysctl", "vm.swapusage"])
    pressure = command(["memory_pressure"])
    disk_info = command(["diskutil", "info", "/"], timeout=25)

    model = "Not reported"
    ram = "Not reported"

    for line in hardware["stdout"].splitlines():
        if "Model Name:" in line:
            model = line.split(":", 1)[1].strip()
        if "Memory:" in line:
            ram = line.split(":", 1)[1].strip()

    os_version = "Not reported"
    for line in os_info["stdout"].splitlines():
        if line.startswith("ProductVersion:"):
            os_version = line.split(":", 1)[1].strip()

    total_ram = None
    try:
        total_ram = int(memory["stdout"]) if memory["stdout"] else None
    except ValueError:
        pass

    return {
        "timestamp": datetime.now().astimezone().isoformat(),
        "computer": model,
        "macos": os_version,
        "ram_label": ram,
        "ram_bytes": total_ram,
        "disk": disk,
        "swap": swap["stdout"],
        "memory_pressure": pressure["stdout"][-1800:],
        "disk_identity": disk_info["stdout"][:3500],
        "platform": platform.platform()
    }


def deep_inspection():
    home = str(Path.home())
    targets = [
        ("Home folder", home),
        ("User Library", str(Path.home() / "Library")),
        ("User Caches", str(Path.home() / "Library" / "Caches")),
        ("User Logs", str(Path.home() / "Library" / "Logs")),
        ("Private temporary folders", "/private/var/folders")
    ]

    reports = []
    for label, path in targets:
        result = command(["du", "-xhd", "1", path], timeout=90)
        lines = result["stdout"].splitlines()
        reports.append({
            "name": label,
            "path": path,
            "output": "\n".join(lines[-18:]) if lines else
                      (result["stderr"] or "No readable output")
        })

    large = command([
        "find",
        str(Path.home() / "Library" / "Caches"),
        str(Path.home() / "Library" / "Logs"),
        "-type", "f", "-size", "+50M", "-print"
    ], timeout=90)

    return {
        "folder_reports": reports,
        "large_files_over_50mb": large["stdout"].splitlines()[:100]
    }


def analyze():
    pip_path = pip_cache_path()
    chrome_path = chrome_cache_path()

    pip_info = directory_size(pip_path)
    chrome_info = directory_size(chrome_path)

    details = deep_inspection()

    result = {
        "timestamp": datetime.now().astimezone().isoformat(),
        "chrome_running": chrome_is_running(),
        "candidates": [
            {
                "id": "pip",
                "name": "Python pip cache",
                "path": str(pip_path),
                "bytes": pip_info["bytes"],
                "size": human(pip_info["bytes"]),
                "files": pip_info["files"],
                "available": pip_info["exists"],
                "action": "Supported pip cache purge"
            },
            {
                "id": "chrome",
                "name": "Google Chrome cache",
                "path": str(chrome_path),
                "bytes": chrome_info["bytes"],
                "size": human(chrome_info["bytes"]),
                "files": chrome_info["files"],
                "available": chrome_info["exists"],
                "action": "Cache-only removal; Chrome must be closed"
            }
        ],
        "deep_inspection": details
    }

    with LOCK:
        STATE["analysis"] = result
        STATE["cleanup"] = None
        STATE["before"] = disk_snapshot()

    return result


def execute_cleanup(selected, confirmed):
    if not confirmed:
        raise ValueError("Explicit user confirmation is required.")

    if not isinstance(selected, list) or not selected:
        raise ValueError("Select at least one cleanup candidate.")

    if any(item not in ("pip", "chrome") for item in selected):
        raise ValueError("Unsupported cleanup selection.")

    with LOCK:
        analysis = STATE["analysis"]
        before = STATE["before"]

    if not analysis or not before:
        raise ValueError("Run Detailed Storage Analysis first.")

    # Check Chrome before starting any cleanup, so a running browser
    # cannot result in a partial cleanup operation.
    if "chrome" in selected and chrome_is_running():
        raise RuntimeError(
            "Google Chrome is still running. Close Chrome completely, "
            "then run the review again."
        )

    results = []

    if "pip" in selected:
        pip_path = pip_cache_path()
        current = directory_size(pip_path)

        if current["exists"] and current["bytes"] > 0:
            purge = command(
                [sys.executable, "-m", "pip", "cache", "purge"],
                timeout=120
            )
            results.append({
                "item": "Python pip cache",
                "success": purge["returncode"] == 0,
                "before": human(current["bytes"]),
                "output": purge["stdout"] or purge["stderr"]
            })
        else:
            results.append({
                "item": "Python pip cache",
                "success": True,
                "before": human(current["bytes"]),
                "output": "No pip cache files found."
            })

    if "chrome" in selected:
        target = chrome_cache_path()
        allowed_parent = (Path.home() / "Library" / "Caches" / "Google").resolve()

        if target.is_symlink():
            raise RuntimeError("Chrome cache path is a symbolic link; cleanup blocked.")

        resolved = target.resolve()
        if resolved.parent != allowed_parent or resolved.name != "Chrome":
            raise RuntimeError("Chrome cache path did not pass the safety check.")

        current = directory_size(target)

        if target.exists() and current["bytes"] > 0:
            shutil.rmtree(target)
            results.append({
                "item": "Google Chrome cache",
                "success": True,
                "before": human(current["bytes"]),
                "output": "The Chrome cache directory was removed."
            })
        else:
            results.append({
                "item": "Google Chrome cache",
                "success": True,
                "before": human(current["bytes"]),
                "output": "No Chrome cache files found."
            })

    outcome = {
        "timestamp": datetime.now().astimezone().isoformat(),
        "selected": selected,
        "results": results,
        "before_disk": before
    }

    with LOCK:
        STATE["cleanup"] = outcome

    return outcome


def verify():
    with LOCK:
        cleanup = STATE["cleanup"]
        before = STATE["before"]

    if not cleanup or not before:
        raise ValueError("Complete an approved cleanup operation first.")

    after = disk_snapshot()
    recovered = after["free_bytes"] - before["free_bytes"]

    result = {
        "timestamp": datetime.now().astimezone().isoformat(),
        "before": before,
        "after": after,
        "free_space_change_bytes": recovered,
        "free_space_change": human(abs(recovered)),
        "free_space_change_direction": (
            "increased" if recovered > 0 else
            "decreased" if recovered < 0 else "unchanged"
        ),
        "cleanup_results": cleanup["results"]
    }
    return result


CSS = r"""
/* SMART_DISK_MAC_BACKEND_STYLES */
.mac-live-status{font-size:11px;color:#62718c;margin:10px 0 18px}
.mac-step{padding:22px;border-radius:17px;background:#eaf1fb;
border:1px solid #d9e4f3;margin:15px 0}
.mac-step h2{font-size:17px;margin:0 0 7px}
.mac-step p{font-size:12px;color:#687994;margin:0 0 14px}
.mac-step button{border:0;border-radius:10px;padding:11px 16px;
background:linear-gradient(100deg,#4774ef,#7764e9);
color:white;font-weight:700;font-size:12px}
.mac-step button:disabled{opacity:.45;cursor:not-allowed}
.mac-output{white-space:pre-wrap;overflow-wrap:anywhere;
font:11px/1.55 ui-monospace,monospace;color:#34445f;
background:#f7f9fd;border:1px solid #dce5f2;border-radius:12px;
padding:14px;margin-top:13px;max-height:340px;overflow:auto}
.mac-candidate{display:flex;align-items:flex-start;gap:10px;
padding:13px;background:#f7f9fd;border:1px solid #dce5f2;
border-radius:12px;margin:9px 0;font-size:12px}
.mac-candidate input{margin-top:4px}
.mac-candidate strong{display:block;margin-bottom:3px}
.mac-step-note{font-size:11px;color:#687994;margin-top:10px}
.mac-stat strong{display:block}
.mac-action-row{display:flex;gap:10px;flex-wrap:wrap}
"""


WORKSPACE = r"""
<div class="mac-workspace" id="macDriveWorkspace">
  <div class="mac-workspace-inner">
    <header class="mac-workspace-header">
      <div class="mac-workspace-brand">
        <div class="mac-brand-icon">💻</div>
        <div>SMART DISK CLEANER
          <small style="display:block;font-size:10px;color:#75839a;font-weight:500">
            MAC DRIVE WORKSPACE · LIVE PYTHON BACKEND
          </small>
        </div>
      </div>
      <button class="mac-back-btn" id="macBackBtn" type="button">
        ← Back to Dashboard
      </button>
    </header>

    <section class="mac-hero">
      <h1>Mac Drive Cleaner</h1>
      <p>Four-step storage inspection, analysis, controlled cleanup and verification.</p>
      <div class="mac-live-status" id="macBackendStatus">
        Local Python backend · Waiting for first system check
      </div>
    </section>

    <div class="mac-stats">
      <article class="mac-stat">
        <small>Total Capacity</small>
        <strong id="macTotal">—</strong>
        <span>Live macOS disk measurement</span>
      </article>
      <article class="mac-stat">
        <small>Used Storage</small>
        <strong id="macUsed">—</strong>
        <span id="macUsedPercent">Awaiting system check</span>
      </article>
      <article class="mac-stat">
        <small>Available Storage</small>
        <strong id="macFree">—</strong>
        <span>Live macOS available space</span>
      </article>
    </div>

    <section class="mac-step">
      <h2>01 · System Check & Inspection</h2>
      <p>Read actual disk capacity, macOS version, Mac model, RAM, swap and system status.</p>
      <button id="macRunSystem">Run System Check</button>
      <div class="mac-output" id="macSystemOutput">Not run yet.</div>
    </section>

    <section class="mac-step">
      <h2>02 · Detailed Storage Analysis</h2>
      <p>Inspect pip and Chrome cache sizes, Library folders, Logs, temporary folders and large cache/log files. This step is read-only.</p>
      <button id="macRunAnalysis" disabled>Run Detailed Analysis</button>
      <div class="mac-output" id="macAnalysisOutput">Complete Step 01 first.</div>
    </section>

    <section class="mac-step">
      <h2>03 · Cleanup Review & Execution</h2>
      <p>Only the explicitly selected cache categories below can be cleaned. Projects, Documents, Application Support, personal data and backups are excluded.</p>

      <label class="mac-candidate">
        <input type="checkbox" id="macSelectPip">
        <span><strong>Python pip cache</strong>
          <span id="macPipInfo">Awaiting analysis</span>
        </span>
      </label>

      <label class="mac-candidate">
        <input type="checkbox" id="macSelectChrome">
        <span><strong>Google Chrome cache</strong>
          <span id="macChromeInfo">Awaiting analysis</span>
        </span>
      </label>

      <div class="mac-step-note" id="macChromeWarning">
        Chrome cache cleanup requires Google Chrome to be fully closed.
      </div>

      <div class="mac-action-row" style="margin-top:14px">
        <button id="macCleanSelected" disabled>Review & Clean Selected</button>
      </div>
      <div class="mac-output" id="macCleanupOutput">No cleanup has been executed.</div>
    </section>

    <section class="mac-step">
      <h2>04 · Final Verification</h2>
      <p>Measure available disk space again and compare it with the measurement taken before cleanup.</p>
      <button id="macVerify" disabled>Run Final Verification</button>
      <div class="mac-output" id="macVerifyOutput">Complete Step 03 first.</div>
    </section>

    <div class="mac-safety">
      <strong>Protection rules:</strong>
      No automatic cleanup. No deletion without an explicit selection and confirmation.
      Only pip cache and the dedicated Chrome cache folder are eligible in this first phase.
      No project files, documents, application profiles, logs, backups or personal files are targeted.
    </div>
  </div>
</div>
"""


JS = r"""
// SMART_DISK_MAC_LIVE_BACKEND
async function macApi(url, options={}) {
  const response = await fetch(url, {
    cache: "no-store",
    headers: {"Content-Type":"application/json"},
    ...options
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Backend request failed.");
  return data;
}

function macText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value == null ? "—" : String(value);
}

function macPretty(value) {
  return JSON.stringify(value, null, 2);
}

function macBusy(id, busy, label) {
  const el = document.getElementById(id);
  if (!el) return;
  el.disabled = busy;
  if (label) el.textContent = label;
}

document.getElementById("macRunSystem").addEventListener("click", async () => {
  macBusy("macRunSystem", true, "Checking Mac…");
  macText("macBackendStatus", "Python backend connected · System check running");
  try {
    const d = await macApi("/api/system");
    macText("macTotal", d.disk.total);
    macText("macUsed", d.disk.used);
    macText("macFree", d.disk.free);
    macText("macUsedPercent", d.disk.used_percent + "% used");
    macText("macSystemOutput",
      "Timestamp: " + d.timestamp +
      "\nMac: " + d.computer +
      "\nmacOS: " + d.macos +
      "\nRAM: " + d.ram_label +
      "\nDisk: " + d.disk.total +
      "\nUsed: " + d.disk.used +
      "\nAvailable: " + d.disk.free +
      "\n\nSWAP\n" + d.swap +
      "\n\nDisk identity\n" + d.disk_identity +
      "\n\nMemory pressure\n" + d.memory_pressure
    );
    macText("macBackendStatus", "Connected · System check completed");
    document.getElementById("macRunAnalysis").disabled = false;
  } catch(e) {
    macText("macSystemOutput", "ERROR: " + e.message);
    macText("macBackendStatus", "Backend error · " + e.message);
  } finally {
    macBusy("macRunSystem", false, "Run System Check");
  }
});

document.getElementById("macRunAnalysis").addEventListener("click", async () => {
  macBusy("macRunAnalysis", true, "Analyzing storage…");
  try {
    const d = await macApi("/api/analyze", {method:"POST", body:"{}"});
    const pip = d.candidates.find(x => x.id === "pip");
    const chrome = d.candidates.find(x => x.id === "chrome");

    macText("macPipInfo",
      pip.path + "\n" + pip.size + " · " + pip.files + " files");
    macText("macChromeInfo",
      chrome.path + "\n" + chrome.size + " · " + chrome.files + " files");
    macText("macChromeWarning", d.chrome_running
      ? "Chrome is currently running. Close it before selecting Chrome cache cleanup."
      : "Chrome is not detected as running.");

    macText("macAnalysisOutput",
      "Analysis time: " + d.timestamp +
      "\n\nCLEANUP CANDIDATES\n" +
      macPretty(d.candidates) +
      "\n\nREAD-ONLY DEEP INSPECTION\n" +
      macPretty(d.deep_inspection)
    );

    document.getElementById("macSelectPip").disabled = !pip.available;
    document.getElementById("macSelectChrome").disabled =
      !chrome.available || d.chrome_running;
    document.getElementById("macCleanSelected").disabled = false;
    macText("macBackendStatus", "Connected · Detailed analysis completed");
  } catch(e) {
    macText("macAnalysisOutput", "ERROR: " + e.message);
  } finally {
    macBusy("macRunAnalysis", false, "Run Detailed Analysis");
  }
});

document.getElementById("macCleanSelected").addEventListener("click", async () => {
  const selected = [];
  if (document.getElementById("macSelectPip").checked) selected.push("pip");
  if (document.getElementById("macSelectChrome").checked) selected.push("chrome");

  if (!selected.length) {
    macText("macCleanupOutput", "Select at least one cleanup candidate.");
    return;
  }

  const names = selected.map(x => x === "pip" ? "Python pip cache" : "Google Chrome cache");
  if (!confirm(
    "Please confirm the exact cleanup selection:\n\n" +
    names.join("\n") +
    "\n\nNo other files or folders will be targeted.\n\nContinue?"
  )) return;

  macBusy("macCleanSelected", true, "Cleaning selected cache…");
  try {
    const d = await macApi("/api/cleanup", {
      method:"POST",
      body:JSON.stringify({selected, confirmed:true})
    });
    macText("macCleanupOutput", macPretty(d));
    document.getElementById("macVerify").disabled = false;
    macText("macBackendStatus", "Cleanup action completed · Final verification available");
  } catch(e) {
    macText("macCleanupOutput", "CLEANUP NOT COMPLETED\n" + e.message);
  } finally {
    macBusy("macCleanSelected", false, "Review & Clean Selected");
  }
});

document.getElementById("macVerify").addEventListener("click", async () => {
  macBusy("macVerify", true, "Verifying…");
  try {
    const d = await macApi("/api/verify");
    macText("macTotal", d.after.total);
    macText("macUsed", d.after.used);
    macText("macFree", d.after.free);
    macText("macUsedPercent", d.after.used_percent + "% used");
    macText("macVerifyOutput", macPretty(d));
    macText("macBackendStatus", "Final verification completed");
  } catch(e) {
    macText("macVerifyOutput", "ERROR: " + e.message);
  } finally {
    macBusy("macVerify", false, "Run Final Verification");
  }
});
"""


def integrate_html():
    if not HTML.exists():
        raise FileNotFoundError(f"HTML file not found: {HTML}")

    source = HTML.read_text(encoding="utf-8")

    if "SMART_DISK_MAC_LIVE_BACKEND" in source:
        print("HTML integration already exists; keeping it unchanged.")
        return

    start = source.find('<div class="mac-workspace" id="macDriveWorkspace">')
    end = source.find("\n<script>\nconst titles=", start)

    if start < 0 or end < 0:
        raise RuntimeError(
            "Could not identify the existing Mac workspace safely. "
            "HTML was not modified."
        )

    source = source[:start] + WORKSPACE + source[end:]

    if "SMART_DISK_MAC_BACKEND_STYLES" not in source:
        source = source.replace("</style>", CSS + "\n</style>", 1)

    script_end = source.rfind("</script>")
    if script_end < 0:
        raise RuntimeError("Could not find the existing HTML script closing tag.")

    source = source[:script_end] + "\n" + JS + "\n" + source[script_end:]

    HTML.write_text(source, encoding="utf-8")
    print("HTML integration completed.")
    print("Existing HTML filename preserved:", HTML.name)
    print("Updated HTML size:", HTML.stat().st_size, "bytes")


class Handler(BaseHTTPRequestHandler):
    def send_json(self, data, status=200):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                raw = HTML.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return

        try:
            if self.path == "/api/system":
                self.send_json(system_check())
            elif self.path == "/api/verify":
                self.send_json(verify())
            else:
                self.send_json({"error": "Endpoint not found."}, 404)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 400)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")

            if self.path == "/api/analyze":
                self.send_json(analyze())
            elif self.path == "/api/cleanup":
                result = execute_cleanup(
                    payload.get("selected"),
                    payload.get("confirmed") is True
                )
                self.send_json(result)
            else:
                self.send_json({"error": "Endpoint not found."}, 404)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 400)

    def log_message(self, fmt, *args):
        print("[SmartDiskCleaner]", fmt % args)


def main():
    if platform.system() != "Darwin":
        print("This Mac backend is designed for macOS.")
        raise SystemExit(1)

    integrate_html()

    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        print(f"Could not start local server on port {PORT}: {exc}")
        print("If another Smart Disk Cleaner server is running, close it first.")
        raise SystemExit(1)

    url = f"http://{HOST}:{PORT}/"
    print("")
    print("SMART DISK CLEANER — PYTHON BACKEND")
    print("Project:", ROOT)
    print("Python file:", Path(__file__).name)
    print("HTML:", HTML.name)
    print("Local URL:", url)
    print("Server is local-only. No public network access.")
    print("Press Control+C to stop.")
    print("")

    threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Smart Disk Cleaner backend.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
