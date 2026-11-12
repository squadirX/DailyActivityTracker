from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path
import os
import shutil
import subprocess
import re
import tempfile

import queue
import threading
import time
import ftplib
import json
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo


APP_TITLE = "SmartBackup"
DEVELOPER = "Developed by Saiful Quadir"

COLORS = {
    "bg": "#0B1220",
    "sidebar": "#0E1728",
    "panel": "#121F33",
    "panel_alt": "#17263D",
    "panel_hover": "#1B2D47",
    "border": "#405875",
    "text": "#EDF3FA",
    "muted": "#C6D1DF",
    "subtle": "#AAB9CB",
    "accent": "#39D6B4",
    "accent_dark": "#153F3B",
    "blue": "#75B7FF",
    "blue_dark": "#1B3553",
    "amber": "#F1C56B",
    "amber_dark": "#49391D",
    "red": "#FF7C88",
    "red_dark": "#48232D",
    "white": "#FFFFFF",
    "button_primary": "#9BE8D3",
    "button_primary_hover": "#BDF3E5",
    "button_secondary": "#DCEBFA",
    "button_secondary_hover": "#EEF6FF",
    "button_text": "#173047",
    "nav_active": "#25445F",
}

FONT = "Helvetica Neue"
MONO = "Menlo"

HOME = Path.home()
CLOUD_STORAGE = HOME / "Library" / "CloudStorage"
DAILY_SOURCES = [HOME / "Documents", HOME / "Downloads"]
ICLOUD_ROOT = HOME / "Library/Mobile Documents/com~apple~CloudDocs"
ONEDRIVE_ROOT = CLOUD_STORAGE / "OneDrive-Personal(2) 2"


def discover_cloud_folder(prefixes: tuple[str, ...], fallback_names: tuple[str, ...] = ()) -> Path | None:
    """Find an existing local cloud-sync folder without changing it."""
    if CLOUD_STORAGE.is_dir():
        try:
            for candidate in sorted(CLOUD_STORAGE.iterdir(), key=lambda item: item.name.lower()):
                if candidate.is_dir() and any(candidate.name.lower().startswith(prefix.lower()) for prefix in prefixes):
                    return candidate
        except OSError:
            pass
    for name in fallback_names:
        candidate = HOME / name
        if candidate.is_dir():
            return candidate
    return None


GOOGLE_DRIVE_ROOT = discover_cloud_folder(("GoogleDrive-", "Google Drive"), ("Google Drive", "My Drive"))
DROPBOX_ROOT = discover_cloud_folder(("Dropbox",), ("Dropbox",))
GOOGLE_DRIVE_BASE = (
    GOOGLE_DRIVE_ROOT / "My Drive"
    if GOOGLE_DRIVE_ROOT and (GOOGLE_DRIVE_ROOT / "My Drive").is_dir()
    else GOOGLE_DRIVE_ROOT
)

DAILY_DESTINATION_GROUPS = {
    "PHYSICAL DESTINATIONS": (
        "Local Mac",
        "Windows C: Drive",
        "Windows D: ADATA",
        "Windows F: Transcend",
    ),
    "CLOUD DESTINATIONS": (
        "iCloud Drive",
        "OneDrive",
        "Google Drive",
        "Dropbox",
    ),
}

DAILY_DESTINATIONS = {
    "Local Mac": HOME / "Documents",
    "Windows C: Drive": Path("/Volumes/Windows_C_Drive/SmartBackup"),
    "Windows D: ADATA": Path("/Volumes/Windows_D_ADATA/SmartBackup"),
    "Windows F: Transcend": Path("/Volumes/Windows_F_TRANSCEND/SmartBackup"),
    "iCloud Drive": ICLOUD_ROOT / "SmartBackup",
    "OneDrive": ONEDRIVE_ROOT / "SmartBackup",
    "Google Drive": GOOGLE_DRIVE_BASE / "SmartBackup" if GOOGLE_DRIVE_BASE else None,
    "Dropbox": DROPBOX_ROOT / "SmartBackup" if DROPBOX_ROOT else None,
}


MANUAL_DESTINATIONS: dict[str, Path | None] = {
    "OneDrive": ONEDRIVE_ROOT / "SmartBackup" / "Archives",
    "iCloud Drive": ICLOUD_ROOT / "SmartBackup" / "Archives",
    "Google Drive": GOOGLE_DRIVE_BASE / "SmartBackup" / "Archives" if GOOGLE_DRIVE_BASE else None,
    "Dropbox": DROPBOX_ROOT / "SmartBackup" / "Archives" if DROPBOX_ROOT else None,
    "Windows C: Drive": Path("/Volumes/Windows_C_Drive"),
    "Windows D: ADATA": Path("/Volumes/Windows_D_ADATA"),
    "Windows F: Transcend": Path("/Volumes/Windows_F_TRANSCEND"),
    "Local Mac": HOME / "Documents",
}

WINDOWS_MOUNTS = {
    "Windows C: Drive": Path("/Volumes/Windows_C_Drive"),
    "Windows D: ADATA": Path("/Volumes/Windows_D_ADATA"),
    "Windows F: Transcend": Path("/Volumes/Windows_F_TRANSCEND"),
}



