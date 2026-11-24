#!/usr/bin/env python3
"""Smart Disk Cleaner — local-only macOS backend. Standard library only."""
import json, os, platform, shutil, subprocess, sys, threading, webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HTML = ROOT / "SmartDiskCleaner_index.html"
HISTORY = ROOT / "history.json"
HOST, PORT = "127.0.0.1", 8790
LOCK = threading.RLock()
STATE = {"analysis": None, "cleanup": None, "before": None}

def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")

def run(args, timeout=60):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        return {"ok": p.returncode == 0, "code": p.returncode, "out": p.stdout.strip(), "err": p.stderr.strip()}
    except Exception as exc:
        return {"ok": False, "code": -1, "out": "", "err": str(exc)}

def human(value):
    if value is None: return "Unavailable"
    n = max(0, int(value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB": return f"{n:.2f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024

def disk_snapshot():
    r = run(["df", "-k", "/"])
    lines = r["out"].splitlines()
    if not r["ok"] or len(lines) < 2:
        return {"available": False, "status": "Unavailable", "source": "df -k /"}
    fields = lines[-1].split()
    try:
        total, used, free = (int(fields[i]) * 1024 for i in (1, 2, 3))
    except (ValueError, IndexError):
        return {"available": False, "status": "Unable to Verify", "source": "df -k /"}
    return {"available": True, "source": "df -k / (mounted root volume)",
            "total_bytes": total, "used_bytes": used, "free_bytes": free,
            "total": human(total), "used": human(used), "free": human(free),
            "used_percent": round(used * 100 / total, 1) if total else None}

def folder_measure(path):
    p = Path(path).expanduser()
    if not p.exists(): return {"status": "Not Present", "path": str(p), "bytes": None, "size": "—"}
    if p.is_symlink(): return {"status": "Skipped (symbolic link)", "path": str(p), "bytes": None, "size": "—"}
    r = run(["du", "-sk", str(p)], timeout=120)
    if not r["ok"]: return {"status": "Permission / measurement unavailable", "path": str(p), "bytes": None, "size": "Unavailable", "detail": r["err"][:240]}
    try: size = int(r["out"].split()[0]) * 1024
    except (ValueError, IndexError): return {"status": "Unable to Verify", "path": str(p), "bytes": None, "size": "Unavailable"}
    return {"status": "Measured", "path": str(p), "bytes": size, "size": human(size)}

def storage_categories():
    home = Path.home()
    specs = [
        ("Applications", [Path("/Applications"), home / "Applications"]),
        ("Documents", [home / "Documents"]),
        ("Mail", [home / "Library" / "Mail"]),
        ("Developer", [home / "Developer", home / "Library" / "Developer"]),
        ("Photos", [home / "Pictures"]),
        ("Music", [home / "Music"]),
        ("TV & Media", [home / "Movies"]),
        ("Trash", [home / ".Trash"]),
    ]
    result = []
    for label, paths in specs:
        measured = [folder_measure(p) for p in paths]
        good = [x for x in measured if x.get("bytes") is not None]
        # These locations are separate; sum only successfully measured locations.
        result.append({"category": label, "status": "Measured" if good else "Unavailable",
                       "bytes": sum(x["bytes"] for x in good) if good else None,
                       "size": human(sum(x["bytes"] for x in good)) if good else "Unavailable",
                       "locations": measured})
    result.extend([
        {"category": "macOS", "status": "Unavailable", "bytes": None, "size": "Unavailable",
         "note": "The official macOS Storage category total is not exposed by a reliable public command."},
        {"category": "System Data", "status": "Unavailable", "bytes": None, "size": "Unavailable",
         "note": "Not inferred from a broad folder scan; APFS and system-managed data overlap."},
    ])
    return result

def system_check():
    disk = disk_snapshot()
    sw = run(["sw_vers"])
    mem = run(["sysctl", "-n", "hw.memsize"])
    swap = run(["sysctl", "vm.swapusage"])
    pressure = run(["memory_pressure"], timeout=30)
    hw = run(["system_profiler", "SPHardwareDataType"], timeout=45)
    model, ram_label = "Unavailable", "Unavailable"
    for line in hw["out"].splitlines():
        if "Model Name:" in line: model = line.split(":", 1)[1].strip()
        if line.strip().startswith("Memory:"): ram_label = line.split(":", 1)[1].strip()
    os_version = next((x.split(":",1)[1].strip() for x in sw["out"].splitlines() if x.startswith("ProductVersion:")), "Unavailable")
    try: ram_bytes = int(mem["out"]) if mem["ok"] else None
    except ValueError: ram_bytes = None
    vm = run(["vm_stat"])
    cpu = run(["sysctl", "-n", "machdep.cpu.brand_string"])
    result = {"timestamp": now(), "computer": model, "macos": os_version,
              "ram_label": ram_label, "ram_bytes": ram_bytes, "ram_total": human(ram_bytes),
              "swap": swap["out"] if swap["ok"] else "Unavailable",
              "memory_pressure": pressure["out"] if pressure["ok"] else "Unavailable",
              "vm_stat": vm["out"] if vm["ok"] else "Unavailable",
              "cpu": cpu["out"] if cpu["ok"] else "Unavailable",
              "disk": disk, "storage_categories": storage_categories(),
              "platform": platform.platform()}
    return result

def chrome_running():
    p = run(["pgrep", "-fl", "Google Chrome"])
    return p["ok"] and bool(p["out"])

def cache_targets():
    root = Path.home() / "Library" / "Caches" / "Google" / "Chrome"
    names = {"Cache", "Code Cache", "GPUCache", "CacheStorage"}
    found = []
    if not root.exists() or root.is_symlink(): return root, found
    root_real = root.resolve()
    for current, dirs, _ in os.walk(root, followlinks=False):
        base = Path(current)
        dirs[:] = [d for d in dirs if not (base / d).is_symlink()]
        for d in list(dirs):
            candidate = base / d
            if d in names and (candidate.resolve().parent == base.resolve()):
                try:
                    candidate.resolve().relative_to(root_real)
                    found.append(candidate)
                    dirs.remove(d)
                except (ValueError, OSError): pass
    return root, found

def analyze():
    home = Path.home()
    categories = storage_categories()
    pip = run([sys.executable, "-m", "pip", "cache", "dir"])
    pip_path = Path(pip["out"]).expanduser() if pip["ok"] and pip["out"] else None
    pip_info = folder_measure(pip_path) if pip_path else {"status":"Unavailable","bytes":None,"size":"Unavailable","path":"Unavailable"}
    chrome_root, chrome_dirs = cache_targets()
    chrome_bytes = 0
    for p in chrome_dirs:
        m = folder_measure(p)
        if m.get("bytes") is not None: chrome_bytes += m["bytes"]
    reports = []
    for label, path in [("Home folder",home),("User Library",home/"Library"),
                        ("User Caches",home/"Library"/"Caches"),("User Logs",home/"Library"/"Logs"),
                        ("Private temporary folders",Path("/private/var/folders"))]:
        r = run(["du","-xhd","1",str(path)], timeout=120)
        reports.append({"name":label,"path":str(path),"status":"Measured" if r["ok"] else "Unavailable",
                        "output":"\n".join(r["out"].splitlines()[-20:]) if r["ok"] else r["err"][:400]})
    large = run(["find",str(home/"Library"/"Caches"),str(home/"Library"/"Logs"),
                 "-type","f","-size","+50M","-print"],timeout=120)
    result = {"timestamp":now(),"storage_categories":categories,
              "chrome_running":chrome_running(),
              "candidates":[
                {"id":"pip","name":"Python pip cache","path":str(pip_path) if pip_path else "Unavailable",
                 "bytes":pip_info.get("bytes"),"size":pip_info.get("size","Unavailable"),
                 "available":pip_info.get("bytes") is not None,"status":pip_info.get("status"),
                 "action":"Use pip's supported cache purge command"},
                {"id":"chrome","name":"Google Chrome cache folders","path":str(chrome_root),
                 "bytes":chrome_bytes,"size":human(chrome_bytes),"files_or_folders":len(chrome_dirs),
                 "available":bool(chrome_dirs),"status":"Measured" if chrome_dirs else "No eligible cache folders found",
                 "action":"Remove only individually verified Cache / Code Cache / GPUCache / CacheStorage folders"}],
              "deep_inspection":{"folder_reports":reports,
                "large_files_over_50mb":large["out"].splitlines()[:100] if large["ok"] else [],
                "large_files_status":"Measured" if large["ok"] else "Unavailable"}}
    with LOCK:
        STATE["analysis"] = result
        STATE["cleanup"] = None
        STATE["before"] = disk_snapshot()
    return result

def execute_cleanup(selected, confirmed):
    if confirmed is not True: raise ValueError("Explicit confirmation is required.")
    if not isinstance(selected,list) or not selected or any(x not in ("pip","chrome") for x in selected):
        raise ValueError("Select one or more supported cleanup categories.")
    with LOCK: analysis, before = STATE["analysis"], STATE["before"]
    if not analysis or not before: raise ValueError("Run Detailed Analysis again before cleanup.")
    if "chrome" in selected and chrome_running():
        raise RuntimeError("Chrome is running. Close it yourself, then run analysis again. No app will be force-quit.")
    results=[]
    if "pip" in selected:
        info=next(x for x in analysis["candidates"] if x["id"]=="pip")
        p=Path(info["path"]).expanduser()
        home=Path.home().resolve()
        if not info["available"]: results.append({"item":"pip","success":False,"detail":"Cache path unavailable; skipped."})
        else:
            try: p.resolve().relative_to(home)
            except (ValueError,OSError): raise RuntimeError("pip cache is outside the user home folder; cleanup blocked.")
            purge=run([sys.executable,"-m","pip","cache","purge"],timeout=180)
            after=folder_measure(p)
            results.append({"item":"Python pip cache","success":purge["ok"],"before_bytes":info["bytes"],
                            "after_bytes":after.get("bytes"),"output":purge["out"] or purge["err"]})
    if "chrome" in selected:
        root, targets=cache_targets()
        if not root.exists() or root.is_symlink(): raise RuntimeError("Chrome cache root missing or unsafe; cleanup blocked.")
        root_real=root.resolve()
        for target in targets:
            if target.is_symlink(): raise RuntimeError("Symbolic link detected; cleanup blocked.")
            resolved=target.resolve()
            try: resolved.relative_to(root_real)
            except ValueError: raise RuntimeError("Cache path escaped Chrome cache root; cleanup blocked.")
            if resolved.name not in {"Cache","Code Cache","GPUCache","CacheStorage"}:
                raise RuntimeError("Unexpected folder name; cleanup blocked.")
        total_before=0
        for target in targets:
            m=folder_measure(target)
            if m.get("bytes") is not None: total_before += m["bytes"]
        removed, errors=0,[]
        for target in targets:
            try:
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                    removed += 1
            except OSError as exc: errors.append(f"{target}: {exc}")
        results.append({"item":"Google Chrome cache","success":not errors,"before_bytes":total_before,
                        "folders_removed":removed,"errors":errors,
                        "note":"Only exact cache-named subfolders were targeted; Chrome profile/root was retained."})
    outcome={"timestamp":now(),"selected":selected,"results":results,"before_disk":before}
    with LOCK: STATE["cleanup"]=outcome
    return outcome


def application_safety():
    p = run(["ps", "-axo", "pid=,comm="], timeout=25)
    apps = []
    if p["ok"]:
        seen = set()
        for line in p["out"].splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2: continue
            pid, command = parts
            path = Path(command)
            if ("/Applications/" in command or "/System/Applications/" in command) and ".app/" in command:
                name = command.split(".app/", 1)[0].rsplit("/", 1)[-1]
                if name not in seen:
                    seen.add(name)
                    apps.append({"name": name, "pid": int(pid) if pid.isdigit() else None})
        status = "Measured"
    else:
        status = "Unavailable"
    return {"timestamp": now(), "status": status, "running_applications": apps[:100],
            "running_application_count": len(apps),
            "open_file_ownership": "Unable to Verify",
            "unsaved_work": "Unable to Verify",
            "user_action": "Save your work and close applications manually. This tool never force-quits apps."}

def read_history():
    if not HISTORY.exists(): return []
    try:
        data=json.loads(HISTORY.read_text(encoding="utf-8"))
        return data if isinstance(data,list) else []
    except (OSError,json.JSONDecodeError): return []

def write_history(entry):
    with LOCK:
        records=read_history()
        records.append(entry)
        temp=HISTORY.with_suffix(".json.tmp")
        temp.write_text(json.dumps(records,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        os.replace(temp,HISTORY)

def verify():
    with LOCK: cleanup,before=STATE["cleanup"],STATE["before"]
    if not cleanup or not before: raise ValueError("Complete an approved cleanup first.")
    after=disk_snapshot()
    delta=after.get("free_bytes",0)-before.get("free_bytes",0) if after.get("available") and before.get("available") else None
    result={"timestamp":now(),"before":before,"after":after,
            "free_space_change_bytes":delta,"free_space_change":human(abs(delta)) if delta is not None else "Unavailable",
            "free_space_change_direction":("increased" if delta>0 else "decreased" if delta<0 else "unchanged") if delta is not None else "Unavailable",
            "cleanup_results":cleanup["results"],
            "note":"Disk free-space change is not the same as bytes deleted; APFS snapshots and background activity can affect it."}
    write_history({"type":"cleanup_verification","result":result})
    return result

class Handler(BaseHTTPRequestHandler):
    def send_json(self,data,status=200):
        raw=json.dumps(data,ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(raw))); self.send_header("Cache-Control","no-store")
        self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        try:
            if self.path in ("/","/index.html"):
                raw=HTML.read_bytes(); self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8")
                self.send_header("Content-Length",str(len(raw))); self.send_header("Cache-Control","no-store")
                self.end_headers(); self.wfile.write(raw); return
            if self.path=="/api/system": self.send_json(system_check()); return
            if self.path=="/api/safety": self.send_json(application_safety()); return
            if self.path=="/api/history": self.send_json({"records":read_history()}); return
            self.send_json({"error":"Endpoint not found."},404)
        except Exception as exc: self.send_json({"error":str(exc)},500)
    def do_POST(self):
        try:
            length=int(self.headers.get("Content-Length","0"))
            payload=json.loads(self.rfile.read(length) or b"{}")
            if self.path=="/api/analyze": self.send_json(analyze())
            elif self.path=="/api/cleanup": self.send_json(execute_cleanup(payload.get("selected"),payload.get("confirmed")))
            elif self.path=="/api/verify": self.send_json(verify())
            else: self.send_json({"error":"Endpoint not found."},404)
        except Exception as exc: self.send_json({"error":str(exc)},400)
    def log_message(self,fmt,*args): print("[SmartDiskCleaner]",fmt%args)

def main():
    if platform.system()!="Darwin":
        print("This backend requires macOS. No cleanup was performed."); raise SystemExit(1)
    if not HTML.is_file(): raise SystemExit(f"HTML file not found: {HTML}")
    if not HISTORY.exists(): HISTORY.write_text("[]\n",encoding="utf-8")
    server=ThreadingHTTPServer((HOST,PORT),Handler)
    url=f"http://{HOST}:{PORT}/"
    print("SMART DISK CLEANER — LOCAL MAC BACKEND")
    print("Project:",ROOT); print("URL:",url); print("Local-only server. Press Control+C to stop.")
    threading.Timer(1.0,lambda:webbrowser.open(url)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: print("\nStopping Smart Disk Cleaner.")
    finally: server.server_close()

if __name__=="__main__": main()