class SmartBackupDashboard:
    """SmartBackup dashboard with manual local archive execution and storage monitoring."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"{APP_TITLE} | Backup Command Center")
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        window_width = min(1420, max(1080, screen_width - 60))
        window_height = min(900, max(700, screen_height - 100))
        self.root.geometry(f"{window_width}x{window_height}")
        self.root.minsize(1040, 680)
        self.root.configure(bg=COLORS["bg"])

        self.selected_source = tk.StringVar(value="")
        self.destination_vars = {
            name: tk.BooleanVar(value=(name == "OneDrive"))
            for name in MANUAL_DESTINATIONS
        }

        self.daily_destination_vars = {
            name: tk.BooleanVar(value=False)
            for name in DAILY_DESTINATIONS
        }
        self.manual_destination_paths = dict(MANUAL_DESTINATIONS)
        self.current_page = "overview"
        self.page_history = []
        self.content: tk.Frame | None = None
        self.nav_buttons: dict[str, tk.Button] = {}

        self.connection_states = {
            name: ("checking", "Checking connection...")
            for name in MANUAL_DESTINATIONS
        }
        self.connection_dots = {}
        self.connection_details = {}
        self.connection_queue = queue.Queue()
        self._connection_worker_started = False
        self._connection_blink_on = True

        self.hardware_queue = queue.Queue()
        self.weather_queue = queue.Queue(maxsize=1)
        self.weather_label = None
        self.weather_worker_started = False
        self.network_traffic_queue = queue.Queue(maxsize=1)
        self.network_traffic_worker_started = False
        self.network_traffic_labels = {}
        self._hardware_ftps_cache = {"value": "Checking...", "status": "checking", "checked_at": None}
        self.hardware_ftps_worker_started = False
        self.hardware_worker_started = False
        self.hardware_labels = {}
        self.hardware_alert_label = None
        self._ftps_alert_active = False
        self._ftps_alert_flash_on = False
        self._ftps_alert_after_id = None
        self._ftps_alert_last_beep = 0.0
        self._hardware_battery_cache = {
            "value": "Checking...",
            "status": "checking",
        }

        self._configure_ttk()
        self._build_shell()
        self.root.after(350, self._animate_brand_logo)
        self.show_page("overview")
        self._start_connection_monitor()
        self._start_hardware_monitor()
        self._start_workspace_weather()
        self._update_workspace_clock()

    def _configure_ttk(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            "Smart.Horizontal.TProgressbar",
            troughcolor=COLORS["panel_alt"],
            background=COLORS["accent"],
            bordercolor=COLORS["panel_alt"],
            lightcolor=COLORS["accent"],
            darkcolor=COLORS["accent"],
            thickness=12,
        )
        style.configure(
            "Smart.Treeview",
            background=COLORS["panel"],
            fieldbackground=COLORS["panel"],
            foreground=COLORS["text"],
            bordercolor=COLORS["border"],
            rowheight=44,
            font=(FONT, 13),
        )
        style.configure(
            "Smart.Treeview.Heading",
            background=COLORS["panel_alt"],
            foreground=COLORS["muted"],
            relief="flat",
            font=(FONT, 18, "bold"),
        )
        style.configure(
            "Vertical.TScrollbar",
            background=COLORS["panel_alt"],
            troughcolor=COLORS["bg"],
            bordercolor=COLORS["bg"],
            arrowcolor=COLORS["muted"],
            darkcolor=COLORS["panel_alt"],
            lightcolor=COLORS["panel_alt"],
            relief="flat",
            width=10,
        )
        style.map(
            "Vertical.TScrollbar",
            background=[("active", COLORS["accent"])],
        )
        style.map(
            "Smart.Treeview",
            background=[("selected", COLORS["blue_dark"])],
            foreground=[("selected", COLORS["white"])],
        )

    def _build_shell(self) -> None:
        shell = tk.Frame(self.root, bg=COLORS["bg"])
        shell.pack(fill="both", expand=True)

        self.sidebar = tk.Frame(shell, bg=COLORS["sidebar"], width=278)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        main = tk.Frame(shell, bg=COLORS["bg"])
        main.pack(side="right", fill="both", expand=True)

        self._build_sidebar()
        self._build_topbar(main)

        body = tk.Frame(main, bg=COLORS["bg"])
        body.pack(fill="both", expand=True, padx=28, pady=(14, 18))

        self.canvas = tk.Canvas(
            body,
            bg=COLORS["bg"],
            highlightthickness=0,
            borderwidth=0,
        )
        self.scrollbar = ttk.Scrollbar(
            body,
            orient="vertical",
            command=self.canvas.yview,
        )
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        self.content = tk.Frame(self.canvas, bg=COLORS["bg"])
        self.content_window = self.canvas.create_window(
            (0, 0), window=self.content, anchor="nw"
        )
        self.content.bind("<Configure>", self._update_scroll_region)
        self.canvas.bind("<Configure>", self._resize_content)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

 
    def _update_scroll_region(self, _event=None) -> None:
        if hasattr(self, "canvas") and hasattr(self, "content"):
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _resize_content(self, event) -> None:
        if hasattr(self, "content_window"):
            self.canvas.itemconfigure(self.content_window, width=event.width)

    def _on_mousewheel(self, event) -> None:
        if hasattr(self, "canvas"):
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _make_pill_button(
        self, parent, text, command, primary=True,
        width=None, height=42, normal_color=None,
        hover_color=None, text_color_override=None,
        shadow_color_override=None, outline_color_override=None
    ):
        is_exit = text.strip().upper() == "EXIT"
        normal = normal_color or ("#C62838" if is_exit else ("#DCEBFA" if primary else "#EAF1F8"))
        hover = hover_color or ("#E53945" if is_exit else ("#C6DCF3" if primary else "#D7E6F5"))
        text_color = text_color_override or ("#FFFFFF" if is_exit else "#172B3F")
        shadow_color = shadow_color_override or ("#641B27" if is_exit else "#D4DFEA")
        outline_color = outline_color_override or ("#FF8790" if is_exit else "#B8CDE2")
        button_width = max(112, width * 9 + 40 if width else len(text) * 8 + 44)

        try:
            parent_bg = parent.cget("bg")
        except tk.TclError:
            parent_bg = COLORS["bg"]

        canvas = tk.Canvas(
            parent, width=button_width, height=height,
            bg=parent_bg, highlightthickness=0, bd=0,
            cursor="hand2"
        )

        x1, y1, x2, y2 = 2, 2, button_width - 2, height - 3
        radius = height // 2 - 2
        points = [
            x1 + radius, y1, x2 - radius, y1,
            x2, y1, x2, y1 + radius,
            x2, y2 - radius, x2, y2,
            x2 - radius, y2, x1 + radius, y2,
            x1, y2, x1, y2 - radius,
            x1, y1 + radius, x1, y1
        ]

        canvas.create_polygon(
            [x + 0.5 if i % 2 == 0 else y + 2
             for i, (x, y) in enumerate(zip(points[::2], points[1::2]))
             for x, y in [(x, y)] for _ in (0,)] ,
            state="hidden"
        ) if False else None

        shadow = [v + (2 if i % 2 else 0) for i, v in enumerate(points)]
        canvas.create_polygon(
            shadow, smooth=True, splinesteps=24,
            fill=shadow_color, outline=""
        )
        canvas.create_polygon(
            points, smooth=True, splinesteps=24,
            fill=normal, outline=outline_color, width=1,
            tags="pill"
        )
        canvas.create_text(
            button_width // 2, height // 2 - 1,
            text=text, fill=text_color,
            font=(FONT, 14, "bold"),
            tags="pill_text"
        )
        canvas.bind("<Enter>", lambda event: canvas.itemconfigure("pill", fill=hover))
        canvas.bind("<Leave>", lambda event: canvas.itemconfigure("pill", fill=normal))
        canvas.bind("<Button-1>", lambda event: command())
        return canvas

    def _action_button(self, parent, text, command,
                       primary=True, width=None):
        return self._make_pill_button(
            parent, text, command, primary=primary, width=width
        )

    def _animate_brand_logo(self) -> None:
        import math

        if not hasattr(self, "brand_mark") or not self.brand_mark.winfo_exists():
            return

        phase = (getattr(self, "_logo_animation_phase", 0.0) + 0.035) % (2 * math.pi)
        self._logo_animation_phase = phase
        blend = (1.0 - math.cos(phase)) / 2.0

        def mix_color(start, end):
            values = []
            for a, b in zip(start, end):
                values.append(round(a + (b - a) * blend))
            return "#{:02X}{:02X}{:02X}".format(*values)

        try:
            self.brand_mark.itemconfigure(
                "cloud_outer",
                fill=mix_color((14, 23, 40), (30, 75, 75))
            )
            self.brand_mark.itemconfigure(
                "cloud_mid",
                fill=mix_color((21, 63, 59), (57, 150, 130))
            )
            self.brand_mark.itemconfigure(
                "cloud_inner",
                fill=mix_color((28, 91, 79), (115, 220, 194))
            )
            self.brand_mark.itemconfigure(
                "badge",
                fill=mix_color((21, 63, 59), (43, 103, 94))
            )
            self.brand_mark.itemconfigure(
                "logo_text",
                fill=mix_color((57, 214, 180), (245, 255, 255))
            )
            self.root.after(30, self._animate_brand_logo)
        except tk.TclError:
            return



    def _build_hardware_watch(self) -> None:
        card = tk.Frame(
            self.sidebar,
            bg=COLORS["panel"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
        )
        card.pack(fill="x", padx=12, pady=(8, 8))

        tk.Label(
            card,
            text="MAC HARDWARE WATCH",
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=(FONT, 10, "bold"),
        ).pack(anchor="w", padx=10, pady=(8, 4))

        rows = [
            ("cpu", "CPU"),
            ("memory", "MEMORY"),
            ("battery", "BATTERY"),
            ("thermal", "THERMAL"),
            ("network", "NETWORK"),
            ("ftps", "FTPS SERVER"),
        ]

        for key, caption in rows:
            row = tk.Frame(card, bg=COLORS["panel"])
            row.pack(fill="x", padx=8, pady=5)

            dot = tk.Label(
                row,
                text="●",
                bg=COLORS["panel"],
                fg="#8A94A6",
                font=(FONT, 10, "bold"),
            )
            dot.pack(side="left")

            tk.Label(
                row,
                text=caption,
                bg=COLORS["panel"],
                fg=COLORS["subtle"],
                font=(FONT, 9, "bold"),
            ).pack(side="left", padx=(5, 2))

            value = tk.Label(
                row,
                text="Checking...",
                bg=COLORS["panel"],
                fg=COLORS["text"],
                font=(FONT, 13, "bold"),
                anchor="w",
                justify="left",
                wraplength=160,
            )
            value.pack(side="right", fill="x", expand=True)

            self.hardware_labels[key] = {
                "dot": dot,
                "value": value,
            }


        tk.Label(
            card,
            text="NETWORK TRAFFIC",
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=(FONT, 9, "bold"),
        ).pack(anchor="w", padx=10, pady=(7, 2))

        traffic_rows = [
            ("download_speed", "DOWNLOAD"),
            ("upload_speed", "UPLOAD"),
            ("total_download", "TOTAL DOWN"),
            ("total_upload", "TOTAL UP"),
        ]

        for key, caption in traffic_rows:
            row = tk.Frame(card, bg=COLORS["panel"])
            row.pack(fill="x", padx=8, pady=3)

            dot = tk.Label(
                row,
                text="●",
                bg=COLORS["panel"],
                fg="#8A94A6",
                font=(FONT, 9, "bold"),
            )
            dot.pack(side="left")

            tk.Label(
                row,
                text=caption,
                bg=COLORS["panel"],
                fg=COLORS["subtle"],
                font=(FONT, 8, "bold"),
            ).pack(side="left", padx=(5, 2))

            value = tk.Label(
                row,
                text="Measuring...",
                bg=COLORS["panel"],
                fg=COLORS["text"],
                font=(FONT, 11, "bold"),
                anchor="w",
                justify="left",
                wraplength=160,
            )
            value.pack(side="right", fill="x", expand=True)

            self.network_traffic_labels[key] = {
                "dot": dot,
                "value": value,
            }

        self.hardware_alert_label = tk.Label(
            card,
            text="SYSTEM ALERT  |  CHECKING",
            bg=COLORS["panel"],
            fg="#F0B44D",
            font=(FONT, 10, "bold"),
            anchor="w",
        )
        self.hardware_alert_label.pack(
            anchor="w", padx=10, pady=(8, 12)
        )

    def _hardware_command(self, command, timeout=4):
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            pass
        return ""

    def _read_hardware_cpu(self):
        output = self._hardware_command(["top", "-l", "1", "-n", "0"])
        match = re.search(r"([\d.]+)%\s*idle", output, re.IGNORECASE)
        if not match:
            return {"value": "N/A", "status": "unknown"}

        idle = float(match.group(1))
        usage = max(0.0, min(100.0, 100.0 - idle))

        if usage >= 90:
            status = "critical"
        elif usage >= 75:
            status = "warning"
        else:
            status = "normal"

        return {"value": f"{usage:.0f}% used", "status": status}

    def _read_hardware_memory(self):
        output = self._hardware_command(["memory_pressure", "-Q"])
        match = re.search(
            r"System-wide memory free percentage:\s*([\d.]+)%",
            output,
            re.IGNORECASE,
        )
        if not match:
            return {
                "value": "N/A",
                "status": "unknown",
                "detail": "Memory pressure unavailable",
            }

        free_percent = float(match.group(1))

        if free_percent < 10:
            status = "critical"
        elif free_percent < 20:
            status = "warning"
        else:
            status = "normal"

        return {
            "value": f"{free_percent:.0f}% free",
            "status": status,
            "detail": "Native memory-free indicator",
        }

    def _read_hardware_battery(self):
        output = self._hardware_command(
            ["ioreg", "-r", "-c", "AppleSmartBattery", "-l"],
            timeout=6,
        )

        if not output:
            return {"value": "N/A", "status": "unknown"}

        def get_value(*keys):
            for key in keys:
                match = re.search(
                    rf'"{re.escape(key)}"\s*=\s*(?:"([^"]*)"|([0-9]+))',
                    output,
                    re.IGNORECASE,
                )
                if match:
                    return match.group(1) or match.group(2)
            return None

        health = get_value(
            "BatteryHealthCondition",
            "BatteryHealth",
        )
        cycles = get_value("CycleCount")
        current = get_value("CurrentCapacity")
        maximum = get_value("MaxCapacity")

        charge = None
        if current and maximum:
            try:
                max_value = float(maximum)
                if max_value > 0:
                    charge = round(float(current) / max_value * 100)
            except (TypeError, ValueError):
                pass

        parts = []
        if charge is not None:
            parts.append(f"{charge}%")
        if cycles is not None:
            parts.append(f"{cycles} cycles")
        if health:
            parts.append(health)

        if not parts:
            return {"value": "N/A", "status": "unknown"}

        health_text = (health or "").lower()
        if "service" in health_text or "replace" in health_text:
            status = "warning"
        elif "good" in health_text or "normal" in health_text:
            status = "normal"
        else:
            status = "unknown"

        return {
            "value": " | ".join(parts),
            "status": status,
        }

    def _read_hardware_thermal(self):
        output = self._hardware_command(
            ["pmset", "-g", "therm"]
        )

        speed_match = re.search(
            r"CPU_Speed_Limit\s*=\s*(\d+)",
            output,
        )
        scheduler_match = re.search(
            r"CPU_Scheduler_Limit\s*=\s*(\d+)",
            output,
        )
        cpu_match = re.search(
            r"CPU_Available_CPUs\s*=\s*(\d+)",
            output,
        )

        if not speed_match and not scheduler_match and not cpu_match:
            return {
                "value": "Thermal data unavailable",
                "status": "unknown",
            }

        speed = int(speed_match.group(1)) if speed_match else None
        scheduler = (
            int(scheduler_match.group(1))
            if scheduler_match else None
        )
        available = int(cpu_match.group(1)) if cpu_match else None

        limits = [
            value for value in (speed, scheduler)
            if value is not None
        ]

        warning_recorded = (
            "thermal warning level has been recorded" in output.lower()
            and "no thermal warning level has been recorded" not in output.lower()
        )

        if warning_recorded or any(value < 50 for value in limits):
            status = "critical"
        elif any(value < 100 for value in limits):
            status = "warning"
        else:
            status = "normal"

        parts = []

        if speed is not None:
            parts.append(f"Speed {speed}%")
        if scheduler is not None:
            parts.append(f"Scheduler {scheduler}%")
        if available is not None:
            parts.append(f"{available} CPUs")

        if "no thermal warning level has been recorded" in output.lower():
            parts.append("No Warning")

        return {
            "value": " | ".join(parts),
            "status": status,
        }

    def _read_hardware_network(self):
        ports = self._hardware_command(
            ["networksetup", "-listallhardwareports"]
        )

        wifi_interface = None

        for block in re.split(r"\n\s*\n", ports):
            name_match = re.search(
                r"^\s*Hardware Port:\s*(.+)$",
                block,
                re.MULTILINE,
            )
            device_match = re.search(
                r"^\s*Device:\s*(\S+)",
                block,
                re.MULTILINE,
            )

            if (
                name_match
                and device_match
                and name_match.group(1).strip().lower() == "wi-fi"
            ):
                wifi_interface = device_match.group(1)
                break

        if not wifi_interface:
            return {
                "value": "Wi-Fi INTERFACE NOT FOUND",
                "status": "warning",
            }

        link = self._hardware_command(
            ["ifconfig", wifi_interface]
        )
        ip_address = self._hardware_command(
            ["ipconfig", "getifaddr", wifi_interface]
        ).strip()

        flags_match = re.search(
            r"flags=\d+<([^>]+)>",
            link,
        )
        flags = flags_match.group(1).split(",") if flags_match else []

        connected = (
            "UP" in flags
            and "RUNNING" in flags
            and bool(re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", ip_address))
            and not ip_address.startswith("169.254.")
        )

        if connected:
            return {
                "value": "Wi-Fi CONNECTED",
                "status": "normal",
                "interface": wifi_interface,
            }

        return {
            "value": "Wi-Fi DISCONNECTED",
            "status": "warning",
            "interface": wifi_interface,
        }

    def _read_hardware_ftps(self):
        network = self._read_hardware_network()

        if network.get("status") != "normal":
            return {
                "value": "BLOCKED - Wi-Fi Disconnected",
                "status": "warning",
            }

        ftp = None

        try:
            ftp = ftplib.FTP_TLS()
            ftp.connect("192.168.2.162", 21, timeout=5)

            if ftp.sock:
                ftp.sock.settimeout(5)

            response = ftp.auth()

            if response.startswith("234"):
                return {
                    "value": "ONLINE",
                    "status": "normal",
                }

            return {
                "value": "OFFLINE",
                "status": "warning",
            }

        except Exception:
            return {
                "value": "OFFLINE",
                "status": "warning",
            }

        finally:
            if ftp is not None:
                try:
                    ftp.close()
                except Exception:
                    pass


    def _hardware_ftps_worker(self):
        while True:
            try:
                result = self._read_hardware_ftps()
                result["checked_at"] = time.monotonic()
                self._hardware_ftps_cache = result
            except Exception:
                self._hardware_ftps_cache = {
                    "value": "OFFLINE",
                    "status": "warning",
                    "checked_at": time.monotonic(),
                }

            time.sleep(15)

    def _read_hardware_snapshot(self):
        cpu = self._read_hardware_cpu()
        memory = self._read_hardware_memory()
        thermal = self._read_hardware_thermal()
        network = self._read_hardware_network()

        battery = self._hardware_battery_cache
        now = time.monotonic()

        if (
            battery.get("checked_at") is None
            or now - battery.get("checked_at", 0) >= 60
        ):
            battery = self._read_hardware_battery()
            battery["checked_at"] = now
            self._hardware_battery_cache = battery

        ftps = self._hardware_ftps_cache

        if network.get("status") != "normal":
            ftps = {
                "value": "BLOCKED - Wi-Fi Disconnected",
                "status": "warning",
                "checked_at": ftps.get("checked_at"),
            }

        metrics = {
            "cpu": cpu,
            "memory": memory,
            "battery": battery,
            "thermal": thermal,
            "network": network,
            "ftps": ftps,
        }

        statuses = [
            item.get("status", "unknown")
            for item in metrics.values()
        ]

        if "critical" in statuses:
            alert = "CRITICAL"
        elif "warning" in statuses:
            alert = "WARNING"
        elif all(status in ("unknown", "checking") for status in statuses):
            alert = "UNAVAILABLE"
        else:
            alert = "HEALTHY"

        return {
            "metrics": metrics,
            "alert": alert,
        }



    def _read_hardware_traffic_counters(self, interface):
        output = self._hardware_command(["netstat", "-ibn"])

        for line in output.splitlines():
            columns = line.split()
            if len(columns) < 10 or columns[0] != interface:
                continue

            if not columns[2].lower().startswith("<link#"):
                continue

            try:
                return int(columns[6]), int(columns[9])
            except (ValueError, IndexError):
                continue

        return None

    def _network_traffic_worker(self):
        previous_interface = None
        previous_rx = None
        previous_tx = None
        previous_time = None
        total_download = 0
        total_upload = 0

        def format_size(byte_count):
            value = float(byte_count)
            for unit in ("B", "KB", "MB", "GB", "TB"):
                if value < 1024 or unit == "TB":
                    return f"{value:.1f} {unit}"
                value /= 1024
            return f"{value:.1f} TB"

        def format_rate(byte_rate):
            value = float(byte_rate)
            for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
                if value < 1024 or unit == "GB/s":
                    return f"{value:.1f} {unit}"
                value /= 1024
            return f"{value:.1f} GB/s"

        while True:
            try:
                network = self._read_hardware_network()
                interface = network.get("interface")

                if network.get("status") != "normal" or not interface:
                    previous_interface = None
                    previous_rx = None
                    previous_tx = None
                    previous_time = None

                    snapshot = {
                        "download_speed": {
                            "value": "Wi-Fi Disconnected",
                            "status": "warning",
                        },
                        "upload_speed": {
                            "value": "Wi-Fi Disconnected",
                            "status": "warning",
                        },
                        "total_download": {
                            "value": format_size(total_download),
                            "status": "warning",
                        },
                        "total_upload": {
                            "value": format_size(total_upload),
                            "status": "warning",
                        },
                    }
                else:
                    counters = self._read_hardware_traffic_counters(interface)

                    if counters is None:
                        snapshot = {
                            key: {
                                "value": "N/A",
                                "status": "unknown",
                            }
                            for key in (
                                "download_speed",
                                "upload_speed",
                                "total_download",
                                "total_upload",
                            )
                        }
                        snapshot["total_download"]["value"] = format_size(total_download)
                        snapshot["total_upload"]["value"] = format_size(total_upload)
                    else:
                        received, sent = counters
                        now = time.monotonic()
                        down_rate = 0.0
                        up_rate = 0.0

                        if (
                            interface == previous_interface
                            and previous_rx is not None
                            and previous_tx is not None
                            and previous_time is not None
                            and received >= previous_rx
                            and sent >= previous_tx
                        ):
                            elapsed = max(now - previous_time, 0.001)
                            down_delta = received - previous_rx
                            up_delta = sent - previous_tx

                            down_rate = down_delta / elapsed
                            up_rate = up_delta / elapsed
                            total_download += down_delta
                            total_upload += up_delta

                        previous_interface = interface
                        previous_rx = received
                        previous_tx = sent
                        previous_time = now

                        snapshot = {
                            "download_speed": {
                                "value": format_rate(down_rate),
                                "status": "normal",
                            },
                            "upload_speed": {
                                "value": format_rate(up_rate),
                                "status": "normal",
                            },
                            "total_download": {
                                "value": format_size(total_download),
                                "status": "normal",
                            },
                            "total_upload": {
                                "value": format_size(total_upload),
                                "status": "normal",
                            },
                        }

                try:
                    self.network_traffic_queue.put_nowait(snapshot)
                except queue.Full:
                    try:
                        self.network_traffic_queue.get_nowait()
                    except queue.Empty:
                        pass
                    self.network_traffic_queue.put_nowait(snapshot)

            except Exception:
                failure = {
                    key: {"value": "N/A", "status": "unknown"}
                    for key in (
                        "download_speed",
                        "upload_speed",
                        "total_download",
                        "total_upload",
                    )
                }
                try:
                    self.network_traffic_queue.put_nowait(failure)
                except queue.Full:
                    try:
                        self.network_traffic_queue.get_nowait()
                    except queue.Empty:
                        pass
                    self.network_traffic_queue.put_nowait(failure)

            time.sleep(1)

    def _apply_hardware_traffic_snapshot(self, snapshot):
        colors = {
            "normal": "#35C98A",
            "warning": "#F0B44D",
            "critical": "#F06464",
            "unknown": "#8A94A6",
        }

        for key, metric in snapshot.items():
            widgets = self.network_traffic_labels.get(key)
            if not widgets:
                continue

            color = colors.get(metric.get("status"), colors["unknown"])
            widgets["dot"].configure(fg=color)
            widgets["value"].configure(
                text=metric.get("value", "N/A"),
                fg=color,
            )

    def _hardware_monitor_worker(self):
        while True:
            try:
                snapshot = self._read_hardware_snapshot()
                self.hardware_queue.put(snapshot)
            except Exception:
                self.hardware_queue.put({
                    "metrics": {},
                    "alert": "UNAVAILABLE",
                })
            time.sleep(3)

    def _start_hardware_monitor(self):
        if not self.hardware_worker_started:
            self.hardware_worker_started = True
            worker = threading.Thread(
                target=self._hardware_monitor_worker,
                daemon=True,
                name="SmartBackupHardwareMonitor",
            )
            worker.start()

        if not self.hardware_ftps_worker_started:
            self.hardware_ftps_worker_started = True
            ftps_worker = threading.Thread(
                target=self._hardware_ftps_worker,
                daemon=True,
                name="SmartBackupFTPSMonitor",
            )
            ftps_worker.start()


        if not self.network_traffic_worker_started:
            self.network_traffic_worker_started = True
            traffic_worker = threading.Thread(
                target=self._network_traffic_worker,
                daemon=True,
                name="SmartBackupNetworkTrafficMonitor",
            )
            traffic_worker.start()

        self.root.after(700, self._poll_hardware_monitor)


    def _poll_hardware_monitor(self):
        latest = None
        try:
            while True:
                latest = self.hardware_queue.get_nowait()
        except queue.Empty:
            pass

        if latest:
            self._apply_hardware_snapshot(latest)


        traffic_snapshot = None
        try:
            while True:
                traffic_snapshot = self.network_traffic_queue.get_nowait()
        except queue.Empty:
            pass

        if traffic_snapshot:
            self._apply_hardware_traffic_snapshot(traffic_snapshot)

        weather_snapshot = None
        try:
            while True:
                weather_snapshot = self.weather_queue.get_nowait()
        except queue.Empty:
            pass

        if weather_snapshot is not None and self.weather_label:
            self.weather_label.configure(
                text=weather_snapshot,
                fg="#35D9F5",
            )

        try:
            self.root.after(700, self._poll_hardware_monitor)
        except tk.TclError:
            pass

    def _set_ftps_alert(self, active):
        if active and not self._ftps_alert_active:
            self._ftps_alert_active = True
            self._ftps_alert_flash_on = True
            self._ftps_alert_last_beep = 0.0
            self._ftps_alert_after_id = self.root.after(
                100, self._ftps_alert_tick
            )

        elif not active and self._ftps_alert_active:
            self._ftps_alert_active = False

            if self._ftps_alert_after_id is not None:
                try:
                    self.root.after_cancel(self._ftps_alert_after_id)
                except tk.TclError:
                    pass

            self._ftps_alert_after_id = None
            self._ftps_alert_flash_on = False

    def _ftps_alert_tick(self):
        if not self._ftps_alert_active:
            self._ftps_alert_after_id = None
            return

        widgets = self.hardware_labels.get("ftps")
        if widgets:
            self._ftps_alert_flash_on = not self._ftps_alert_flash_on
            color = (
                "#F06464"
                if self._ftps_alert_flash_on
                else COLORS["panel"]
            )
            widgets["dot"].configure(fg=color)
            widgets["value"].configure(fg=color)

        now = time.monotonic()
        if now - self._ftps_alert_last_beep >= 2.0:
            try:
                self.root.bell()
            except tk.TclError:
                self._ftps_alert_active = False
                self._ftps_alert_after_id = None
                return

            self._ftps_alert_last_beep = now

        try:
            self._ftps_alert_after_id = self.root.after(
                500, self._ftps_alert_tick
            )
        except tk.TclError:
            self._ftps_alert_active = False
            self._ftps_alert_after_id = None

    def _apply_hardware_snapshot(self, snapshot):
        colors = {
            "normal": "#35C98A",
            "warning": "#F0B44D",
            "critical": "#F06464",
            "unknown": "#8A94A6",
            "checking": "#8A94A6",
        }

        metrics = snapshot.get("metrics", {})
        ftps_metric = metrics.get("ftps", {})
        ftps_value = str(ftps_metric.get("value", "")).upper()

        ftps_offline = (
            ftps_metric.get("status") == "warning"
            and (
                "OFFLINE" in ftps_value
                or "BLOCKED" in ftps_value
            )
        )

        self._set_ftps_alert(ftps_offline)

        for key, metric in metrics.items():
            widgets = self.hardware_labels.get(key)
            if not widgets:
                continue

            status = metric.get("status", "unknown")
            value = metric.get("value", "N/A")

            if key == "ftps" and self._ftps_alert_active:
                widgets["value"].configure(text=value)
                continue

            color = colors.get(status, colors["unknown"])
            widgets["dot"].configure(fg=color)
            widgets["value"].configure(
                text=value,
                fg=color,
            )

        alert = snapshot.get("alert", "UNAVAILABLE")
        alert_colors = {
            "HEALTHY": "#35C98A",
            "WARNING": "#F0B44D",
            "CRITICAL": "#F06464",
            "UNAVAILABLE": "#8A94A6",
        }

        if self.hardware_alert_label:
            self.hardware_alert_label.configure(
                text=f"SYSTEM ALERT  |  {alert}",
                fg=alert_colors.get(alert, "#8A94A6"),
            )

    def _start_workspace_weather(self):
        if self.weather_worker_started:
            return
        self.weather_worker_started = True
        threading.Thread(
            target=self._workspace_weather_worker,
            name="SmartBackupWorkspaceWeather",
            daemon=True,
        ).start()

    def _workspace_weather_worker(self):
        url = (
            "https://api.open-meteo.com/v1/forecast"
            "?latitude=43.6532&longitude=-79.3832"
            "&current=temperature_2m&timezone=America%2FToronto"
        )

        while True:
            display = "--°C"
            try:
                request = urllib.request.Request(
                    url,
                    headers={"User-Agent": "SmartBackup-Dashboard/1.0"},
                )
                with urllib.request.urlopen(request, timeout=8) as response:
                    data = json.loads(response.read().decode("utf-8"))

                temperature = data.get("current", {}).get("temperature_2m")
                if temperature is not None:
                    display = f"{round(float(temperature))}°C"
            except Exception:
                display = "--°C"

            try:
                self.weather_queue.put_nowait(display)
            except queue.Full:
                try:
                    self.weather_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self.weather_queue.put_nowait(display)
                except queue.Full:
                    pass

            time.sleep(300)

    def _update_workspace_clock(self):
        try:
            toronto_now = datetime.now(ZoneInfo("America/Toronto"))
            clock_text = toronto_now.strftime("%I:%M %p (%Z)").lstrip("0")
            if self.weather_label:
                current_text = self.weather_label.cget("text")
                temperature = current_text.split(" | ", 1)[0]
                self.weather_label.configure(
                    text=f"{temperature} | {clock_text}"
                )
            self.root.after(1000, self._update_workspace_clock)
        except tk.TclError:
            pass

    def _build_sidebar(self) -> None:
        brand = tk.Frame(self.sidebar, bg=COLORS["sidebar"])
        brand.pack(fill="x", padx=22, pady=(28, 34))

        self.brand_mark = tk.Canvas(
            brand,
            width=66,
            height=66,
            bg=COLORS["sidebar"],
            bd=0,
            highlightthickness=0,
            relief="flat",
        )
        self.brand_mark.pack(side="left")

        self.brand_mark.create_oval(
            3, 13, 63, 65,
            fill="#0E1728",
            outline="",
            tags="cloud_outer",
        )
        self.brand_mark.create_oval(
            7, 5, 59, 61,
            fill="#153F3B",
            outline="",
            tags="cloud_mid",
        )
        self.brand_mark.create_oval(
            12, 9, 56, 57,
            fill="#1C5B4F",
            outline="",
            tags="cloud_inner",
        )
        self.brand_mark.create_oval(
            14, 13, 52, 53,
            fill=COLORS["accent_dark"],
            outline="",
            tags="badge",
        )
        self.brand_mark.create_text(
            33, 32,
            text="S",
            fill=COLORS["accent"],
            font=(FONT, 25, "bold"),
            tags="logo_text",
        )

        brand_text = tk.Frame(brand, bg=COLORS["sidebar"])
        brand_text.pack(side="left", padx=(11, 0))
        tk.Label(
            brand_text,
            text="SmartBackup",
            bg=COLORS["sidebar"],
            fg=COLORS["text"],
            font=(FONT, 17, "bold"),
        ).pack(anchor="w")
        tk.Label(
            brand_text,
            text="BACKUP COMMAND CENTER",
            bg=COLORS["sidebar"],
            fg=COLORS["subtle"],
            font=(FONT, 9, "bold"),
        ).pack(anchor="w", pady=(9, 0))

        workspace_row = tk.Frame(self.sidebar, bg=COLORS["sidebar"])
        workspace_row.pack(anchor="w", fill="x", padx=23, pady=(0, 9))

        tk.Label(
            workspace_row,
            text="WORKSPACE",
            bg=COLORS["sidebar"],
            fg=COLORS["subtle"],
            font=(FONT, 11, "bold"),
        ).pack(side="left")

        self.weather_label = tk.Label(
            workspace_row,
            text="--°C | --:-- -- (EST)",
            bg=COLORS["sidebar"],
            fg="#35D9F5",
            font=(FONT, 11, "bold"),
        )
        self.weather_label.pack(side="left", padx=(10, 0))

        navigation = [
            ("overview", "01", "Overview"),
            ("daily", "02", "Daily Backup"),
            ("manual", "03", "Manual Archive"),
            ("activity", "04", "Activity & Reports"),
        ]
        for key, symbol, label in navigation:
            button = self._make_pill_button(
                self.sidebar,
                f"{symbol}  |  {label}",
                lambda page=key: self.show_page(page),
                primary=False,
                width=23,
                height=44,
            )
            button.itemconfigure("pill", fill="#1B3553", outline="#1B3553")
            button.itemconfigure("pill_text", fill="#FFFFFF")
            button.bind(
                "<Enter>",
                lambda event, widget=button:
                    widget.itemconfigure("pill", fill="#25445F")
            )
            button.bind(
                "<Leave>",
                lambda event, widget=button:
                    widget.itemconfigure("pill", fill="#1B3553")
            )
            button.pack(fill="x", padx=10, pady=5)
            self.nav_buttons[key] = button

        self._build_hardware_watch()

        spacer = tk.Frame(self.sidebar, bg=COLORS["sidebar"])
        spacer.pack(fill="both", expand=True)

        status_card = tk.Frame(
            self.sidebar,
            bg=COLORS["panel"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
        )
        status_card.pack(fill="x", padx=15, pady=(0, 17))
        tk.Label(
            status_card,
            text="SYSTEM MODE",
            bg=COLORS["panel"],
            fg=COLORS["subtle"],
            font=(FONT, 11, "bold"),
        ).pack(anchor="w", padx=13, pady=(12, 5))
        tk.Label(
            status_card,
            text="UI Design Preview",
            bg=COLORS["panel"],
            fg=COLORS["accent"],
            font=(FONT, 13, "bold"),
        ).pack(anchor="w", padx=13)
        tk.Label(
            status_card,
            text="No files will be transferred",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=(FONT, 11),
        ).pack(anchor="w", padx=13, pady=(3, 12))

        exit_button = self._make_pill_button(
            self.sidebar, "EXIT", self._confirm_exit,
            primary=False, width=12
        )
        exit_button.pack(anchor="w", padx=20, pady=(0, 12))

        tk.Label(
            self.sidebar,
            text=DEVELOPER,
            bg=COLORS["sidebar"],
            fg=COLORS["subtle"],
            font=(FONT, 11),
        ).pack(anchor="w", padx=21, pady=(0, 17))

    def _build_topbar(self, parent: tk.Frame) -> None:
        top = tk.Frame(parent, bg=COLORS["bg"], height=74)
        top.pack(fill="x", padx=28, pady=(20, 0))
        top.pack_propagate(False)

        left = tk.Frame(top, bg=COLORS["bg"])
        left.pack(side="left", fill="y")
        self.page_title = tk.Label(
            left,
            text="Overview",
            bg=COLORS["bg"],
            fg=COLORS["text"],
            font=(FONT, 25, "bold"),
        )
        self.page_title.pack(anchor="w")
        self.page_subtitle = tk.Label(
            left,
            text="A clear view of your backup workflow and destinations.",
            bg=COLORS["bg"],
            fg=COLORS["muted"],
            font=(FONT, 12),
        )
        self.page_subtitle.pack(anchor="w", pady=(4, 0))

        pill = tk.Label(
            top,
            text="UI PREVIEW  |  TRANSFER ENGINE NOT CONNECTED",
            bg=COLORS["amber_dark"],
            fg=COLORS["amber"],
            padx=12,
            pady=8,
            font=(FONT, 11, "bold"),
        )
        pill.pack(side="right", anchor="center", pady=8)

        self.previous_button = self._make_pill_button(
            top, "← PREVIOUS", self._go_previous,
            primary=False, width=14, height=36
        )
        self.previous_button.pack(side="right", anchor="center", padx=(0, 10))

    def _confirm_exit(self) -> None:
        self.root.destroy()

    def _go_previous(self) -> None:
        if not self.page_history:
            return

        previous_page = self.page_history.pop()
        self.show_page(previous_page, record_history=False)

    def _clear_content(self) -> None:
        if self.content is not None:
            for widget in self.content.winfo_children():
                widget.destroy()

    def show_page(self, page: str, record_history: bool = True) -> None:
        if record_history and page != self.current_page:
            self.page_history.append(self.current_page)

        self.current_page = page
        # Manual review is placed directly below the destination card.
        self._clear_content()

        for key, button in self.nav_buttons.items():
            button.itemconfigure("pill", fill="#1B3553", outline="#1B3553")
            button.itemconfigure("pill_text", fill="#FFFFFF")

        page_info = {
            "overview": ("Overview", "A clear view of your backup workflow and destinations."),
            "daily": ("Daily Backup", "Keep the existing automatic Documents and Downloads workflow."),
            "manual": ("Manual Archive", "Choose a folder, select destinations, review, and then back up."),
            "activity": ("Activity & Reports", "A single place for transfer history, results, and verification reports."),
        }
        title, subtitle = page_info[page]
        self.page_title.configure(text=title)
        self.page_subtitle.configure(text=subtitle)
        self.previous_button.itemconfigure(
            "pill",
            fill="#C6DCF3" if self.page_history else "#EAF1F8"
        )

        if page == "overview":
            self.previous_button.pack_forget()
        elif not self.previous_button.winfo_manager():
            self.previous_button.pack(
                side="right", anchor="center", padx=(0, 10)
            )

        if page == "overview":
            self._build_overview()
        elif page == "daily":
            self._build_daily_page()
        elif page == "manual":
            self._build_manual_page()
        else:
            self._build_activity_page()

    def _card(self, parent: tk.Widget, padding: int = 17) -> tk.Frame:
        frame = tk.Frame(
            parent,
            bg=COLORS["panel"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
            padx=padding,
            pady=padding,
        )
        return frame

    def _section_heading(self, parent: tk.Widget, title: str, subtitle: str | None = None) -> None:
        wrapper = tk.Frame(parent, bg=COLORS["bg"])
        wrapper.pack(fill="x", pady=(0, 12))
        tk.Label(
            wrapper,
            text=title,
            bg=COLORS["bg"],
            fg=COLORS["text"],
            font=(FONT, 18, "bold"),
        ).pack(anchor="w")
        if subtitle:
            tk.Label(
                wrapper,
                text=subtitle,
                bg=COLORS["bg"],
                fg=COLORS["muted"],
                font=(FONT, 12),
            ).pack(anchor="w", pady=(3, 0))

    def _metric_card(self, parent: tk.Widget, value: str, label: str, hint: str, accent: str) -> None:
        card = self._card(parent, 16)
        card.pack(side="left", fill="both", expand=True, padx=(0, 12))
        tk.Label(card, text=value, bg=COLORS["panel"], fg=accent, font=(FONT, 23, "bold")).pack(anchor="w")
        tk.Label(card, text=label, bg=COLORS["panel"], fg=COLORS["text"], font=(FONT, 13, "bold")).pack(anchor="w", pady=(4, 0))
        tk.Label(card, text=hint, bg=COLORS["panel"], fg=COLORS["muted"], font=(FONT, 11)).pack(anchor="w", pady=(5, 0))

    def _action_button(self, parent: tk.Widget, text: str, command,
                       primary: bool = True, width: int | None = None) -> tk.Button:
        is_exit = text.strip().upper() == "EXIT"

        green_actions = {
            "OPEN MANUAL ARCHIVE  →",
            "OPEN DAILY BACKUP  →",
            "CREATE MANUAL ARCHIVE  →",
        }

        if text.strip().upper() in green_actions:
            return self._make_pill_button(
                parent,
                text,
                command,
                primary=primary,
                width=width,
                height=42,
                normal_color="#14532D",
                hover_color="#166534",
                text_color_override="#FFFFFF",
                shadow_color_override="#0B2E1A",
                outline_color_override="#2E7D4F",
            )

        normal_bg = "#C62828" if is_exit else (COLORS["button_primary"] if primary else COLORS["button_secondary"])
        hover_bg = "#E53935" if is_exit else (COLORS["button_primary_hover"] if primary else COLORS["button_secondary_hover"])
        text_color = "#FFFFFF" if is_exit else COLORS["button_text"]

        button = tk.Button(
            parent,
            text=text,
            command=command,
            bg=normal_bg,
            fg=text_color,
            activebackground=hover_bg,
            activeforeground=text_color,
            relief="raised",
            overrelief="groove",
            bd=2,
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            highlightcolor=COLORS["border"],
            padx=18,
            pady=9,
            font=(FONT, 12, "bold"),
            cursor="hand2",
            width=width,
            height=2,
            justify="center",
            anchor="center",
        )
        button.bind("<Enter>", lambda event: button.configure(bg=hover_bg))
        button.bind("<Leave>", lambda event: button.configure(bg=normal_bg))
        return button

    def _build_overview(self) -> None:
        assert self.content is not None
        hero = tk.Frame(self.content, bg=COLORS["panel_alt"], padx=24, pady=22)
        hero.pack(fill="x", pady=(0, 17))
        hero_left = tk.Frame(hero, bg=COLORS["panel_alt"])
        hero_left.pack(side="left", fill="both", expand=True)
        tk.Label(
            hero_left,
            text="Your backups. Organized.",
            bg=COLORS["panel_alt"],
            fg=COLORS["text"],
            font=(FONT, 23, "bold"),
        ).pack(anchor="w")
        tk.Label(
            hero_left,
            text="Run your regular daily backup or create a one-time archive to the destination you choose.",
            bg=COLORS["panel_alt"],
            fg=COLORS["muted"],
            font=(FONT, 12),
            wraplength=650,
            justify="left",
        ).pack(anchor="w", pady=(7, 0))
        self._action_button(hero, "OPEN MANUAL ARCHIVE  →", lambda: self.show_page("manual"), True).pack(side="right", padx=(15, 0), pady=10)

        metrics = tk.Frame(self.content, bg=COLORS["bg"])
        metrics.pack(fill="x", pady=(0, 23))
        self._metric_card(metrics, "02", "Backup modes", "Daily Backup + Manual Archive", COLORS["blue"])
        self._metric_card(metrics, "08", "Manual destinations", "Local, iCloud, OneDrive, Google Drive, Dropbox, C:, D: and F:", COLORS["accent"])
        self._metric_card(metrics, "—", "Active transfers", "Transfer engine not connected", COLORS["amber"])

        self._section_heading(self.content, "BACKUP WORKSPACE", "Choose how you want to protect your files.")
        cards = tk.Frame(self.content, bg=COLORS["bg"])
        cards.pack(fill="x", pady=(0, 22))

        daily = self._card(cards, 20)
        daily.configure(height=270)
        daily.pack_propagate(False)
        daily.pack(side="left", fill="both", expand=True, padx=(0, 9))
        tk.Label(daily, text="◷", bg=COLORS["panel"], fg=COLORS["blue"], font=(FONT, 25, "bold")).pack(anchor="w")
        tk.Label(daily, text="Daily Smart Backup", bg=COLORS["panel"], fg=COLORS["text"], font=(FONT, 17, "bold")).pack(anchor="w", pady=(8, 5))
        tk.Label(
            daily,
            text="Automatically scan Documents and Downloads for new or modified items. Keep the familiar review and approval process.",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=(FONT, 12),
            wraplength=390,
            justify="left",
        ).pack(anchor="w", pady=(0, 15))
        tk.Label(daily, text="LOCAL  ·  WINDOWS C:  ·  WINDOWS D:  ·  WINDOWS F:", bg=COLORS["panel"], fg=COLORS["text"], font=(FONT, 9, "bold")).pack(anchor="w", pady=(0, 7))
        tk.Label(daily, text="ICLOUD  ·  ONEDRIVE  ·  GOOGLE DRIVE  ·  DROPBOX", bg=COLORS["panel"], fg=COLORS["text"], font=(FONT, 9, "bold")).pack(anchor="w", pady=(0, 13))
        self._action_button(daily, "OPEN DAILY BACKUP  →", lambda: self.show_page("daily"), False).pack(anchor="w")

        manual = self._card(cards, 20)
        manual.configure(height=270)
        manual.pack_propagate(False)
        manual.pack(side="left", fill="both", expand=True, padx=(9, 0))
        tk.Label(manual, text="▣", bg=COLORS["panel"], fg=COLORS["accent"], font=(FONT, 25, "bold")).pack(anchor="w")
        tk.Label(manual, text="Manual Archive Backup", bg=COLORS["panel"], fg=COLORS["text"], font=(FONT, 17, "bold")).pack(anchor="w", pady=(8, 5))
        tk.Label(
            manual,
            text="Browse to any folder, choose one or more destinations, review the selection, and monitor each file in the progress window.",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=(FONT, 12),
            wraplength=390,
            justify="left",
        ).pack(anchor="w", pady=(0, 15))
        tk.Label(manual, text="LOCAL  ·  WINDOWS C:  ·  WINDOWS D:  ·  WINDOWS F:", bg=COLORS["panel"], fg=COLORS["text"], font=(FONT, 9, "bold")).pack(anchor="w", pady=(0, 7))
        tk.Label(manual, text="ICLOUD  ·  ONEDRIVE  ·  GOOGLE DRIVE  ·  DROPBOX", bg=COLORS["panel"], fg=COLORS["text"], font=(FONT, 9, "bold")).pack(anchor="w", pady=(0, 13))
        self._action_button(manual, "CREATE MANUAL ARCHIVE  →", lambda: self.show_page("manual"), True).pack(anchor="w")

        self._section_heading(self.content, "DESTINATION OVERVIEW", "Physical drive capacity is read from the filesystem. Cloud provider quota is shown only when verified through its API.")
        self._destination_strip(self.content)

    def _display_path(self, path: Path | None) -> str:
        return str(path) if path else "Sync folder not detected on this Mac"

    def _destination_status(self, name: str) -> str:
        path = MANUAL_DESTINATIONS.get(name)
        if path is None:
            return "SETUP REQUIRED - FOLDER NOT DETECTED"
        if name in WINDOWS_MOUNTS:
            return "NETWORK VOLUME MOUNTED" if WINDOWS_MOUNTS[name].is_mount() else "NETWORK VOLUME NOT MOUNTED"
        if name in {"OneDrive", "iCloud Drive", "Google Drive", "Dropbox"}:
            sync_root = {
                "OneDrive": ONEDRIVE_ROOT,
                "iCloud Drive": ICLOUD_ROOT,
                "Google Drive": GOOGLE_DRIVE_ROOT,
                "Dropbox": DROPBOX_ROOT,
            }.get(name)
            if sync_root and sync_root.is_dir():
                return "LOCAL SYNC FOLDER FOUND - CLOUD SYNC NOT VERIFIED"
            return "SYNC FOLDER NOT DETECTED"
        if path.parent.is_dir():
            return "LOCAL PARENT FOLDER FOUND - WRITE ACCESS NOT TESTED"
        return "LOCAL PARENT FOLDER NOT FOUND"

    def _capacity_for_path(self, path, name=None):
        if path is None:
            return None

        try:
            candidate = Path(path).expanduser()

            if name in WINDOWS_MOUNTS:
                if not candidate.is_mount():
                    return None
                return shutil.disk_usage(candidate)

            while not candidate.exists() and candidate != candidate.parent:
                candidate = candidate.parent

            if not candidate.exists():
                return None

            return shutil.disk_usage(candidate)
        except (OSError, ValueError):
            return None

    def _load_cloud_usage(self):
        if getattr(self, "_cloud_usage_loaded", False):
            return

        self._cloud_totals_gb = {
            "iCloud Drive": 5.0,
            "Google Drive": 15.0,
            "Dropbox": 2.0,
            "OneDrive": 1024.0,
        }

        self._cloud_keys = {
            "iCloud Drive": "cloud_usage_icloud",
            "Google Drive": "cloud_usage_google_drive",
            "Dropbox": "cloud_usage_dropbox",
            "OneDrive": "cloud_usage_onedrive",
        }

        self._cloud_links = {
            "iCloud Drive": None,
            "Google Drive": "https://one.google.com/storage",
            "Dropbox": "https://www.dropbox.com/home",
            "OneDrive": "https://onedrive.live.com/",
        }

        self.cloud_usage_gb = {
            "iCloud Drive": 1.7,
            "Google Drive": 14.45,
            "Dropbox": 0.0052,
            "OneDrive": 24.0,
        }

        self.cloud_card_widgets = {}
        self._cloud_usage_loaded = True

        subprocess = __import__("subprocess")

        for name, key in self._cloud_keys.items():
            result = subprocess.run(
                ["defaults", "read", "com.smartbackup.dashboard", key],
                capture_output=True,
                text=True,
            )

            if result.returncode == 0:
                try:
                    value = float(result.stdout.strip())
                    if 0 <= value <= self._cloud_totals_gb[name]:
                        self.cloud_usage_gb[name] = value
                except ValueError:
                    pass

    def _save_cloud_usage(self, name, value):
        subprocess = __import__("subprocess")
        key = self._cloud_keys[name]

        subprocess.run(
            [
                "defaults", "write", "com.smartbackup.dashboard",
                key, "-float", str(value)
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def _format_cloud_size(self, value):
        if value < 0.01:
            return f"{value * 1000:.1f} MB"
        if value >= 1024:
            return f"{value / 1024:.2f} TB"
        return f"{value:.2f} GB"

    def _open_cloud_provider(self, name):
        subprocess = __import__("subprocess")

        if name == "iCloud Drive":
            subprocess.Popen([
                "open", "-a", "System Settings",
                "x-apple.systempreferences:com.apple.AppleAccount"
            ])
            return

        url = self._cloud_links.get(name)
        if url:
            subprocess.Popen(["open", url])

    def _update_cloud_card(self, name, source_widgets=None):
        source = source_widgets or self.cloud_card_widgets.get(name)

        if not source:
            return

        try:
            value = float(source["entry"].get().strip())
        except (ValueError, TypeError):
            messagebox.showerror(
                "Invalid Storage Value",
                "Enter Used Storage as a number in GB."
            )
            return

        total = self._cloud_totals_gb[name]

        if value < 0 or value > total:
            messagebox.showerror(
                "Invalid Storage Value",
                f"Used Storage must be between 0 and {total:g} GB."
            )
            return

        try:
            self._save_cloud_usage(name, value)
        except Exception as exc:
            messagebox.showerror(
                "Save Failed",
                f"Could not save the storage value.\n{exc}"
            )
            return

        self.cloud_usage_gb[name] = value
        free = total - value
        percent = value / total * 100 if total else 0

        groups = []
        for registry_name in ("cloud_card_widgets", "daily_cloud_card_widgets"):
            registry = getattr(self, registry_name, {})
            widgets = registry.get(name) if isinstance(registry, dict) else None
            if widgets and all(widgets is not item for item in groups):
                groups.append(widgets)

        if source not in groups:
            groups.append(source)

        summary_text = (
            f"{percent:.2f}% used  |  "
            f"{self._format_cloud_size(value)} used  |  "
            f"{self._format_cloud_size(free)} free / "
            f"{self._format_cloud_size(total)}"
        )

        color = (
            COLORS["accent"] if percent < 70
            else COLORS["amber"] if percent < 90
            else COLORS["red"]
        )

        for widgets in groups:
            try:
                widgets["summary"].config(text=summary_text)

                bar = widgets["bar"]
                height = max(5, bar.winfo_reqheight())
                width = max(1, bar.winfo_width())

                bar.delete("all")
                bar.create_rectangle(
                    0, 0, width, height,
                    fill=COLORS["panel_alt"],
                    outline="",
                )
                bar.create_rectangle(
                    0, 0, width * min(100, max(0, percent)) / 100,
                    height,
                    fill=color,
                    outline="",
                )

                entry = widgets["entry"]
                entry.configure(state="normal")
                entry.delete(0, "end")
                entry.insert(0, f"{value:.4f}".rstrip("0").rstrip("."))
                # Keep the input editable for future manual updates.

                widgets["status"].config(text="UPDATED AND SAVED")
            except (tk.TclError, KeyError, TypeError):
                continue

    def _make_cloud_update_button(self, parent, font_size=8):
        try:
            parent_bg = parent.cget("bg")
        except tk.TclError:
            parent_bg = COLORS["panel"]

        button = tk.Canvas(
            parent,
            width=82,
            height=28,
            bg=parent_bg,
            bd=0,
            highlightthickness=0,
            relief="flat",
            cursor="hand2",
        )

        def draw_pill(color):
            button.delete("pill")
            button.create_oval(
                1, 2, 25, 26,
                fill=color, outline="", tags="pill"
            )
            button.create_rectangle(
                13, 2, 70, 26,
                fill=color, outline="", tags="pill"
            )
            button.create_oval(
                58, 2, 82, 26,
                fill=color, outline="", tags="pill"
            )
            button.create_text(
                41, 14,
                text="UPDATE",
                fill="#FFFFFF",
                font=(FONT, font_size, "bold"),
                tags="pill",
            )

        draw_pill("#F28C28")
        button.bind("<Enter>", lambda event: draw_pill("#D97706"))
        button.bind("<Leave>", lambda event: draw_pill("#F28C28"))
        return button

    def _add_cloud_capacity(self, parent, name):
        self._load_cloud_usage()

        total = self._cloud_totals_gb[name]
        used = self.cloud_usage_gb[name]
        free = total - used
        percent = used / total * 100 if total else 0

        summary = tk.Label(
            parent,
            text=(
                f"{percent:.2f}% used  |  "
                f"{self._format_cloud_size(used)} used  |  "
                f"{self._format_cloud_size(free)} free / "
                f"{self._format_cloud_size(total)}"
            ),
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=(FONT, 9, "bold"),
            wraplength=300,
            justify="left",
        )
        summary.pack(anchor="w", pady=(7, 4))

        bar = tk.Canvas(
            parent,
            height=10,
            bg=COLORS["panel_alt"],
            highlightthickness=0,
            bd=0,
        )
        bar.pack(fill="x")

        def redraw(event=None):
            width = max(1, bar.winfo_width())
            current = self.cloud_usage_gb[name]
            current_percent = current / total * 100 if total else 0
            color = (
                COLORS["accent"] if current_percent < 70
                else COLORS["amber"] if current_percent < 90
                else COLORS["red"]
            )

            bar.delete("all")
            bar.create_rectangle(
                0, 0, width, 10,
                fill=COLORS["panel_alt"],
                outline="",
            )
            bar.create_rectangle(
                0, 0,
                width * min(100, max(0, current_percent)) / 100,
                10,
                fill=color,
                outline="",
            )

        bar.bind("<Configure>", redraw)
        bar.after_idle(redraw)

        link_row = tk.Frame(parent, bg=COLORS["panel"])
        link_row.pack(fill="x", pady=(7, 2))

        link = tk.Label(
            link_row,
            text="OPEN ↗",
            bg=COLORS["panel"],
            fg=COLORS["blue"],
            font=(FONT, 8, "bold"),
            cursor="hand2",
        )
        link.pack(side="left")

        link.bind(
            "<Button-1>",
            lambda event, provider=name: self._open_cloud_provider(provider)
        )

        input_row = tk.Frame(parent, bg=COLORS["panel"])
        input_row.pack(fill="x", pady=(5, 0))

        tk.Label(
            input_row,
            text="Used GB",
            bg=COLORS["panel"],
            fg=COLORS["subtle"],
            font=(FONT, 8),
        ).pack(side="left")

        entry = tk.Entry(
            input_row,
            width=10,
            font=(FONT, 9),
            relief="solid",
            bd=1,
        )
        entry.insert(0, f"{used:.4f}".rstrip("0").rstrip("."))
        entry.pack(side="left", padx=(6, 5))

        update_button = self._make_cloud_update_button(
            input_row, font_size=8
        )
        update_button.pack(side="left")
        update_button.bind(
            "<Button-1>",
            lambda event, provider=name: self._update_cloud_card(provider)
        )

        status = tk.Label(
            parent,
            text="MANUAL STORAGE SNAPSHOT",
            bg=COLORS["panel"],
            fg=COLORS["subtle"],
            font=(FONT, 7),
        )
        status.pack(anchor="w", pady=(4, 0))

        self.cloud_card_widgets[name] = {
            "summary": summary,
            "bar": bar,
            "entry": entry,
            "status": status,
        }

    def _add_capacity_bar(self, parent, path, name):
        cloud_names = {
            "OneDrive", "iCloud Drive",
            "Google Drive", "Dropbox"
        }

        if name in cloud_names:
            self._add_cloud_capacity(parent, name)
            return

        if path is None:
            return

        usage = self._capacity_for_path(path, name)

        if usage is None:
            tk.Label(
                parent,
                text="Capacity unavailable - check mount",
                bg=COLORS["panel"],
                fg=COLORS["subtle"],
                font=(FONT, 9),
                wraplength=300,
                justify="left",
            ).pack(anchor="w", pady=(7, 0))
            return

        total_gb = usage.total / (1024 ** 3)
        used_gb = usage.used / (1024 ** 3)
        free_gb = usage.free / (1024 ** 3)

        percent = (
            usage.used / usage.total * 100
            if usage.total else 0
        )

        bar_color = (
            COLORS["accent"] if percent < 70
            else COLORS["amber"] if percent < 90
            else COLORS["red"]
        )

        tk.Label(
            parent,
            text=(
                f"{percent:.1f}% used  |  "
                f"{used_gb:.1f} GB used  |  "
                f"{free_gb:.1f} GB free / {total_gb:.1f} GB"
            ),
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=(FONT, 9, "bold"),
            wraplength=300,
            justify="left",
        ).pack(anchor="w", pady=(7, 4))

        bar = tk.Canvas(
            parent,
            height=10,
            bg=COLORS["panel_alt"],
            highlightthickness=0,
            bd=0,
        )
        bar.pack(fill="x")

        def redraw(event=None):
            width = max(1, bar.winfo_width())
            bar.delete("all")
            bar.create_rectangle(
                0, 0, width, 10,
                fill=COLORS["panel_alt"],
                outline="",
            )
            fill_width = width * min(100, max(0, percent)) / 100
            bar.create_rectangle(
                0, 0, fill_width, 10,
                fill=bar_color,
                outline="",
            )

        bar.bind("<Configure>", redraw)
        bar.after_idle(redraw)



    def _destination_strip(self, parent: tk.Widget) -> None:
        entries = [
            ("Local Mac", COLORS["accent"]),
            ("Windows C: Drive", COLORS["blue"]),
            ("Windows D: ADATA", COLORS["blue"]),
            ("Windows F: Transcend", COLORS["amber"]),
            ("OneDrive", COLORS["accent"]),
            ("Google Drive", COLORS["blue"]),
            ("iCloud Drive", COLORS["blue"]),
            ("Dropbox", COLORS["amber"]),
        ]

        self.connection_dots = {}
        self.connection_details = {}

        grid = tk.Frame(parent, bg=COLORS["bg"])
        grid.pack(fill="x")

        for index, (name, accent) in enumerate(entries):
            row, col = divmod(index, 3)

            card = self._card(grid, 16)
            card.grid(
                row=row,
                column=col,
                sticky="nsew",
                padx=(0, 9) if col < 2 else (0, 0),
                pady=6,
            )
            grid.grid_columnconfigure(col, weight=1)

            header = tk.Frame(card, bg=COLORS["panel"])
            header.pack(fill="x")

            dot = tk.Label(
                header,
                text="●",
                bg=COLORS["panel"],
                fg=COLORS["amber"],
                font=(FONT, 18, "bold"),
            )
            dot.pack(side="left", padx=(0, 8))

            self.connection_dots[name] = dot

            tk.Label(
                header,
                text=name,
                bg=COLORS["panel"],
                fg=accent,
                font=(FONT, 13, "bold"),
            ).pack(side="left")

            path = self.manual_destination_paths.get(name)

            path_text = (
                self._display_path(path)
                if path is not None
                else "Destination folder not detected"
            )

            tk.Label(
                card,
                text=path_text,
                bg=COLORS["panel"],
                fg=COLORS["muted"],
                font=(MONO, 9),
                wraplength=300,
                justify="left",
            ).pack(anchor="w", pady=(6, 0))

            state, detail = self.connection_states.get(
                name, ("checking", "Checking connection...")
            )

            detail_label = tk.Label(
                card,
                text=detail,
                bg=COLORS["panel"],
                fg=COLORS["subtle"],
                font=(FONT, 9, "bold"),
                wraplength=300,
                justify="left",
            )
            detail_label.pack(anchor="w", pady=(8, 0))

            self.connection_details[name] = detail_label

            self._add_capacity_bar(card, path, name)


    def _build_daily_page(self) -> None:
        assert self.content is not None

        today = time.strftime("%Y-%m-%d")
        cloud_names = {"iCloud Drive", "OneDrive", "Google Drive", "Dropbox"}

        self._load_cloud_usage()
        self.daily_cloud_card_widgets = {}

        def selectable_text(parent, value, font=None):
            widget = tk.Entry(
                parent,
                font=font or (MONO, 9),
                relief="flat",
                bd=0,
                bg=COLORS["panel"],
                fg=COLORS["muted"],
                readonlybackground=COLORS["panel"],
                highlightthickness=0,
                takefocus=True,
            )
            widget.insert(0, str(value))
            widget.configure(state="readonly")

            def select_all(event, target=widget):
                target.configure(state="normal")
                target.selection_range(0, "end")
                target.icursor("end")
                target.configure(state="readonly")
                return "break"

            def copy_selection(event, target=widget):
                try:
                    selected = target.selection_get()
                except tk.TclError:
                    selected = target.get()
                target.clipboard_clear()
                target.clipboard_append(selected)
                return "break"

            widget.bind("<Command-a>", select_all)
            widget.bind("<Control-a>", select_all)
            widget.bind("<Command-c>", copy_selection)
            widget.bind("<Control-c>", copy_selection)
            widget.pack(fill="x", anchor="w")
            return widget

        def size_text(value):
            if value >= 1024 ** 4:
                return f"{value / (1024 ** 4):.2f} TB"
            return f"{value / (1024 ** 3):.2f} GB"

        def draw_bar(canvas, percent):
            width = max(1, canvas.winfo_width())
            height = 6
            color = (
                COLORS["accent"] if percent < 70
                else COLORS["amber"] if percent < 90
                else COLORS["red"]
            )
            canvas.delete("all")
            canvas.create_rectangle(
                0, 0, width, height,
                fill=COLORS["panel"],
                outline="",
            )
            canvas.create_rectangle(
                0, 0, width * min(100, max(0, percent)) / 100,
                height,
                fill=color,
                outline="",
            )

        heading = tk.Frame(self.content, bg=COLORS["bg"])
        heading.pack(fill="x", pady=(0, 12))

        tk.Label(
            heading,
            text="DAILY BACKUP DESTINATIONS",
            bg=COLORS["bg"],
            fg=COLORS["text"],
            font=(FONT, 17, "bold"),
        ).pack(anchor="w")

        tk.Label(
            heading,
            text=f"Backup date folder: Backup-{today}  |  Select the destinations you want to review.",
            bg=COLORS["bg"],
            fg=COLORS["muted"],
            font=(FONT, 11),
        ).pack(anchor="w", pady=(4, 0))

        columns = tk.Frame(self.content, bg=COLORS["bg"])
        columns.pack(fill="both", expand=True)
        columns.grid_columnconfigure(0, weight=1, uniform="destination_group")
        columns.grid_columnconfigure(1, weight=1, uniform="destination_group")

        self.connection_dots = getattr(self, "connection_dots", {})
        self.connection_details = getattr(self, "connection_details", {})

        for column, (group_name, destination_names) in enumerate(
            DAILY_DESTINATION_GROUPS.items()
        ):
            group = self._card(columns, 10)
            group.grid(
                row=0,
                column=column,
                sticky="nsew",
                padx=(0, 7) if column == 0 else (7, 0),
            )

            icon = "▣" if "Physical" in group_name else "☁"
            tk.Label(
                group,
                text=f"{icon}  {group_name.upper()}",
                bg=COLORS["panel"],
                fg=COLORS["text"],
                font=(FONT, 14, "bold"),
            ).pack(anchor="w", pady=(0, 8))

            for name in destination_names:
                target_base = DAILY_DESTINATIONS.get(name)
                date_target = (
                    target_base / f"Backup-{today}"
                    if target_base is not None else None
                )

                row = tk.Frame(
                    group,
                    bg=COLORS["panel_alt"],
                    highlightbackground=COLORS["border"],
                    highlightthickness=1,
                    padx=9,
                    pady=8,
                )
                row.pack(fill="x", pady=4)

                top = tk.Frame(row, bg=COLORS["panel_alt"])
                top.pack(fill="x")

                tk.Label(
                    top,
                    text=name,
                    bg=COLORS["panel_alt"],
                    fg=COLORS["text"],
                    font=(FONT, 12, "bold"),
                ).pack(side="left", anchor="w")

                checkbox = tk.Checkbutton(
                    top,
                    variable=self.daily_destination_vars[name],
                    onvalue=True,
                    offvalue=False,
                    bg=COLORS["panel_alt"],
                    activebackground=COLORS["panel_alt"],
                    selectcolor=COLORS["panel"],
                    relief="flat",
                    bd=0,
                    highlightthickness=0,
                    cursor="hand2",
                )
                checkbox.pack(side="right", padx=(5, 0))

                status_row = tk.Frame(row, bg=COLORS["panel_alt"])
                status_row.pack(fill="x", pady=(4, 4))

                dot = tk.Label(
                    status_row,
                    text="●",
                    bg=COLORS["panel_alt"],
                    fg=COLORS["amber"],
                    font=(FONT, 18, "bold"),
                )
                dot.pack(side="left", padx=(0, 6))

                initial_detail = self.connection_states.get(
                    name, ("checking", "Checking connection...")
                )[1]

                detail = tk.Label(
                    status_row,
                    text=initial_detail,
                    bg=COLORS["panel_alt"],
                    fg=COLORS["muted"],
                    font=(FONT, 10),
                    anchor="w",
                    justify="left",
                    wraplength=360,
                )
                detail.pack(side="left", fill="x", expand=True)

                self.connection_dots[name] = dot
                self.connection_details[name] = detail

                tk.Label(
                    row,
                    text="DESTINATION PATH",
                    bg=COLORS["panel_alt"],
                    fg=COLORS["subtle"],
                    font=(FONT, 9, "bold"),
                ).pack(anchor="w", pady=(3, 1))

                selectable_text(
                    row,
                    str(date_target) if date_target else "Destination folder not detected",
                    font=(MONO, 9),
                )

                if name in cloud_names:
                    total = self._cloud_totals_gb[name]
                    used = self.cloud_usage_gb[name]
                    free = total - used
                    percent = used / total * 100 if total else 0

                    tk.Label(
                        row,
                        text="CLOUD PROVIDER STORAGE — MANUAL SNAPSHOT (NOT LIVE)",
                        bg=COLORS["panel_alt"],
                        fg=COLORS["subtle"],
                        font=(FONT, 9, "bold"),
                    ).pack(anchor="w", pady=(7, 2))

                    summary = tk.Label(
                        row,
                        text=(
                            f"{percent:.2f}% used  |  "
                            f"{self._format_cloud_size(used)} used  |  "
                            f"{self._format_cloud_size(free)} free / "
                            f"{self._format_cloud_size(total)}"
                        ),
                        bg=COLORS["panel_alt"],
                        fg=COLORS["muted"],
                        font=(FONT, 9, "bold"),
                        anchor="w",
                        justify="left",
                        wraplength=360,
                    )
                    summary.pack(anchor="w", pady=(2, 4))

                    bar = tk.Canvas(
                        row,
                        height=6,
                        bg=COLORS["panel"],
                        highlightthickness=0,
                        bd=0,
                    )
                    bar.pack(fill="x", pady=(2, 4))
                    bar.bind(
                        "<Configure>",
                        lambda event, canvas=bar, value=percent:
                            draw_bar(canvas, value),
                    )
                    bar.after_idle(lambda canvas=bar, value=percent: draw_bar(canvas, value))

                    link_row = tk.Frame(row, bg=COLORS["panel_alt"])
                    link_row.pack(fill="x", pady=(5, 2))

                    link = tk.Label(
                        link_row,
                        text="OPEN ↗",
                        bg=COLORS["panel_alt"],
                        fg=COLORS["blue"],
                        font=(FONT, 9, "bold"),
                        cursor="hand2",
                    )
                    link.pack(side="left")
                    link.bind(
                        "<Button-1>",
                        lambda event, provider=name:
                            self._open_cloud_provider(provider),
                    )

                    input_row = tk.Frame(row, bg=COLORS["panel_alt"])
                    input_row.pack(fill="x", pady=(5, 0))

                    tk.Label(
                        input_row,
                        text="Used GB",
                        bg=COLORS["panel_alt"],
                        fg=COLORS["subtle"],
                        font=(FONT, 9),
                    ).pack(side="left")

                    entry = tk.Entry(
                        input_row,
                        width=11,
                        font=(FONT, 10),
                        relief="solid",
                        bd=1,
                    )
                    entry.insert(0, f"{used:.4f}".rstrip("0").rstrip("."))
                    entry.pack(side="left", padx=(6, 5))

                    update_button = self._make_cloud_update_button(
                        input_row, font_size=9
                    )
                    update_button.pack(side="left")

                    snapshot_status = tk.Label(
                        row,
                        text="MANUAL STORAGE SNAPSHOT",
                        bg=COLORS["panel_alt"],
                        fg=COLORS["subtle"],
                        font=(FONT, 8),
                    )
                    snapshot_status.pack(anchor="w", pady=(4, 0))

                    cloud_widgets = {
                        "summary": summary,
                        "bar": bar,
                        "entry": entry,
                        "status": snapshot_status,
                    }
                    self.daily_cloud_card_widgets[name] = cloud_widgets

                    update_button.bind(
                        "<Button-1>",
                        lambda event, provider=name:
                            self._update_cloud_card(
                                provider,
                                self.daily_cloud_card_widgets[provider],
                            ),
                    )

                    tk.Label(
                        row,
                        text="Cloud provider sync: NOT VERIFIED",
                        bg=COLORS["panel_alt"],
                        fg=COLORS["amber"],
                        font=(FONT, 9, "bold"),
                    ).pack(anchor="w", pady=(4, 0))

                else:
                    capacity_path = (
                        WINDOWS_MOUNTS.get(name, date_target)
                        if name in WINDOWS_MOUNTS else date_target
                    )
                    usage = self._capacity_for_path(capacity_path, name)

                    tk.Label(
                        row,
                        text="LOCAL FILESYSTEM CAPACITY",
                        bg=COLORS["panel_alt"],
                        fg=COLORS["subtle"],
                        font=(FONT, 9, "bold"),
                    ).pack(anchor="w", pady=(7, 2))

                    if usage is None:
                        capacity_line = (
                            "Free: unavailable  |  Used: unavailable  |  "
                            "Total: unavailable"
                        )
                        percent = 0.0
                    else:
                        total, used, free = usage.total, usage.used, usage.free
                        percent = used / total * 100 if total else 0.0
                        capacity_line = (
                            f"Free: {size_text(free)}  |  "
                            f"Used: {size_text(used)}  |  "
                            f"Total: {size_text(total)}  |  "
                            f"{percent:.1f}% used"
                        )

                    selectable_text(row, capacity_line, font=(FONT, 9))

                    bar = tk.Canvas(
                        row,
                        height=6,
                        bg=COLORS["panel"],
                        highlightthickness=0,
                        bd=0,
                    )
                    bar.pack(fill="x", pady=(5, 2))
                    bar.bind(
                        "<Configure>",
                        lambda event, canvas=bar, value=percent:
                            draw_bar(canvas, value),
                    )
                    bar.after_idle(lambda canvas=bar, value=percent: draw_bar(canvas, value))

        footer = tk.Frame(self.content, bg=COLORS["bg"])
        footer.pack(fill="x", pady=(12, 4))

        tk.Label(
            footer,
            text="No backup starts on this screen. Review and approval are required first.",
            bg=COLORS["bg"],
            fg=COLORS["muted"],
            font=(FONT, 10),
        ).pack(side="left", anchor="w")

        self._action_button(
            footer,
            "REVIEW SELECTED DESTINATIONS  →",
            self._show_daily_review,
            True,
        ).pack(side="right")


    def _path_row(self, parent: tk.Widget, label: str, path: Path) -> None:
        wrapper = tk.Frame(parent, bg=COLORS["panel"])
        wrapper.pack(fill="x", pady=5)
        tk.Label(wrapper, text=label, bg=COLORS["panel"], fg=COLORS["accent"], font=(FONT, 11, "bold"), width=15, anchor="w").pack(anchor="w")
        tk.Label(
            wrapper,
            text=self._display_path(path),
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=(MONO, 10),
            wraplength=420,
            justify="left",
        ).pack(anchor="w", pady=(3, 5))

    def _build_manual_page(self) -> None:
        assert self.content is not None

        intro = tk.Frame(self.content, bg=COLORS["bg"])
        intro.pack(fill="x", pady=(0, 14))
        tk.Label(
            intro,
            text="Select a folder and decide where it should go.",
            bg=COLORS["bg"], fg=COLORS["text"],
            font=(FONT, 18, "bold")
        ).pack(anchor="w")
        tk.Label(
            intro,
            text="Choose a source folder, select destinations, and review before proceeding.",
            bg=COLORS["bg"], fg=COLORS["muted"],
            font=(FONT, 11)
        ).pack(anchor="w", pady=(4, 0))

        top_row = tk.Frame(self.content, bg=COLORS["bg"])
        top_row.pack(fill="x", pady=(0, 12))

        source_card = self._card(top_row, 15)
        source_card.pack(side="left", fill="both", expand=True, padx=(0, 9))
        tk.Label(
            source_card, text="01  SOURCE FOLDER",
            bg=COLORS["panel"], fg=COLORS["text"],
            font=(FONT, 12, "bold")
        ).pack(anchor="w")

        source_row = tk.Frame(source_card, bg=COLORS["panel"])
        source_row.pack(fill="x", pady=(10, 0))
        self.source_entry = tk.Entry(
            source_row, textvariable=self.selected_source,
            bg=COLORS["panel_alt"], fg=COLORS["text"],
            insertbackground=COLORS["text"], relief="flat",
            bd=0, font=(MONO, 10)
        )
        self.source_entry.pack(
            side="left", fill="x", expand=True,
            ipady=9, padx=(0, 8)
        )
        self.source_entry.bind(
            "<KeyRelease>", lambda event: self._update_manual_info()
        )
        self._make_pill_button(
            source_row, "BROWSE FOLDER", self._browse_source,
            primary=False,
            normal_color="#F28C28",
            hover_color="#D97706",
            text_color_override="#FFFFFF",
            shadow_color_override="#B85C00",
            outline_color_override="#FFB15C"
        ).pack(side="right")

        tk.Label(
            source_card,
            text="The selected folder structure will be preserved.",
            bg=COLORS["panel"], fg=COLORS["muted"],
            font=(FONT, 10)
        ).pack(anchor="w", pady=(8, 0))

        info_card = self._card(top_row, 15)
        info_card.pack(side="right", fill="y")
        info_card.configure(width=275)
        info_card.pack_propagate(False)

        tk.Label(
            info_card, text="ARCHIVE INFORMATION",
            bg=COLORS["panel"], fg=COLORS["accent"],
            font=(FONT, 12, "bold")
        ).pack(anchor="w", pady=(0, 8))

        self.manual_info_labels = {}
        for key, heading, initial in [
            ("source", "SOURCE FOLDER", "Not selected"),
            ("count", "DESTINATIONS SELECTED", "0"),
            ("destinations", "DESTINATION SUMMARY", "None selected"),
            ("space", "AVAILABLE SPACE", "Not calculated"),
            ("check", "PATH CHECK", "Not checked"),
        ]:
            tk.Label(
                info_card, text=heading,
                bg=COLORS["panel"], fg=COLORS["subtle"],
                font=(FONT, 8, "bold")
            ).pack(anchor="w", pady=(5, 1))
            value = tk.Label(
                info_card, text=initial,
                bg=COLORS["panel"], fg=COLORS["text"],
                font=(FONT, 9), wraplength=245, justify="left"
            )
            value.pack(anchor="w")
            self.manual_info_labels[key] = value

        self.manual_info_check = "Not checked"

        destination_card = self._card(self.content, 15)
        destination_card.pack(fill="x", pady=(0, 12))
        tk.Label(
            destination_card, text="02  BACKUP DESTINATIONS",
            bg=COLORS["panel"], fg=COLORS["text"],
            font=(FONT, 12, "bold")
        ).pack(anchor="w")
        tk.Label(
            destination_card,
            text="Select destinations and choose the target folder on each drive.",
            bg=COLORS["panel"], fg=COLORS["muted"],
            font=(FONT, 10)
        ).pack(anchor="w", pady=(4, 8))

        grid = tk.Frame(destination_card, bg=COLORS["panel"])
        grid.pack(fill="x")
        self.manual_status_labels = {}
        self.manual_path_labels = {}

        for index, (name, path) in enumerate(MANUAL_DESTINATIONS.items()):
            row, col = divmod(index, 4)
            tile = tk.Frame(
                grid, bg=COLORS["panel_alt"],
                highlightbackground=COLORS["border"],
                highlightthickness=1, padx=8, pady=7
            )
            tile.grid(row=row, column=col, sticky="nsew", padx=4, pady=4)
            grid.grid_columnconfigure(col, weight=1, uniform="destinations")

            name_row = tk.Frame(tile, bg=COLORS["panel_alt"])
            name_row.pack(fill="x", anchor="w")

            tk.Checkbutton(
                name_row, text="", variable=self.destination_vars[name],
                command=self._update_manual_info,
                bg=COLORS["panel_alt"], fg=COLORS["text"],
                activebackground=COLORS["panel_alt"],
                activeforeground=COLORS["accent"],
                selectcolor=COLORS["panel"],
                font=(FONT, 9, "bold"), anchor="w",
                cursor="hand2"
            ).pack(side="left", padx=(0, 5))

            state = self.connection_states.get(
                name, ("checking", "Checking connection...")
            )[0]
            initial_dot_color = {
                "connected": COLORS["accent"],
                "checking": COLORS["amber"],
                "disconnected": COLORS["red"],
            }.get(state, COLORS["amber"])

            dot = tk.Label(
                name_row,
                text="●",
                bg=COLORS["panel_alt"],
                fg=initial_dot_color,
                font=(FONT, 18, "bold"),
            )
            dot.pack(side="left", padx=(0, 6))

            self.connection_dots[name] = dot

            tk.Label(
                name_row,
                text=name,
                bg=COLORS["panel_alt"],
                fg=COLORS["text"],
                font=(FONT, 9, "bold"),
                anchor="w",
                justify="left",
                wraplength=135,
            ).pack(side="left", anchor="w")

            path_label = tk.Label(
                tile,
                text=self._display_path(self.manual_destination_paths.get(name)),
                bg=COLORS["panel_alt"], fg="#38BDF8",
                font=(MONO, 8, "bold"), wraplength=180, justify="left"
            )
            path_label.pack(anchor="w", padx=3, pady=(4, 5))
            self.manual_path_labels[name] = path_label

            self._make_pill_button(
                tile,
                "CHOOSE FOLDER",
                lambda key=name: self._choose_destination_folder(key),
                primary=False,
                width=14,
                height=38,
                normal_color="#5B9E68",
                hover_color="#478856",
                text_color_override="#FFFFFF",
                shadow_color_override="#397447",
                outline_color_override="#76B982"
            ).pack(anchor="w", pady=(1, 4))

            status = tk.Label(
                tile, text=self._destination_status(name),
                bg=COLORS["panel_alt"], fg="#22C55E",
                font=(FONT, 9, "bold"),
                wraplength=180, justify="left"
            )
            status.pack(anchor="w")
            self.manual_status_labels[name] = status

        check_row = tk.Frame(destination_card, bg=COLORS["panel"])
        check_row.pack(fill="x", pady=(9, 0))
        self._make_pill_button(
            check_row, "CHECK LOCAL PATH STATUS",
            self._refresh_manual_status, primary=False
        ).pack(side="left")
        tk.Label(
            check_row,
            text="Local write access only; cloud sync is not confirmed.",
            bg=COLORS["panel"], fg=COLORS["muted"],
            font=(FONT, 9)
        ).pack(side="left", padx=9)

        review_row = tk.Frame(self.content, bg=COLORS["bg"])
        review_row.pack(fill="x", pady=(0, 12))

        self._make_pill_button(
            review_row,
            "REVIEW SELECTED BACKUP  →",
            self._show_manual_review,
            primary=True
        ).pack(side="right")

        tk.Label(
            review_row,
            text="Review opens first. No backup starts from this prototype.",
            bg=COLORS["bg"],
            fg=COLORS["muted"],
            font=(FONT, 10),
        ).pack(side="right", padx=12)

        self._update_manual_info()

    def _update_manual_info(self) -> None:
        labels = getattr(self, "manual_info_labels", None)
        if not labels:
            return

        source = self.selected_source.get().strip()
        selected = self._selected_destinations()

        labels["source"].configure(
            text=Path(source).name if source else "Not selected"
        )
        labels["count"].configure(text=str(len(selected)))
        labels["destinations"].configure(
            text=", ".join(name for name, path in selected)
            if selected else "None selected"
        )

        space_lines = []
        for name, target in selected:
            if target is None:
                space_lines.append(f"{name}: unavailable")
                continue
            candidate = target.expanduser()
            while not candidate.exists() and candidate != candidate.parent:
                candidate = candidate.parent
            try:
                if candidate.is_dir():
                    free_gb = shutil.disk_usage(candidate).free / (1024 ** 3)
                    space_lines.append(f"{name}: {free_gb:.1f} GB free")
                else:
                    space_lines.append(f"{name}: unavailable")
            except OSError:
                space_lines.append(f"{name}: unavailable")

        labels["space"].configure(
            text="\n".join(space_lines) if space_lines else "Select a destination"
        )
        labels["check"].configure(
            text=getattr(self, "manual_info_check", "Not checked")
        )

    def _refresh_manual_status(self):
        for name, label in getattr(self, "manual_status_labels", {}).items():
            target = self.manual_destination_paths.get(name)

            if target is None:
                label.configure(
                    text="FOLDER NOT DETECTED",
                    fg=COLORS["red"],
                )
                continue

            candidate = target.expanduser()

            while not candidate.exists() and candidate != candidate.parent:
                candidate = candidate.parent

            if not candidate.is_dir():
                label.configure(
                    text="PATH NOT AVAILABLE",
                    fg=COLORS["red"],
                )
                continue

            try:
                fd, test_path = tempfile.mkstemp(
                    prefix=".smartbackup_write_test_",
                    dir=str(candidate),
                )
                os.close(fd)
                os.remove(test_path)

                if name in {
                    "OneDrive",
                    "iCloud Drive",
                    "Google Drive",
                    "Dropbox",
                }:
                    label.configure(
                        text="LOCAL WRITE OK · CLOUD SYNC UNVERIFIED",
                        fg=COLORS["amber"],
                    )
                else:
                    label.configure(
                        text="LOCAL WRITE OK",
                        fg=COLORS["accent"],
                    )

            except OSError as exc:
                label.configure(
                    text=f"WRITE FAILED: {exc}",
                    fg=COLORS["red"],
                )

        self.manual_info_check = "Path check completed"
        self._update_manual_info()

    def _browse_source(self) -> None:
        selected = filedialog.askdirectory(title="Select Source Folder", mustexist=True)
        if selected:
            self.selected_source.set(selected)
            self._update_manual_info()

    def _choose_destination_folder(self, name):
        selected = filedialog.askdirectory(
            title=f"Choose target folder for {name}",
            mustexist=True,
        )

        if selected:
            self.manual_destination_paths[name] = Path(selected)

            label = getattr(self, "manual_path_labels", {}).get(name)
            if label:
                label.configure(text=selected)

            self._refresh_manual_status()
            self._update_manual_info()

    def _selected_destinations(self) -> list[tuple[str, Path | None]]:
        return [
            (name, self.manual_destination_paths.get(name))
            for name in MANUAL_DESTINATIONS
            if self.destination_vars[name].get()
        ]

    def _show_manual_review(self) -> None:
        source = self.selected_source.get().strip()
        destinations = self._selected_destinations()

        if not source:
            messagebox.showinfo("Source Required", "Choose a source folder before opening the review window.")
            return
        if not Path(source).is_dir():
            messagebox.showwarning("Source Not Available", "The selected source folder is not currently available.")
            return
        if not destinations:
            messagebox.showinfo("Destination Required", "Select at least one backup destination.")
            return

        self._open_review_window(
            mode="Manual Archive Backup",
            sources=[("Selected folder", Path(source))],
            destinations=destinations,
        )

    def _show_daily_review(self) -> None:
        selected = [
            name for name, variable in self.daily_destination_vars.items()
            if variable.get()
        ]

        if not selected:
            messagebox.showwarning(
                "No Destinations Selected",
                "Select at least one destination before reviewing."
            )
            return

        today = time.strftime("%Y-%m-%d")
        destinations = []

        for name in selected:
            base_path = DAILY_DESTINATIONS.get(name)
            target_path = (
                base_path / f"Backup-{today}"
                if base_path is not None else None
            )
            destinations.append((name, target_path))

        self._open_review_window(
            mode="Daily Smart Backup",
            sources=[
                ("Documents", DAILY_SOURCES[0]),
                ("Downloads", DAILY_SOURCES[1]),
            ],
            destinations=destinations,
        )


    def _open_review_window(self, mode: str, sources: list[tuple[str, Path]], destinations: list[tuple[str, Path | None]]) -> None:
        window = tk.Toplevel(self.root)
        window.title(f"SmartBackup | {mode} Review")
        window.geometry("1000x740")
        window.minsize(850, 620)
        window.configure(bg=COLORS["bg"])
        window.transient(self.root)
        window.grab_set()

        outer = tk.Frame(window, bg=COLORS["bg"], padx=25, pady=22)
        outer.pack(fill="both", expand=True)

        tk.Label(outer, text="REVIEW BEFORE BACKUP", bg=COLORS["bg"], fg=COLORS["accent"], font=(FONT, 11, "bold")).pack(anchor="w")
        tk.Label(outer, text=mode, bg=COLORS["bg"], fg=COLORS["text"], font=(FONT, 25, "bold")).pack(anchor="w", pady=(5, 4))
        tk.Label(
            outer,
            text="Confirm the source and selected destinations before the backup engine runs.",
            bg=COLORS["bg"],
            fg=COLORS["muted"],
            font=(FONT, 12),
        ).pack(anchor="w", pady=(0, 16))

        source_card = self._card(outer, 15)
        source_card.pack(fill="x", pady=(0, 12))
        tk.Label(source_card, text="SOURCE", bg=COLORS["panel"], fg=COLORS["blue"], font=(FONT, 11, "bold")).pack(anchor="w", pady=(0, 7))
        for label, path in sources:
            tk.Label(source_card, text=label, bg=COLORS["panel"], fg=COLORS["text"], font=(FONT, 18, "bold")).pack(anchor="w")
            tk.Label(source_card, text=str(path), bg=COLORS["panel"], fg=COLORS["muted"], font=(MONO, 10), wraplength=760, justify="left").pack(anchor="w", pady=(2, 8))

        dest_card = self._card(outer, 15)
        dest_card.pack(fill="both", expand=True, pady=(0, 12))
        tk.Label(dest_card, text="SELECTED DESTINATIONS", bg=COLORS["panel"], fg=COLORS["accent"], font=(FONT, 11, "bold")).pack(anchor="w", pady=(0, 8))
        review_status_labels: dict[str, tk.Label] = {}
        for label, path in destinations:
            row = tk.Frame(dest_card, bg=COLORS["panel"])
            row.pack(fill="x", pady=7)
            tk.Label(row, text="●", bg=COLORS["panel"], fg=COLORS["accent"], font=(FONT, 13)).pack(side="left")
            name_frame = tk.Frame(row, bg=COLORS["panel"])
            name_frame.pack(side="left", fill="x", expand=True, padx=(10, 10))
            tk.Label(name_frame, text=label, bg=COLORS["panel"], fg=COLORS["text"], font=(FONT, 13, "bold")).pack(anchor="w")
            tk.Label(name_frame, text=self._display_path(path), bg=COLORS["panel"], fg=COLORS["muted"], font=(MONO, 9), wraplength=770, justify="left").pack(anchor="w", pady=(3, 0))
            status = tk.Label(row, text="CHECK PENDING", bg=COLORS["amber_dark"], fg=COLORS["amber"], padx=10, pady=6, font=(FONT, 10, "bold"))
            status.pack(side="right")
            review_status_labels[label] = status

        def check_review_paths() -> None:
            cloud_names = {"OneDrive", "iCloud Drive", "Google Drive", "Dropbox"}
            mounts = globals().get("WINDOWS_MOUNTS", {})

            for label, path in destinations:
                status_text = "DESTINATION NOT CONFIGURED"

                try:
                    if path is None:
                        raise FileNotFoundError("Destination folder is not configured.")

                    target = Path(path).expanduser().resolve(strict=False)

                    if label in mounts:
                        mount = Path(mounts[label]).expanduser()
                        if not mount.is_mount():
                            raise FileNotFoundError("NETWORK VOLUME NOT MOUNTED")
                        try:
                            target.relative_to(mount.resolve())
                        except ValueError:
                            raise OSError("Selected folder is outside the mounted drive.")

                    sync_warning = ""
                    if label in cloud_names:
                        sync_root = self._connection_path(label)
                        if not sync_root or not Path(sync_root).expanduser().is_dir():
                            raise FileNotFoundError("SYNC ROOT NOT DETECTED")
                        try:
                            target.relative_to(Path(sync_root).expanduser().resolve())
                            sync_warning = " · CLOUD SYNC UNVERIFIED"
                        except ValueError:
                            sync_warning = " · OUTSIDE SYNC ROOT"

                    if target.exists() and not target.is_dir():
                        raise NotADirectoryError("Destination is not a folder.")

                    candidate = target
                    while not candidate.exists() and candidate != candidate.parent:
                        candidate = candidate.parent

                    if not candidate.is_dir():
                        raise FileNotFoundError("No available parent folder.")

                    fd, test_path = tempfile.mkstemp(
                        prefix=".smartbackup_review_test_",
                        dir=str(candidate),
                    )
                    os.close(fd)
                    os.remove(test_path)

                    if target.exists():
                        status_text = "LOCAL WRITE OK" + sync_warning
                    else:
                        status_text = "PARENT WRITE OK · TARGET WILL BE CREATED" + sync_warning

                except Exception as exc:
                    status_text = str(exc)

                is_problem = any(
                    token in status_text
                    for token in (
                        "NOT CONFIGURED",
                        "NOT MOUNTED",
                        "NOT DETECTED",
                        "FAILED",
                        "outside the mounted drive",
                        "not a folder",
                        "No available parent",
                    )
                )
                is_warning = (
                    "UNVERIFIED" in status_text
                    or "OUTSIDE SYNC ROOT" in status_text
                    or "TARGET WILL BE CREATED" in status_text
                )

                review_status_labels[label].configure(
                    text=status_text,
                    bg=(
                        COLORS["red_dark"] if is_problem
                        else COLORS["amber_dark"] if is_warning
                        else COLORS["blue_dark"]
                    ),
                    fg=(
                        COLORS["red"] if is_problem
                        else COLORS["amber"] if is_warning
                        else COLORS["blue"]
                    ),
                )

            messagebox.showinfo(
                "Destination Path Check",
                "The selected destination folders were checked for local path access and write permission. "
                "Cloud upload and remote file integrity are not verified.",
            )

        item_card = self._card(outer, 15)
        item_card.pack(fill="x", pady=(0, 12))
        tk.Label(
            item_card,
            text="ITEM REVIEW",
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=(FONT, 11, "bold"),
        ).pack(anchor="w")
        placeholder_text = (
            "The daily scan results, file list, counts, and total size will appear here after the existing backup engine is connected."
            if mode == "Daily Smart Backup"
            else "The selected folder contents, file count, total size, and scan results will appear here after the archive scanner is connected."
        )
        tk.Label(
            item_card,
            text=placeholder_text,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=(FONT, 11),
            wraplength=760,
            justify="left",
        ).pack(anchor="w", pady=(7, 0))

        notice = tk.Frame(outer, bg=COLORS["amber_dark"], padx=12, pady=10)
        notice.pack(fill="x", pady=(0, 13))
        tk.Label(
            notice,
            text="UI preview only. Path checks are read-only. No files are scanned, copied, uploaded, overwritten, or deleted.",
            bg=COLORS["amber_dark"],
            fg=COLORS["amber"],
            font=(FONT, 11, "bold"),
            wraplength=760,
            justify="left",
        ).pack(anchor="w")

        controls = tk.Frame(outer, bg=COLORS["bg"])
        controls.pack(fill="x")
        self._action_button(controls, "CANCEL", window.destroy, False).pack(side="right")
        if mode == "Manual Archive Backup":
            next_label = "START MANUAL BACKUP"
            next_command = lambda: self._start_manual_backup(
                window, sources, destinations
            )
        else:
            next_label = "CONTINUE TO PROGRESS PREVIEW"
            next_command = lambda: self._open_progress_preview(window, sources, destinations)

        self._action_button(
            controls,
            next_label,
            next_command,
            True,
        ).pack(side="right", padx=(0, 9))
        self._action_button(
            controls,
            "CHECK DESTINATION PATHS",
            check_review_paths,
            False,
        ).pack(side="left")



    def _manual_backup_format_size(self, value):
        value = float(value)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or unit == "TB":
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} TB"

    def _manual_backup_validate_destinations(self, job):
        source = job["source"]
        total_size = job["total_bytes"]
        valid = []
        errors = []
        warnings = []
        seen = set()
        device_groups = {}

        mounts = globals().get("WINDOWS_MOUNTS", {})
        cloud_names = {"OneDrive", "iCloud Drive", "Google Drive", "Dropbox"}

        for name, raw_path in job["destinations"]:
            if raw_path is None:
                errors.append(f"{name}: destination is not configured.")
                continue

            try:
                target = Path(raw_path).expanduser().resolve(strict=False)

                if target in seen:
                    errors.append(f"{name}: duplicate destination folder selected.")
                    continue
                seen.add(target)

                try:
                    target.relative_to(source)
                    errors.append(
                        f"{name}: destination is inside the source folder. "
                        "Choose a separate destination."
                    )
                    continue
                except ValueError:
                    pass

                if target.exists() and not target.is_dir():
                    errors.append(f"{name}: destination path is not a folder.")
                    continue

                if name in mounts:
                    mount = Path(mounts[name]).expanduser()
                    if not mount.is_mount():
                        errors.append(f"{name}: drive is not mounted.")
                        continue
                    try:
                        target.relative_to(mount.resolve())
                    except ValueError:
                        errors.append(f"{name}: selected folder is outside its mounted drive.")
                        continue

                if name in cloud_names:
                    sync_root = self._connection_path(name)
                    if not sync_root or not Path(sync_root).expanduser().is_dir():
                        errors.append(f"{name}: local cloud sync folder is not detected.")
                        continue
                    try:
                        target.relative_to(Path(sync_root).expanduser().resolve())
                    except ValueError:
                        warnings.append(
                            f"{name}: selected folder is outside the detected sync root. "
                            "Only a local folder copy can be confirmed."
                        )

                candidate = target
                while not candidate.exists() and candidate != candidate.parent:
                    candidate = candidate.parent

                if not candidate.is_dir():
                    errors.append(f"{name}: no existing parent folder is available.")
                    continue

                fd, test_path = tempfile.mkstemp(
                    prefix=".smartbackup_preflight_",
                    dir=str(candidate),
                )
                os.close(fd)
                os.remove(test_path)

                device = os.stat(candidate).st_dev
                free_bytes = shutil.disk_usage(candidate).free

                valid.append((name, target, candidate, device))
                device_groups.setdefault(device, []).append((name, free_bytes))

            except Exception as exc:
                errors.append(f"{name}: {exc}")

        for device, entries in device_groups.items():
            available = min(item[1] for item in entries)
            required = total_size * len(entries)
            names = ", ".join(item[0] for item in entries)

            if available < required:
                errors.append(
                    f"{names}: insufficient local free space. "
                    f"Available {self._manual_backup_format_size(available)}, "
                    f"required approximately {self._manual_backup_format_size(required)}."
                )

        return valid, errors, warnings

    def _start_manual_backup(self, parent, sources, destinations):
        if getattr(self, "_manual_backup_active", False):
            messagebox.showinfo(
                "Backup Running",
                "A Manual Archive backup is already running.",
            )
            return

        if not sources:
            messagebox.showwarning("Source Required", "No source folder was provided.")
            return

        try:
            source = Path(sources[0][1]).expanduser().resolve(strict=True)
        except Exception as exc:
            messagebox.showerror("Source Error", str(exc))
            return

        if not source.is_dir():
            messagebox.showerror("Source Error", "The selected source is not a folder.")
            return

        if not destinations:
            messagebox.showwarning(
                "Destination Required",
                "Select at least one backup destination.",
            )
            return

        window = tk.Toplevel(self.root)
        window.title("SmartBackup | Manual Backup Progress")
        window.geometry("1080x780")
        window.minsize(900, 650)
        window.configure(bg=COLORS["bg"])
        window.transient(self.root)

        body = tk.Frame(window, bg=COLORS["bg"], padx=24, pady=20)
        body.pack(fill="both", expand=True)

        tk.Label(
            body,
            text="LIVE TRANSFER MONITOR",
            bg=COLORS["bg"],
            fg=COLORS["accent"],
            font=(FONT, 11, "bold"),
        ).pack(anchor="w")

        tk.Label(
            body,
            text="Manual Archive Backup",
            bg=COLORS["bg"],
            fg=COLORS["text"],
            font=(FONT, 24, "bold"),
        ).pack(anchor="w", pady=(4, 12))

        summary = tk.Frame(body, bg=COLORS["bg"])
        summary.pack(fill="x", pady=(0, 12))

        summary_labels = {}
        for key, heading, color in (
            ("progress", "OVERALL PROGRESS", COLORS["accent"]),
            ("files", "FILES PROCESSED", COLORS["blue"]),
            ("eta", "ESTIMATED REMAINING", COLORS["amber"]),
        ):
            card = self._card(summary, 13)
            card.pack(side="left", fill="both", expand=True, padx=(0, 8))
            tk.Label(
                card,
                text=heading,
                bg=COLORS["panel"],
                fg=COLORS["muted"],
                font=(FONT, 10, "bold"),
            ).pack(anchor="w")
            value = tk.Label(
                card,
                text="0%",
                bg=COLORS["panel"],
                fg=color,
                font=(FONT, 22, "bold"),
            )
            value.pack(anchor="w", pady=(5, 0))
            summary_labels[key] = value

        progress_card = self._card(body, 14)
        progress_card.pack(fill="x", pady=(0, 12))

        progress_header = tk.Frame(progress_card, bg=COLORS["panel"])
        progress_header.pack(fill="x")

        tk.Label(
            progress_header,
            text="TRANSFER PROGRESS",
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=(FONT, 14, "bold"),
        ).pack(side="left")

        progress_percent = tk.Label(
            progress_header,
            text="0%",
            bg=COLORS["panel"],
            fg=COLORS["accent"],
            font=(FONT, 13, "bold"),
        )
        progress_percent.pack(side="right")

        progress_bar = ttk.Progressbar(
            progress_card,
            maximum=100,
            value=0,
            style="Smart.Horizontal.TProgressbar",
        )
        progress_bar.pack(fill="x", pady=(12, 8))

        detail_label = tk.Label(
            progress_card,
            text="Scanning source folder. No destination files have been copied yet.",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=(FONT, 10),
            justify="left",
            wraplength=950,
        )
        detail_label.pack(anchor="w")

        table_card = self._card(body, 12)
        table_card.pack(fill="both", expand=True, pady=(0, 10))

        tk.Label(
            table_card,
            text="FILE-BY-FILE ACTIVITY",
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=(FONT, 14, "bold"),
        ).pack(anchor="w", pady=(0, 8))

        tree = ttk.Treeview(
            table_card,
            columns=("file", "destination", "status"),
            show="headings",
            style="Smart.Treeview",
            height=8,
        )
        tree.heading("file", text="FILE")
        tree.heading("destination", text="DESTINATION")
        tree.heading("status", text="STATUS")
        tree.column("file", width=420, anchor="w")
        tree.column("destination", width=230, anchor="w")
        tree.column("status", width=150, anchor="center")
        tree.pack(fill="both", expand=True)

        status_label = tk.Label(
            body,
            text="SCANNING SOURCE FOLDER...",
            bg=COLORS["bg"],
            fg=COLORS["amber"],
            font=(FONT, 11, "bold"),
            anchor="w",
            justify="left",
            wraplength=950,
        )
        status_label.pack(fill="x", pady=(0, 9))

        controls = tk.Frame(body, bg=COLORS["bg"])
        controls.pack(fill="x")

        job = {
            "source": source,
            "destinations": destinations,
            "window": window,
            "queue": queue.Queue(),
            "cancel": threading.Event(),
            "finished": False,
            "phase": "scan",
            "summary_labels": summary_labels,
            "progress_percent": progress_percent,
            "progress_bar": progress_bar,
            "detail_label": detail_label,
            "status_label": status_label,
            "tree": tree,
            "controls": controls,
            "cancel_button": None,
            "files": [],
            "dirs": [],
            "skipped": 0,
            "total_bytes": 0,
            "total_ops": 0,
            "validated_destinations": [],
            "started": None,
        }

        def request_cancel():
            if job["finished"]:
                window.destroy()
                return
            job["cancel"].set()
            try:
                status_label.configure(
                    text="CANCELLATION REQUESTED. Waiting for the current operation to stop..."
                )
            except tk.TclError:
                pass

        job["cancel_button"] = self._action_button(
            controls,
            "CANCEL BACKUP",
            request_cancel,
            False,
        )
        job["cancel_button"].pack(side="right")

        def exit_or_cancel():
            if job["finished"]:
                window.destroy()
                return

            confirmed = messagebox.askyesno(
                "Exit Backup Window",
                "The backup is still running. Request cancellation before exiting?",
                parent=window,
            )
            if confirmed:
                request_cancel()

        job["exit_button"] = self._action_button(
            controls,
            "EXIT",
            exit_or_cancel,
            False,
        )
        job["exit_button"].pack(side="right", padx=(0, 8))

        window.protocol("WM_DELETE_WINDOW", request_cancel)
        window.grab_set()

        try:
            parent.grab_release()
            parent.destroy()
        except tk.TclError:
            pass

        self._manual_backup_active = True

        worker = threading.Thread(
            target=self._manual_backup_scan_worker,
            args=(job,),
            daemon=True,
            name="SmartBackupManualScanner",
        )
        worker.start()

        window.after(100, lambda: self._manual_backup_poll(job))

    def _manual_backup_scan_worker(self, job):
        files = []
        dirs = []
        skipped = 0
        scan_errors = []
        source = job["source"]
        cancel_event = job["cancel"]

        def on_error(exc):
            scan_errors.append(str(exc))

        try:
            for root, dirnames, filenames in os.walk(
                source,
                topdown=True,
                followlinks=False,
                onerror=on_error,
            ):
                if cancel_event.is_set():
                    job["queue"].put({"type": "scan_cancelled"})
                    return

                current = Path(root)
                retained_dirs = []

                for dirname in dirnames:
                    folder = current / dirname
                    if folder.is_symlink():
                        skipped += 1
                        continue
                    retained_dirs.append(dirname)
                    dirs.append(folder.relative_to(source).as_posix())

                dirnames[:] = retained_dirs

                for filename in filenames:
                    if cancel_event.is_set():
                        job["queue"].put({"type": "scan_cancelled"})
                        return

                    path = current / filename

                    try:
                        if path.is_symlink() or not path.is_file():
                            skipped += 1
                            continue
                        size = path.stat().st_size
                        relative = path.relative_to(source).as_posix()
                        files.append((relative, size))
                    except Exception as exc:
                        scan_errors.append(f"{path}: {exc}")

            if scan_errors:
                job["queue"].put({
                    "type": "scan_error",
                    "errors": scan_errors[:20],
                    "error_count": len(scan_errors),
                })
            else:
                job["queue"].put({
                    "type": "scan_done",
                    "files": files,
                    "dirs": dirs,
                    "skipped": skipped,
                })

        except Exception as exc:
            job["queue"].put({
                "type": "scan_error",
                "errors": [str(exc)],
                "error_count": 1,
            })

    def _manual_backup_copy_worker(self, job):
        source = job["source"]
        files = job["files"]
        dirs = job["dirs"]
        destinations = job["validated_destinations"]
        cancel_event = job["cancel"]
        result_queue = job["queue"]

        archive_paths = []
        errors = []
        success_count = 0
        failed_count = 0
        processed_count = 0
        committed_bytes = 0
        written_bytes = 0
        total_ops = job["total_ops"]
        started = time.monotonic()

        try:
            stamp = time.strftime("%Y%m%d_%H%M%S")

            for name, target, candidate, device in destinations:
                if cancel_event.is_set():
                    break

                target.mkdir(parents=True, exist_ok=True)

                base_name = f"SmartBackup_Manual_{stamp}"
                archive = target / base_name
                suffix = 1

                while archive.exists():
                    archive = target / f"{base_name}_{suffix:02d}"
                    suffix += 1

                archive.mkdir(parents=False, exist_ok=False)
                archive_paths.append((name, archive))

            if cancel_event.is_set():
                result_queue.put({
                    "type": "finished",
                    "success": 0,
                    "failed": 0,
                    "processed": 0,
                    "written": 0,
                    "errors": [],
                    "cancelled": True,
                    "archive_paths": archive_paths,
                })
                return

            for name, archive in archive_paths:
                for relative in dirs:
                    (archive / Path(relative)).mkdir(parents=True, exist_ok=True)

            result_queue.put({
                "type": "copy_started",
                "archive_paths": archive_paths,
            })

            last_update = 0.0

            for name, archive in archive_paths:
                for relative, scanned_size in files:
                    if cancel_event.is_set():
                        break

                    src = source / Path(relative)
                    destination = archive / Path(relative)
                    temporary = destination.with_name(
                        f".{destination.name}.smartbackup-partial"
                    )
                    current_written = 0

                    try:
                        if src.is_symlink() or not src.is_file():
                            raise OSError("Source file is no longer available as a regular file.")

                        destination.parent.mkdir(parents=True, exist_ok=True)

                        if destination.exists():
                            raise FileExistsError("Refusing to overwrite an existing file.")

                        with src.open("rb") as input_file, temporary.open("xb") as output_file:
                            while True:
                                if cancel_event.is_set():
                                    raise InterruptedError("Backup cancelled.")

                                chunk = input_file.read(1024 * 1024)
                                if not chunk:
                                    break

                                output_file.write(chunk)
                                current_written += len(chunk)
                                written_bytes += len(chunk)

                                now = time.monotonic()
                                if now - last_update >= 0.15:
                                    fraction = (
                                        min(current_written / scanned_size, 1.0)
                                        if scanned_size > 0 else 0.0
                                    )
                                    result_queue.put({
                                        "type": "progress",
                                        "processed": processed_count,
                                        "success": success_count,
                                        "failed": failed_count,
                                        "units": processed_count + fraction,
                                        "written": committed_bytes,
                                        "current_written": current_written,
                                        "total_ops": total_ops,
                                        "current_file": relative,
                                        "current_destination": name,
                                        "started": started,
                                    })
                                    last_update = now

                        if cancel_event.is_set():
                            raise InterruptedError("Backup cancelled.")

                        shutil.copystat(src, temporary, follow_symlinks=False)

                        if destination.exists():
                            raise FileExistsError("Refusing to overwrite an existing file.")

                        temporary.replace(destination)

                        committed_bytes += current_written
                        success_count += 1
                        processed_count += 1

                        result_queue.put({
                            "type": "file",
                            "file": relative,
                            "destination": name,
                            "status": "COPIED",
                            "processed": processed_count,
                            "success": success_count,
                            "failed": failed_count,
                            "written": committed_bytes,
                            "total_ops": total_ops,
                        })

                    except InterruptedError:
                        try:
                            temporary.unlink(missing_ok=True)
                        except OSError:
                            pass
                        cancel_event.set()
                        break

                    except Exception as exc:
                        try:
                            temporary.unlink(missing_ok=True)
                        except OSError:
                            pass

                        written_bytes -= current_written
                        failed_count += 1
                        processed_count += 1
                        errors.append(f"{name} | {relative} | {exc}")

                        result_queue.put({
                            "type": "file",
                            "file": relative,
                            "destination": name,
                            "status": "FAILED",
                            "processed": processed_count,
                            "success": success_count,
                            "failed": failed_count,
                            "written": committed_bytes,
                            "total_ops": total_ops,
                        })

                if cancel_event.is_set():
                    break

            result_queue.put({
                "type": "finished",
                "success": success_count,
                "failed": failed_count,
                "processed": processed_count,
                "written": committed_bytes,
                "errors": errors[:30],
                "cancelled": cancel_event.is_set(),
                "archive_paths": archive_paths,
            })

        except Exception as exc:
            result_queue.put({
                "type": "finished",
                "success": success_count,
                "failed": failed_count,
                "processed": processed_count,
                "written": committed_bytes,
                "errors": [f"Backup setup failed: {exc}"] + errors[:20],
                "cancelled": cancel_event.is_set(),
                "archive_paths": archive_paths,
                "fatal": True,
            })

    def _manual_backup_finish(self, job, status, detail):
        job["finished"] = True
        self._manual_backup_active = False

        try:
            job["status_label"].configure(text=status)
            job["detail_label"].configure(text=detail)

            button = job.get("cancel_button")
            if button is not None:
                button.destroy()

            job["detail_label"].configure(
                text=detail + "\n\nBackup process finished. Click EXIT to close this window."
            )

            exit_button = job.get("exit_button")
            if exit_button is not None:
                exit_button.tkraise()

            job["window"].protocol(
                "WM_DELETE_WINDOW",
                job["window"].destroy,
            )
        except tk.TclError:
            pass

    def _manual_backup_poll(self, job):
        events = job["queue"]

        while True:
            try:
                event = events.get_nowait()
            except queue.Empty:
                break

            event_type = event.get("type")

            if event_type == "scan_cancelled":
                self._manual_backup_finish(
                    job,
                    "CANCELLED BEFORE COPY",
                    "The source scan was cancelled. No destination files were copied.",
                )

            elif event_type == "scan_error":
                error_text = "\n".join(event.get("errors", []))
                self._manual_backup_finish(
                    job,
                    "SOURCE SCAN FAILED — NO COPY STARTED",
                    f"Scan errors: {event.get('error_count', 0)}\n{error_text}",
                )

            elif event_type == "scan_done":
                job["files"] = event["files"]
                job["dirs"] = event["dirs"]
                job["skipped"] = event["skipped"]
                job["total_bytes"] = sum(size for relative, size in job["files"])
                job["total_ops"] = len(job["files"]) * len(job["destinations"])

                valid, errors, warnings = self._manual_backup_validate_destinations(job)

                if errors:
                    self._manual_backup_finish(
                        job,
                        "DESTINATION PREFLIGHT FAILED — NO COPY STARTED",
                        "\n".join(errors),
                    )
                    continue

                job["validated_destinations"] = valid

                destination_text = "\n".join(
                    f"{name}: {target}"
                    for name, target, candidate, device in valid
                )

                warning_text = ""
                if warnings:
                    warning_text = "\n\nWarnings:\n" + "\n".join(warnings)

                cloud_text = (
                    "\nCloud upload is not verified by this local copy operation."
                )

                confirm_text = (
                    f"Source folder:\n{job['source']}\n\n"
                    f"Files found: {len(job['files'])}\n"
                    f"Total source size: {self._manual_backup_format_size(job['total_bytes'])}\n"
                    f"Destination count: {len(valid)}\n\n"
                    f"Selected destinations:\n{destination_text}\n\n"
                    f"Symbolic links and unsupported entries skipped: {job['skipped']}"
                    f"{warning_text}{cloud_text}\n\n"
                    "Start copying files now?"
                )

                if job["cancel"].is_set():
                    self._manual_backup_finish(
                        job,
                        "CANCELLED BEFORE COPY",
                        "No destination files were copied.",
                    )
                    continue

                if not messagebox.askyesno(
                    "Confirm Real Backup",
                    confirm_text,
                    parent=job["window"],
                ):
                    self._manual_backup_finish(
                        job,
                        "CANCELLED BEFORE COPY",
                        "You cancelled at the final confirmation. No destination files were copied.",
                    )
                    continue

                job["phase"] = "copy"
                job["started"] = time.monotonic()
                job["status_label"].configure(
                    text="BACKUP RUNNING — COPYING FILES TO SELECTED DESTINATIONS..."
                )
                job["detail_label"].configure(
                    text=(
                        f"{len(job['files'])} source files · "
                        f"{self._manual_backup_format_size(job['total_bytes'])} per destination · "
                        f"{job['total_ops']} destination-file operations"
                    )
                )

                worker = threading.Thread(
                    target=self._manual_backup_copy_worker,
                    args=(job,),
                    daemon=True,
                    name="SmartBackupManualCopy",
                )
                worker.start()

            elif event_type == "copy_started":
                paths = "\n".join(
                    f"{name}: {path}" for name, path in event["archive_paths"]
                )
                job["detail_label"].configure(
                    text="Archive folders created. Copying files...\n" + paths
                )

            elif event_type == "progress":
                total_ops = event["total_ops"]
                units = event["units"]
                percent = min(100.0, (units / total_ops) * 100) if total_ops else 0.0

                job["progress_bar"].configure(value=percent)
                job["progress_percent"].configure(text=f"{percent:.1f}%")
                job["summary_labels"]["progress"].configure(text=f"{percent:.1f}%")
                job["summary_labels"]["files"].configure(
                    text=f"{event['processed']} / {total_ops}"
                )

                elapsed = max(0.1, time.monotonic() - event["started"])
                remaining_units = max(0.0, total_ops - units)
                eta = (
                    f"{elapsed * remaining_units / units:.0f}s"
                    if units > 0 and remaining_units > 0 else "—"
                )
                job["summary_labels"]["eta"].configure(text=eta)

                active_bytes = event["written"] + event["current_written"]
                speed = active_bytes / elapsed / (1024 * 1024)

                job["detail_label"].configure(
                    text=(
                        f"{percent:.1f}% · "
                        f"{self._manual_backup_format_size(active_bytes)} written · "
                        f"{speed:.2f} MB/s · "
                        f"Current: {event['current_file']} → {event['current_destination']}"
                    )
                )

            elif event_type == "file":
                tree = job["tree"]
                tree.insert(
                    "",
                    "end",
                    values=(
                        event["file"],
                        event["destination"],
                        event["status"],
                    ),
                )

                children = tree.get_children()
                if len(children) > 300:
                    tree.delete(children[0])

                total_ops = event["total_ops"]
                job["summary_labels"]["files"].configure(
                    text=f"{event['processed']} / {total_ops}"
                )

            elif event_type == "finished":
                success = event.get("success", 0)
                failed = event.get("failed", 0)
                cancelled = event.get("cancelled", False)
                errors = event.get("errors", [])
                archives = event.get("archive_paths", [])

                if cancelled:
                    final_status = "CANCELLED — PARTIAL BACKUP"
                elif event.get("fatal"):
                    final_status = "BACKUP STOPPED — SETUP ERROR"
                elif failed:
                    final_status = "BACKUP FINISHED WITH ERRORS"
                else:
                    final_status = "BACKUP COMPLETED SUCCESSFULLY"

                if not cancelled and not event.get("fatal"):
                    job["progress_bar"].configure(value=100)
                    job["progress_percent"].configure(text="100%")
                    job["summary_labels"]["progress"].configure(text="100%")

                archive_text = "\n".join(
                    f"{name}: {path}" for name, path in archives
                )
                error_text = "\n".join(errors[:10])

                detail = (
                    f"Files successfully copied: {success}\n"
                    f"Failed file operations: {failed}\n"
                    f"Files scanned: {len(job['files'])}\n"
                    f"Symbolic links/unsupported entries skipped: {job['skipped']}\n\n"
                    f"Archive locations:\n{archive_text}"
                )

                if error_text:
                    detail += "\n\nErrors:\n" + error_text

                if cancelled:
                    detail += (
                        "\n\nPreviously completed files remain in the new archive folders. "
                        "The backup is incomplete."
                    )

                job["summary_labels"]["files"].configure(
                    text=f"{success} copied / {failed} failed"
                )
                job["summary_labels"]["eta"].configure(text="Finished")
                self._manual_backup_finish(job, final_status, detail)

        if not job["finished"]:
            try:
                job["window"].after(
                    100,
                    lambda: self._manual_backup_poll(job),
                )
            except tk.TclError:
                job["cancel"].set()


    def _open_progress_preview(self, parent: tk.Toplevel, sources, destinations) -> None:
        """Run the real Daily Smart Backup using the existing transfer-monitor design."""
        if getattr(self, "_daily_backup_active", False):
            messagebox.showinfo("Backup Running", "A Daily Smart Backup is already running.")
            return

        window = tk.Toplevel(self.root)
        window.title("SmartBackup | Daily Backup Progress")
        window.geometry("1080x780")
        window.minsize(900, 650)
        window.configure(bg=COLORS["bg"])
        window.transient(self.root)

        body = tk.Frame(window, bg=COLORS["bg"], padx=24, pady=20)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="LIVE TRANSFER MONITOR", bg=COLORS["bg"], fg=COLORS["accent"], font=(FONT, 11, "bold")).pack(anchor="w")
        tk.Label(body, text="Daily Smart Backup", bg=COLORS["bg"], fg=COLORS["text"], font=(FONT, 24, "bold")).pack(anchor="w", pady=(4, 12))

        summary = tk.Frame(body, bg=COLORS["bg"])
        summary.pack(fill="x", pady=(0, 12))
        summary_labels = {}
        for key, heading, color in (
            ("progress", "OVERALL PROGRESS", COLORS["accent"]),
            ("files", "FILES PROCESSED", COLORS["blue"]),
            ("eta", "ESTIMATED REMAINING", COLORS["amber"]),
        ):
            card = self._card(summary, 13)
            card.pack(side="left", fill="both", expand=True, padx=(0, 8))
            tk.Label(card, text=heading, bg=COLORS["panel"], fg=COLORS["muted"], font=(FONT, 10, "bold")).pack(anchor="w")
            value = tk.Label(card, text="0%", bg=COLORS["panel"], fg=color, font=(FONT, 22, "bold"))
            value.pack(anchor="w", pady=(5, 0))
            summary_labels[key] = value

        progress_card = self._card(body, 14)
        progress_card.pack(fill="x", pady=(0, 12))
        progress_header = tk.Frame(progress_card, bg=COLORS["panel"])
        progress_header.pack(fill="x")
        tk.Label(progress_header, text="TRANSFER PROGRESS", bg=COLORS["panel"], fg=COLORS["text"], font=(FONT, 14, "bold")).pack(side="left")
        progress_percent = tk.Label(progress_header, text="0%", bg=COLORS["panel"], fg=COLORS["accent"], font=(FONT, 13, "bold"))
        progress_percent.pack(side="right")
        progress_bar = ttk.Progressbar(progress_card, maximum=100, value=0, style="Smart.Horizontal.TProgressbar")
        progress_bar.pack(fill="x", pady=(12, 8))
        detail_label = tk.Label(progress_card, text="Scanning Documents and Downloads. No destination files have been copied yet.", bg=COLORS["panel"], fg=COLORS["muted"], font=(FONT, 10), justify="left", wraplength=950)
        detail_label.pack(anchor="w")

        preview_area = tk.Frame(body, bg=COLORS["bg"])
        preview_area.pack(fill="both", expand=True, pady=(0, 10))
        table_card = self._card(preview_area, 12)
        table_card.pack(side="left", fill="both", expand=True, padx=(0, 10))
        preview_summary = self._card(preview_area, 12)
        preview_summary.pack(side="right", fill="y")
        preview_summary.configure(width=270)
        preview_summary.pack_propagate(False)
        tk.Label(preview_summary, text="BACKUP SUMMARY", bg=COLORS["panel"], fg=COLORS["text"], font=(FONT, 13, "bold")).pack(anchor="w", pady=(0, 12))
        preview_summary_labels = {}
        for key, heading in (("files", "FILES FOUND"), ("size", "TOTAL SOURCE SIZE"), ("sources", "SOURCE FOLDERS"), ("destinations", "DESTINATIONS"), ("skipped", "SKIPPED ENTRIES")):
            tk.Label(preview_summary, text=heading, bg=COLORS["panel"], fg=COLORS["muted"], font=(FONT, 9, "bold")).pack(anchor="w", pady=(7, 0))
            value = tk.Label(preview_summary, text="—", bg=COLORS["panel"], fg=COLORS["text"], font=(FONT, 10), justify="left", anchor="w", wraplength=235)
            value.pack(anchor="w", fill="x")
            preview_summary_labels[key] = value
        tk.Label(table_card, text="BACKUP FILE REVIEW / FILE-BY-FILE ACTIVITY", bg=COLORS["panel"], fg=COLORS["text"], font=(FONT, 14, "bold")).pack(anchor="w", pady=(0, 8))
        tree_frame = tk.Frame(table_card, bg=COLORS["panel"])
        tree_frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(tree_frame, columns=("file", "destination", "status"), show="headings", style="Smart.Treeview", height=12)
        tree.heading("file", text="FILE")
        tree.heading("destination", text="DESTINATION")
        tree.heading("status", text="STATUS")
        tree.column("file", width=420, anchor="w")
        tree.column("destination", width=230, anchor="w")
        tree.column("status", width=150, anchor="center")
        tree_scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=tree_scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        tree_scrollbar.pack(side="right", fill="y")

        status_label = tk.Label(body, text="SCANNING SOURCE FOLDERS...", bg=COLORS["bg"], fg=COLORS["amber"], font=(FONT, 11, "bold"), anchor="w", justify="left", wraplength=950)
        status_label.pack(fill="x", pady=(0, 9))
        controls = tk.Frame(body, bg=COLORS["bg"])
        controls.pack(fill="x")

        normalized_sources = []
        for label, raw_path in sources:
            try:
                source_path = Path(raw_path).expanduser().resolve(strict=True)
            except Exception as exc:
                messagebox.showerror("Source Error", f"{label}: {exc}", parent=window)
                window.destroy()
                return
            if not source_path.is_dir():
                messagebox.showerror("Source Error", f"{label} is not an accessible folder.", parent=window)
                window.destroy()
                return
            normalized_sources.append((str(label), source_path))

        job = {
            "sources": normalized_sources,
            "destinations": destinations,
            "window": window,
            "queue": queue.Queue(),
            "cancel": threading.Event(),
            "finished": False,
            "summary_labels": summary_labels,
            "progress_percent": progress_percent,
            "progress_bar": progress_bar,
            "detail_label": detail_label,
            "status_label": status_label,
            "tree": tree,
            "preview_summary_labels": preview_summary_labels,
            "files": [],
            "dirs": [],
            "skipped": 0,
            "total_bytes": 0,
            "total_ops": 0,
            "validated_destinations": [],
            "started": None,
            "phase": "scan",
            "row_ids": {},
        }

        def request_cancel():
            if job["finished"]:
                window.destroy()
                return
            job["cancel"].set()
            if job["phase"] == "preview":
                finish("CANCELLED BEFORE COPY", "You cancelled after reviewing the file list. No destination files were copied.")
                return
            try:
                status_label.configure(text="CANCELLATION REQUESTED. Waiting for the current operation to stop...")
            except tk.TclError:
                pass

        def start_copy():
            if job["finished"] or job["phase"] != "preview":
                return
            job["phase"] = "copy"
            job["started"] = time.monotonic()
            status_label.configure(text="BACKUP RUNNING — COPYING FILES...")
            detail_label.configure(text=f"{len(job['files'])} files · {self._manual_backup_format_size(job['total_bytes'])} per destination · {job['total_ops']} destination-file operations")
            confirm_button.configure(state="disabled", text="BACKUP STARTED")
            threading.Thread(target=copy_worker, daemon=True, name="SmartBackupDailyCopy").start()

        confirm_button = self._action_button(controls, "CONFIRM BACKUP", start_copy, False)
        confirm_button.pack(side="right", padx=(0, 8))
        confirm_button.configure(state="disabled")
        cancel_button = self._action_button(controls, "CANCEL", request_cancel, False)
        cancel_button.pack(side="right")
        window.protocol("WM_DELETE_WINDOW", request_cancel)
        window.grab_set()

        try:
            parent.grab_release()
            parent.destroy()
        except tk.TclError:
            pass

        def finish(status, detail):
            job["finished"] = True
            self._daily_backup_active = False
            try:
                status_label.configure(text=status)
                detail_label.configure(text=detail + "\n\nBackup process finished. Click CLOSE to close this window.")
                cancel_button.destroy()
                confirm_button.configure(text="CLOSE", state="normal", command=window.destroy)
                confirm_button.tkraise()
                window.protocol("WM_DELETE_WINDOW", window.destroy)
            except tk.TclError:
                pass

        def scan_worker():
            files, dirs, skipped, scan_errors = [], [], 0, []
            today = time.strftime("%Y-%m-%d", time.localtime())
            try:
                for label, source_path in normalized_sources:
                    for root, dirnames, filenames in os.walk(source_path, topdown=True, followlinks=False, onerror=lambda exc: scan_errors.append(str(exc))):
                        if job["cancel"].is_set():
                            job["queue"].put({"type": "scan_cancelled"})
                            return
                        current = Path(root)
                        retained = []
                        for dirname in dirnames:
                            folder = current / dirname
                            if folder.is_symlink():
                                skipped += 1
                            else:
                                retained.append(dirname)
                        dirnames[:] = retained
                        for filename in filenames:
                            if job["cancel"].is_set():
                                job["queue"].put({"type": "scan_cancelled"})
                                return
                            path = current / filename
                            try:
                                if path.is_symlink() or not path.is_file():
                                    skipped += 1
                                    continue

                                info = path.stat()
                                modified_today = (
                                    time.strftime("%Y-%m-%d", time.localtime(info.st_mtime)) == today
                                )

                                birth_time = getattr(info, "st_birthtime", None)
                                if birth_time is None:
                                    birth_time_ns = getattr(info, "st_birthtime_ns", None)
                                    if birth_time_ns is not None:
                                        birth_time = birth_time_ns / 1_000_000_000

                                created_today = (
                                    birth_time is not None
                                    and time.strftime("%Y-%m-%d", time.localtime(birth_time)) == today
                                )

                                if not (created_today or modified_today):
                                    continue

                                files.append((
                                    label,
                                    source_path,
                                    path.relative_to(source_path).as_posix(),
                                    info.st_size
                                ))
                            except Exception as exc:
                                scan_errors.append(f"{path}: {exc}")
                if scan_errors:
                    job["queue"].put({"type": "scan_error", "errors": scan_errors[:20], "error_count": len(scan_errors)})
                else:
                    job["queue"].put({"type": "scan_done", "files": files, "dirs": dirs, "skipped": skipped})
            except Exception as exc:
                job["queue"].put({"type": "scan_error", "errors": [str(exc)], "error_count": 1})

        def copy_worker():
            errors, archives = [], []
            success = failed = processed = committed = 0
            started = time.monotonic()
            try:
                for name, target, candidate, device in job["validated_destinations"]:
                    if job["cancel"].is_set():
                        break
                    target.mkdir(parents=True, exist_ok=False)
                    archives.append((name, target))
                for name, target in archives:
                    for label, relative in job["dirs"]:
                        (target / label / Path(relative)).mkdir(parents=True, exist_ok=True)
                job["queue"].put({"type": "copy_started", "archives": archives})
                last_update = 0.0
                for name, target in archives:
                    for label, source_path, relative, scanned_size in job["files"]:
                        if job["cancel"].is_set():
                            break
                        src = source_path / Path(relative)
                        destination = target / label / Path(relative)
                        temporary = destination.with_name(f".{destination.name}.smartbackup-partial")
                        current_written = 0
                        try:
                            if src.is_symlink() or not src.is_file():
                                raise OSError("Source file is no longer available as a regular file.")
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            if destination.exists():
                                raise FileExistsError("Refusing to overwrite an existing file.")
                            with src.open("rb") as input_file, temporary.open("xb") as output_file:
                                while True:
                                    if job["cancel"].is_set():
                                        raise InterruptedError("Backup cancelled.")
                                    chunk = input_file.read(1024 * 1024)
                                    if not chunk:
                                        break
                                    output_file.write(chunk)
                                    current_written += len(chunk)
                                    now = time.monotonic()
                                    if now - last_update >= 0.15:
                                        fraction = min(current_written / scanned_size, 1.0) if scanned_size else 0.0
                                        job["queue"].put({"type": "progress", "processed": processed, "success": success, "failed": failed, "units": processed + fraction, "written": committed, "current_written": current_written, "total_ops": job["total_ops"], "current_file": f"{label}/{relative}", "current_destination": name, "started": started})
                                        last_update = now
                            if job["cancel"].is_set():
                                raise InterruptedError("Backup cancelled.")
                            shutil.copystat(src, temporary, follow_symlinks=False)
                            if destination.exists():
                                raise FileExistsError("Refusing to overwrite an existing file.")
                            temporary.replace(destination)
                            committed += current_written
                            success += 1
                            processed += 1
                            job["queue"].put({"type": "file", "file": f"{label}/{relative}", "destination": name, "status": "SUCCESSFUL TRANSFER", "processed": processed, "success": success, "failed": failed, "total_ops": job["total_ops"]})
                        except InterruptedError:
                            temporary.unlink(missing_ok=True)
                            job["cancel"].set()
                            break
                        except Exception as exc:
                            temporary.unlink(missing_ok=True)
                            failed += 1
                            processed += 1
                            errors.append(f"{name} | {label}/{relative} | {exc}")
                            job["queue"].put({"type": "file", "file": f"{label}/{relative}", "destination": name, "status": "FAILED", "processed": processed, "success": success, "failed": failed, "total_ops": job["total_ops"]})
                    if job["cancel"].is_set():
                        break
                job["queue"].put({"type": "finished", "success": success, "failed": failed, "processed": processed, "written": committed, "errors": errors[:30], "cancelled": job["cancel"].is_set(), "archives": archives})
            except Exception as exc:
                job["queue"].put({"type": "finished", "success": success, "failed": failed, "processed": processed, "written": committed, "errors": [f"Backup setup failed: {exc}"] + errors[:20], "cancelled": job["cancel"].is_set(), "archives": archives, "fatal": True})

        def poll():
            events = job["queue"]
            while True:
                try:
                    event = events.get_nowait()
                except queue.Empty:
                    break
                kind = event.get("type")
                if kind == "scan_cancelled":
                    finish("CANCELLED BEFORE COPY", "The source scan was cancelled. No destination files were copied.")
                elif kind == "scan_error":
                    finish("SOURCE SCAN FAILED — NO COPY STARTED", f"Scan errors: {event.get('error_count', 0)}\n" + "\n".join(event.get("errors", [])))
                elif kind == "scan_done":
                    job["files"], job["dirs"], job["skipped"] = event["files"], event["dirs"], event["skipped"]
                    job["total_bytes"] = sum(item[3] for item in job["files"])
                    job["total_ops"] = len(job["files"]) * len(destinations)
                    valid, errors, warnings = [], [], []
                    seen = set()
                    device_groups = {}
                    mounts = globals().get("WINDOWS_MOUNTS", {})
                    cloud_names = {"OneDrive", "iCloud Drive", "Google Drive", "Dropbox"}
                    source_roots = [path.resolve() for label, path in normalized_sources]
                    for name, raw_target in destinations:
                        if raw_target is None:
                            errors.append(f"{name}: destination is not configured.")
                            continue
                        try:
                            target = Path(raw_target).expanduser().resolve(strict=False)
                            if target in seen:
                                errors.append(f"{name}: duplicate destination folder selected.")
                                continue
                            seen.add(target)
                            if target.exists():
                                errors.append(f"{name}: today's backup folder already exists. Choose another date or move the existing folder; no files will be overwritten.")
                                continue
                            unsafe = False
                            for source_root in source_roots:
                                if target == source_root or target.is_relative_to(source_root) or source_root.is_relative_to(target):
                                    errors.append(f"{name}: destination overlaps a source folder ({source_root}). Select a separate drive or folder.")
                                    unsafe = True
                                    break
                            if unsafe:
                                continue
                            if name in mounts:
                                mount = Path(mounts[name]).expanduser()
                                if not mount.is_mount():
                                    errors.append(f"{name}: drive is not mounted.")
                                    continue
                                if not target.is_relative_to(mount.resolve()):
                                    errors.append(f"{name}: selected folder is outside its mounted drive.")
                                    continue
                            if name in cloud_names:
                                sync_root = self._connection_path(name)
                                if not sync_root or not Path(sync_root).expanduser().is_dir():
                                    errors.append(f"{name}: local cloud sync folder is not detected.")
                                    continue
                                if not target.is_relative_to(Path(sync_root).expanduser().resolve()):
                                    warnings.append(f"{name}: only a local folder copy can be confirmed; cloud upload is not verified.")
                            candidate = target
                            while not candidate.exists() and candidate != candidate.parent:
                                candidate = candidate.parent
                            if not candidate.is_dir():
                                errors.append(f"{name}: no existing parent folder is available.")
                                continue
                            free_bytes = shutil.disk_usage(candidate).free
                            fd, test_path = tempfile.mkstemp(prefix=".smartbackup_preflight_", dir=str(candidate))
                            os.close(fd)
                            os.remove(test_path)
                            device = os.stat(candidate).st_dev
                            valid.append((name, target, candidate, device))
                            device_groups.setdefault(device, []).append((name, free_bytes))
                        except Exception as exc:
                            errors.append(f"{name}: {exc}")
                    for device, entries in device_groups.items():
                        available = min(item[1] for item in entries)
                        required = job["total_bytes"] * len(entries)
                        if available < required:
                            names = ", ".join(item[0] for item in entries)
                            errors.append(f"{names}: insufficient free space. Available {self._manual_backup_format_size(available)}, required approximately {self._manual_backup_format_size(required)}.")
                    if errors or not valid:
                        finish("DESTINATION PREFLIGHT FAILED — NO COPY STARTED", "\n".join(errors or ["No valid destination was selected."]))
                        continue
                    job["validated_destinations"] = valid
                    destination_text = "\n".join(f"{name}: {target}" for name, target, candidate, device in valid)
                    warning_text = "\n".join(warnings)
                    source_text = "\n".join(f"{label}: {path}" for label, path in normalized_sources)
                    preview_summary_labels["files"].configure(text=str(len(job["files"])))
                    preview_summary_labels["size"].configure(text=self._manual_backup_format_size(job["total_bytes"]))
                    preview_summary_labels["sources"].configure(text=source_text)
                    preview_summary_labels["destinations"].configure(text=destination_text)
                    preview_summary_labels["skipped"].configure(text=str(job["skipped"]))
                    row_number = 0
                    for destination_name, target, candidate, device in valid:
                        for label, source_path, relative, scanned_size in job["files"]:
                            row_id = f"preview_{row_number}"
                            row_number += 1
                            display_file = f"{label}/{relative}"
                            tree.insert("", "end", iid=row_id, values=(display_file, destination_name, "PENDING"))
                            job["row_ids"][(destination_name, display_file)] = row_id
                    job["phase"] = "preview"
                    status_label.configure(text="BACKUP PREVIEW READY — REVIEW THE COMPLETE FILE LIST BEFORE CONFIRMING.")
                    detail_label.configure(text=f"{len(job['files'])} files found · {self._manual_backup_format_size(job['total_bytes'])} total source size. No files have been copied yet. Existing files will not be overwritten.")
                    summary_labels["files"].configure(text=f"0 / {job['total_ops']}")
                    if warning_text:
                        detail_label.configure(text=detail_label.cget("text") + "\nWarnings: " + warning_text)
                    confirm_button.configure(state="normal", text="CONFIRM BACKUP")
                    cancel_button.configure(text="CANCEL")
                elif kind == "copy_started":
                    detail_label.configure(text="Daily backup folders created. Copying files...\n" + "\n".join(f"{name}: {path}" for name, path in event["archives"]))
                elif kind == "progress":
                    active_row = job["row_ids"].get((event["current_destination"], event["current_file"]))
                    if active_row:
                        tree.item(active_row, values=(event["current_file"], event["current_destination"], "COPYING..."))
                    total = event["total_ops"]
                    units = event["units"]
                    percent = min(100.0, (units / total) * 100) if total else 100.0
                    progress_bar.configure(value=percent)
                    progress_percent.configure(text=f"{percent:.1f}%")
                    summary_labels["progress"].configure(text=f"{percent:.1f}%")
                    summary_labels["files"].configure(text=f"{event['processed']} / {total}")
                    elapsed = max(0.1, time.monotonic() - event["started"])
                    remaining = max(0.0, total - units)
                    summary_labels["eta"].configure(text=f"{elapsed * remaining / units:.0f}s" if units > 0 and remaining > 0 else "—")
                    active_bytes = event["written"] + event["current_written"]
                    speed = active_bytes / elapsed / (1024 * 1024)
                    detail_label.configure(text=f"{percent:.1f}% · {self._manual_backup_format_size(active_bytes)} written · {speed:.2f} MB/s · Current: {event['current_file']} → {event['current_destination']}")
                elif kind == "file":
                    row_id = job["row_ids"].get((event["destination"], event["file"]))
                    if row_id:
                        tree.item(row_id, values=(event["file"], event["destination"], event["status"]))
                    summary_labels["files"].configure(text=f"{event['processed']} / {event['total_ops']}")
                elif kind == "finished":
                    success, failed = event.get("success", 0), event.get("failed", 0)
                    cancelled = event.get("cancelled", False)
                    if cancelled:
                        final = "CANCELLED — PARTIAL BACKUP"
                    elif event.get("fatal"):
                        final = "BACKUP STOPPED — SETUP ERROR"
                    elif failed:
                        final = "BACKUP FINISHED WITH ERRORS"
                    else:
                        final = "BACKUP COMPLETED SUCCESSFULLY"
                    if not cancelled and not event.get("fatal"):
                        progress_bar.configure(value=100)
                        progress_percent.configure(text="100%")
                        summary_labels["progress"].configure(text="100%")
                    archive_text = "\n".join(f"{name}: {path}" for name, path in event.get("archives", []))
                    error_text = "\n".join(event.get("errors", [])[:10])
                    detail = (f"Files successfully copied: {success}\nFailed file operations: {failed}\nFiles scanned: {len(job['files'])}\nSymbolic links/unsupported entries skipped: {job['skipped']}\n\nBackup locations:\n{archive_text}")
                    if error_text:
                        detail += "\n\nErrors:\n" + error_text
                    if cancelled:
                        detail += "\n\nPreviously completed files remain in the backup folders. This backup is incomplete."
                    summary_labels["files"].configure(text=f"{success} copied / {failed} failed")
                    summary_labels["eta"].configure(text="Finished")
                    finish(final, detail)
            if not job["finished"]:
                try:
                    window.after(100, poll)
                except tk.TclError:
                    job["cancel"].set()

        self._daily_backup_active = True
        threading.Thread(target=scan_worker, daemon=True, name="SmartBackupDailyScanner").start()
        window.after(100, poll)

    def _build_activity_page(self) -> None:
        assert self.content is not None
        summary = self._card(self.content, 20)
        summary.pack(fill="x", pady=(0, 14))
        tk.Label(summary, text="BACKUP HISTORY", bg=COLORS["panel"], fg=COLORS["text"], font=(FONT, 13, "bold")).pack(anchor="w")
        tk.Label(
            summary,
            text="The completed application will record each backup run, selected destinations, file-level outcomes, failed items, retry attempts, and checksum verification results.",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=(FONT, 12),
            wraplength=900,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

        table = self._card(self.content, 14)
        table.pack(fill="both", expand=True)
        columns = ("date", "mode", "destination", "result")
        tree = ttk.Treeview(table, columns=columns, show="headings", style="Smart.Treeview", height=8)
        headings = {
            "date": ("DATE / TIME", 170),
            "mode": ("BACKUP MODE", 190),
            "destination": ("DESTINATION", 270),
            "result": ("RESULT", 170),
        }
        for key, (label, width) in headings.items():
            tree.heading(key, text=label)
            tree.column(key, width=width, anchor="w")
        tree.pack(fill="both", expand=True)
        tree.insert("", "end", values=("—", "No recorded runs", "—", "Awaiting engine integration"))
        tk.Label(table, text="No real backup history is connected in this UI prototype.", bg=COLORS["panel"], fg=COLORS["subtle"], font=(FONT, 11)).pack(anchor="w", pady=(10, 0))

    def _connection_path(self, name: str):
        paths = {
            "OneDrive": ONEDRIVE_ROOT,
            "iCloud Drive": ICLOUD_ROOT,
            "Google Drive": GOOGLE_DRIVE_ROOT,
            "Dropbox": DROPBOX_ROOT,
            "Windows C: Drive": WINDOWS_MOUNTS.get("Windows C: Drive"),
            "Windows D: ADATA": WINDOWS_MOUNTS.get("Windows D: ADATA"),
            "Windows F: Transcend": WINDOWS_MOUNTS.get("Windows F: Transcend"),
            "Local Mac": HOME / "Documents",
        }
        return paths.get(name)

    def _start_connection_monitor(self) -> None:
        if self._connection_worker_started:
            return

        self._connection_worker_started = True

        worker = threading.Thread(
            target=self._connection_worker,
            daemon=True,
            name="SmartBackupConnectionMonitor",
        )
        worker.start()

        self.root.after(250, self._poll_connection_results)
        self.root.after(500, self._animate_connection_dots)

    def _connection_worker(self) -> None:
        while True:
            for name in list(self.connection_states):
                path = self._connection_path(name)

                try:
                    if path is None:
                        raise FileNotFoundError("Destination folder not detected")

                    path = Path(path).expanduser()

                    if name in WINDOWS_MOUNTS and not path.is_mount():
                        raise FileNotFoundError("Windows volume is not mounted")

                    if not path.is_dir():
                        raise FileNotFoundError("Destination folder is unavailable")

                    with os.scandir(path) as entries:
                        next(entries, None)

                    if name in {
                        "OneDrive", "iCloud Drive",
                        "Google Drive", "Dropbox"
                    }:
                        detail = (
                            "Connected - local sync folder accessible; "
                            "cloud sync not verified"
                        )
                    elif name in WINDOWS_MOUNTS:
                        detail = "Connected - volume mounted and folder readable"
                    else:
                        detail = "Connected - folder accessible"

                    self.connection_queue.put(
                        (name, "connected", detail)
                    )

                except Exception as exc:
                    self.connection_queue.put(
                        (
                            name,
                            "disconnected",
                            f"Disconnected - {type(exc).__name__}: {exc}",
                        )
                    )

            time.sleep(15)

    def _poll_connection_results(self) -> None:
        try:
            while True:
                name, state, detail = self.connection_queue.get_nowait()

                self.connection_states[name] = (state, detail)

                label = self.connection_details.get(name)
                if label is not None and label.winfo_exists():
                    label.config(text=detail)

        except queue.Empty:
            pass

        self.root.after(250, self._poll_connection_results)

    def _animate_connection_dots(self) -> None:
        self._connection_blink_on = not self._connection_blink_on

        colors = {
            "connected": COLORS["accent"],
            "checking": COLORS["amber"],
            "disconnected": COLORS["red"],
        }

        for name, dot in list(self.connection_dots.items()):
            if not dot.winfo_exists():
                continue

            state, _ = self.connection_states.get(
                name, ("checking", "Checking connection...")
            )

            color = colors.get(state, COLORS["amber"])

            if state != "connected" and not self._connection_blink_on:
                color = COLORS["panel"]

            dot.config(fg=color)

        self.root.after(500, self._animate_connection_dots)


def main() -> None:
    root = tk.Tk()
    SmartBackupDashboard(root)
    root.mainloop()


if __name__ == "__main__":
    main()
