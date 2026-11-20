"""Daily Activity Tracker — simple, local-first macOS foreground-app logger."""
import csv
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from pathlib import Path
from datetime import datetime, date, timedelta
from collections import defaultdict
import json
import time
import imaplib
import email
import ssl
import threading
import queue
import os
from zoneinfo import ZoneInfo
import urllib.request
import socket
import ftplib
from email.utils import parseaddr, parsedate_to_datetime

APP_NAME = "Daily Activity Tracker"
ROOT = Path.home() / "DailyActivityTracker"
DATA_DIR = ROOT / "data"
CONFIG_FILE = ROOT / "categories.json"
MAIL_CONFIG_FILE = ROOT / "mail_tracker.json"
MAIL_ACCOUNTS_FILE = ROOT / "mail_accounts.json"
APP_VERSION = "2.0.0"
DEVELOPER_NAME = "Saiful Quadir"
KEYCHAIN_SERVICE = "DailyActivityTracker.MailAccount"
POLL_SECONDS = 15
FIELDS = ["date", "start_time", "end_time", "duration_seconds", "app", "category", "device", "activity"]

DEFAULT_CATEGORIES = {
    "Google Chrome": "Learning", "Safari": "Learning", "ChatGPT": "Learning",
    "Microsoft Excel": "Work", "Numbers": "Work", "Visual Studio Code": "Work",
    "Terminal": "Work", "Python": "Work", "Mail": "Communication",
    "Microsoft Outlook": "Communication", "Messages": "Communication",
    "WhatsApp": "Communication", "Zoom": "Communication", "Microsoft Teams": "Communication",
    "YouTube": "Entertainment", "Music": "Entertainment", "Spotify": "Entertainment",
    "Finder": "System", "System Settings": "System"
}
CATEGORIES = ["Work", "Learning", "Communication", "Entertainment", "Personal", "System", "Other"]
MAIL_CATEGORIES_DEFAULT = [
    "LinkedIn", "Indeed", "Glassdoor", "Banks", "Friends", "College",
    "Relatives", "Government / Official", "Work / Office", "Bills / Utilities",
    "Shopping / E-commerce", "Travel", "Subscriptions", "Social Media",
    "Promotional", "Others",
]
MAIL_PROVIDERS = {
    "Yahoo": ("imap.mail.yahoo.com", 993),
    "Gmail": ("imap.gmail.com", 993),
}
MAIL_RANGES = ["Today", "Yesterday", "Last 7 days", "Last 15 days", "Last 1 month", "Last 3 months"]


def load_mail_config():
    ROOT.mkdir(parents=True, exist_ok=True)
    default = {"categories": MAIL_CATEGORIES_DEFAULT, "sender_rules": {}}
    try:
        data = json.loads(MAIL_CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return default
        categories = data.get("categories")
        rules = data.get("sender_rules")
        return {
            "categories": categories if isinstance(categories, list) and categories else MAIL_CATEGORIES_DEFAULT.copy(),
            "sender_rules": rules if isinstance(rules, dict) else {},
        }
    except (OSError, json.JSONDecodeError):
        return default


def save_mail_config(config):
    ROOT.mkdir(parents=True, exist_ok=True)
    MAIL_CONFIG_FILE.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")


def load_mail_accounts():
    """Load account labels only; passwords are kept in macOS Keychain."""
    ROOT.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(MAIL_ACCOUNTS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict) and x.get("email") and x.get("provider") in MAIL_PROVIDERS]
    except (OSError, json.JSONDecodeError):
        pass
    return []


def save_mail_accounts(accounts):
    ROOT.mkdir(parents=True, exist_ok=True)
    temp = MAIL_ACCOUNTS_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(accounts, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(MAIL_ACCOUNTS_FILE)


def keychain_get_password(email_address):
    result = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", email_address, "-w"], capture_output=True, text=True, timeout=8)
    return result.stdout.rstrip("\r\n") if result.returncode == 0 else ""


def keychain_set_password(email_address, password):
    # macOS Keychain is the only persistent password store used by this app.
    result = subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", email_address, "-w", password], capture_output=True, text=True, timeout=8)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "Could not save the password to macOS Keychain.").strip())


def keychain_delete_password(email_address):
    subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", email_address], capture_output=True, text=True, timeout=8)


def mail_date_window(label):
    today = date.today()
    if label == "Today":
        return today, today + timedelta(days=1)
    if label == "Yesterday":
        return today - timedelta(days=1), today
    if label == "Last 7 days":
        return today - timedelta(days=6), today + timedelta(days=1)
    if label == "Last 15 days":
        return today - timedelta(days=14), today + timedelta(days=1)
    if label == "Last 1 month":
        start = today - timedelta(days=30)
        return start, today + timedelta(days=1)
    start = today - timedelta(days=89)
    return start, today + timedelta(days=1)


def classify_mail_sender(sender, subject, rules, categories):
    address = parseaddr(sender)[1].lower()
    domain = address.rsplit("@", 1)[-1] if "@" in address else ""
    # Explicit user rule wins; rules can be keyed by full email or sender domain.
    for key in (address, domain):
        if key in rules and rules[key] in categories:
            return rules[key]
    text = (sender + " " + subject + " " + domain).lower()
    tests = [
        (["linkedin"], "LinkedIn"), (["indeed"], "Indeed"), (["glassdoor"], "Glassdoor"),
        (["bank", "banking", "visa", "mastercard", "paypal", "wise.com", "wealthsimple"], "Banks"),
        (["college", "university", ".edu", "school"], "College"),
        (["government", ".gov", "canada.ca", "service canada", "cra.gc.ca"], "Government / Official"),
        (["hydro", "utility", "electricity", "internet bill", "phone bill"], "Bills / Utilities"),
        (["amazon", "walmart", "ebay", "shopify", "shipping", "order confirmation"], "Shopping / E-commerce"),
        (["airline", "air canada", "booking.com", "expedia", "hotel reservation", "trip.com"], "Travel"),
        (["newsletter", "unsubscribe", "subscription", "digest"], "Subscriptions"),
        (["facebook", "instagram", "twitter", "x.com", "tiktok"], "Social Media"),
        (["sale", "promo", "promotion", "coupon", "offer", "marketing"], "Promotional"),
    ]
    for needles, category in tests:
        if category in categories and any(needle in text for needle in needles):
            return category
    return "Others" if "Others" in categories else categories[-1]


def load_categories():
    ROOT.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CATEGORIES, indent=2), encoding="utf-8")
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return DEFAULT_CATEGORIES.copy()


def csv_path(day=None):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"activity_{(day or date.today()).isoformat()}.csv"


def current_app():
    # macOS only. System Events may request Automation permission on first use.
    script = 'tell application "System Events" to get name of first application process whose frontmost is true'
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
    if result.returncode:
        raise RuntimeError((result.stderr or "macOS could not identify the active app").strip())
    return result.stdout.strip() or "Unknown"


def append_row(row):
    path = csv_path(date.fromisoformat(row["date"]))
    new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def read_day(day):
    path = csv_path(day)
    if not path.exists():
        return []
    try:
        with path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


class Tracker:
    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_NAME}  v{APP_VERSION}")
        self.root.geometry("920x690")
        self.root.minsize(760, 590)
        self.root.configure(bg="#F4F7FB")
        self.categories = load_categories()
        self.view_day = date.today()
        self.running = False
        self.active_app = None
        self.session_start = None
        self.after_id = None
        self.started_at = None
        self.last_error = ""
        self._closing = False
        self._clock_after = None
        self._logo_after = None
        self._queue_after = None
        self._weather_after = None
        self._weather_busy = False
        self._ftp_busy = False
        self._ftp_after = None
        self._ftp_blink_after = None
        self._ftp_online = False
        self._ftp_blink_on = False
        self.live_queue = queue.Queue()
        self._style()
        self._build()
        self.refresh()
        self._update_live_clock()
        self._animate_logo()
        self._start_weather()
        self._start_ftp_monitor()
        self._poll_live_queue()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        bg = "#F4F7FB"
        navy = "#102A43"
        text = "#172B4D"
        muted = "#64748B"

        style.configure("TFrame", background=bg)
        style.configure(
            "TLabel", background=bg, foreground=text,
            font=("Helvetica", 10)
        )
        style.configure(
            "Sub.TLabel", background=bg, foreground=muted,
            font=("Helvetica", 9)
        )

        style.configure(
            "Primary.TButton",
            font=("Helvetica", 12, "bold"),
            padding=(18, 13), background="#2563EB",
            foreground="#FFFFFF", borderwidth=3,
            relief="raised"
        )
        style.map(
            "Primary.TButton",
            background=[("pressed", "#1E40AF"), ("active", "#1D4ED8")],
            relief=[("pressed", "sunken"), ("!pressed", "raised")],
            foreground=[("active", "#FFFFFF")]
        )

        style.configure(
            "Accent.TButton",
            font=("Helvetica", 11, "bold"),
            padding=(16, 12), background="#0F766E",
            foreground="#FFFFFF", borderwidth=3,
            relief="raised"
        )
        style.map(
            "Accent.TButton",
            background=[("pressed", "#134E4A"), ("active", "#115E59")],
            relief=[("pressed", "sunken"), ("!pressed", "raised")],
            foreground=[("active", "#FFFFFF")]
        )

        style.configure(
            "Danger.TButton",
            font=("Helvetica", 11, "bold"),
            padding=(16, 12), background="#DC2626",
            foreground="#FFFFFF", borderwidth=3,
            relief="raised"
        )
        style.map(
            "Danger.TButton",
            background=[("pressed", "#991B1B"), ("active", "#B91C1C")],
            relief=[("pressed", "sunken"), ("!pressed", "raised")],
            foreground=[("active", "#FFFFFF")]
        )

        style.configure(
            "TButton", font=("Helvetica", 11, "bold"),
            padding=(14, 11), background="#E2E8F0",
            foreground="#172B4D", borderwidth=2,
            relief="raised"
        )
        style.map(
            "TButton",
            background=[("pressed", "#CBD5E1"), ("active", "#D5E0EC")],
            relief=[("pressed", "sunken"), ("!pressed", "raised")]
        )

        style.configure(
            "Treeview",
            rowheight=34,
            font=("Helvetica", 10),
            background="#FFFFFF",
            fieldbackground="#FFFFFF",
            foreground="#26364B",
            borderwidth=0
        )
        style.configure(
            "Treeview.Heading",
            font=("Helvetica", 9, "bold"),
            background="#EDF2F7",
            foreground="#475569",
            padding=(2, 11),
            relief="flat"
        )
        style.map(
            "Treeview",
            background=[("selected", "#DBEAFE")],
            foreground=[("selected", "#172B4D")]
        )
        style.configure(
            "Vertical.TScrollbar",
            background="#E2E8F0",
            troughcolor="#F8FAFC",
            borderwidth=0,
            arrowsize=12
        )

    def _build(self):
        BG = "#F4F7FB"
        WHITE = "#FFFFFF"
        NAVY = "#102A43"
        TEXT = "#172B4D"
        MUTED = "#64748B"
        BORDER = "#E3EAF2"

        self.root.configure(bg=BG)
        self.root.geometry("1280x820")
        self.root.minsize(1050, 700)

        main_area = tk.Frame(self.root, bg=BG)
        main_area.pack(side="top", fill="both", expand=True)

        main_canvas = tk.Canvas(
            main_area, bg=BG, highlightthickness=0
        )
        main_scrollbar = tk.Scrollbar(
            main_area,
            orient="vertical",
            command=main_canvas.yview,
            width=28,
            bg="#CBD5E1",
            activebackground="#94A3B8",
            troughcolor="#F1F5F9",
            relief="flat",
            bd=0,
            highlightthickness=0
        )
        main_canvas.configure(yscrollcommand=main_scrollbar.set)

        main_canvas.pack(side="left", fill="both", expand=True)
        main_scrollbar.pack(side="right", fill="y")

        outer = tk.Frame(main_canvas, bg=BG)
        outer_window = main_canvas.create_window(
            (0, 0), window=outer, anchor="nw"
        )

        outer.bind(
            "<Configure>",
            lambda event: main_canvas.configure(
                scrollregion=main_canvas.bbox("all")
            )
        )
        main_canvas.bind(
            "<Configure>",
            lambda event: main_canvas.itemconfigure(
                outer_window, width=event.width
            )
        )

        def scroll_main_dashboard(event):
            if event.widget.winfo_toplevel() is not self.root:
                return

            delta = event.delta
            if abs(delta) >= 120:
                steps = int(-delta / 120)
            else:
                steps = -int(delta)

            if steps:
                main_canvas.yview_scroll(steps, "units")
            return "break"

        self.root.bind_all(
            "<MouseWheel>", scroll_main_dashboard
        )

        # Main header
        header = tk.Frame(outer, bg=NAVY, padx=23, pady=19)
        header.pack(fill="x", pady=(0, 14))

        self.logo = tk.Canvas(header, width=54, height=54,
                               bg=NAVY, highlightthickness=0)
        self.logo.pack(side="left", padx=(0, 15))
        header_left = tk.Frame(header, bg=NAVY)
        header_left.pack(side="left", fill="x", expand=True)

        tk.Label(
            header_left, text="DAILY ACTIVITY TRACKER",
            bg=NAVY, fg=WHITE,
            font=("Helvetica", 21, "bold")
        ).pack(anchor="w")

        tk.Label(
            header_left,
            text="Your productivity command center  |  Private Mac activity tracking",
            bg=NAVY, fg="#C5D5E5",
            font=("Helvetica", 10)
        ).pack(anchor="w", pady=(5, 0))


        # Date navigation
        daybar = tk.Frame(
            outer, bg=WHITE, padx=13, pady=10,
            highlightbackground=BORDER, highlightthickness=1
        )
        daybar.pack(fill="x", pady=(0, 10))

        ttk.Button(
            daybar, text="← Previous day",
            command=lambda: self.change_day(-1)
        ).pack(side="left")

        self.day_label = ttk.Label(
            daybar, text="", font=("Helvetica", 11, "bold")
        )
        self.day_label.pack(side="left", padx=16)

        ttk.Button(
            daybar, text="Today",
            command=self.show_today
        ).pack(side="left")

        ttk.Button(
            daybar, text="Next day →",
            command=lambda: self.change_day(1)
        ).pack(side="left", padx=6)

        # Dynamic Year Remaining 3D card
        year_panel = tk.Frame(
            daybar, bg="#EAFBF1", padx=13, pady=8,
            relief="raised", bd=2,
            highlightbackground="#8AD9AC", highlightthickness=1
        )
        year_panel.pack(side="right", padx=(12, 2))

        year_heading = tk.Frame(year_panel, bg="#EAFBF1")
        year_heading.pack(fill="x")

        tk.Label(
            year_heading, text="YEAR REMAINING",
            bg="#EAFBF1", fg="#176B45",
            font=("Helvetica", 8, "bold")
        ).pack(side="left")

        year_number = tk.Label(
            year_heading, text="",
            bg="#EAFBF1", fg="#124B35",
            font=("Helvetica", 13, "bold")
        )
        year_number.pack(side="right", padx=(12, 0))

        year_metrics = tk.Frame(year_panel, bg="#EAFBF1")
        year_metrics.pack(fill="x", pady=(5, 6))

        year_values = {}
        for column, unit in enumerate(("Days", "Weeks", "Months")):
            metric = tk.Frame(
                year_metrics, bg="#D8F3E4", padx=8, pady=3,
                relief="ridge", bd=1,
                highlightbackground="#B0E5C5", highlightthickness=1
            )
            metric.grid(row=0, column=column, padx=(0, 6), sticky="ew")

            number_label = tk.Label(
                metric, text="0",
                bg="#D8F3E4", fg="#145C3B",
                font=("Helvetica", 16, "bold")
            )
            number_label.pack(anchor="center")

            tk.Label(
                metric, text=unit,
                bg="#D8F3E4", fg="#39775A",
                font=("Helvetica", 10, "bold")
            ).pack(anchor="center")

            year_values[unit] = number_label

        year_metrics.grid_columnconfigure((0, 1, 2), weight=1)

        year_bar = tk.Canvas(
            year_panel, height=9, bg="#BFE8CF",
            highlightthickness=0, bd=0
        )
        year_bar.pack(fill="x")

        year_progress = {"value": 0.0}

        def draw_year_bar(event=None):
            width = year_bar.winfo_width()
            height = year_bar.winfo_height()
            if width <= 1 or height <= 1:
                return

            year_bar.delete("all")
            fill_width = int(width * year_progress["value"])
            if fill_width > 0:
                year_bar.create_rectangle(
                    0, 0, fill_width, height,
                    fill="#16A765", outline=""
                )
                if fill_width > 4:
                    year_bar.create_line(
                        2, 1, fill_width - 2, 1,
                        fill="#8CF5B7", width=1
                    )

        year_bar.bind("<Configure>", draw_year_bar)

        def update_year_remaining():
            today = date.today()
            year_start = date(today.year, 1, 1)
            year_end = date(today.year, 12, 31)
            next_year = date(today.year + 1, 1, 1)

            days_left = (year_end - today).days
            weeks_left = days_left // 7
            months_left = 12 - today.month

            total_days = (next_year - year_start).days
            elapsed_days = (today - year_start).days
            progress = elapsed_days / max(1, total_days - 1)
            year_progress["value"] = min(1.0, max(0.0, progress))

            year_number.config(text=str(today.year))
            year_values["Days"].config(text=str(days_left))
            year_values["Weeks"].config(text=str(weeks_left))
            year_values["Months"].config(text=str(months_left))

            draw_year_bar()
            self.root.after(60_000, update_year_remaining)

        update_year_remaining()

        # Live information
        live = tk.Frame(outer, bg=WHITE, padx=14, pady=11,
                        highlightbackground=BORDER, highlightthickness=1)
        live.pack(fill="x", pady=(0, 12))

        live_top = tk.Frame(live, bg=WHITE)
        live_top.pack(fill="x")
        clock_box = tk.Frame(live_top, bg=WHITE)
        clock_box.pack(side="left", fill="x", expand=True)
        tk.Label(clock_box, text="TORONTO LOCAL TIME", bg=WHITE,
                 fg=MUTED, font=("Helvetica", 8, "bold")).pack(anchor="w")
        self.clock_label = tk.Label(clock_box, text="Loading...",
                                    bg=WHITE, fg=TEXT,
                                    font=("Helvetica", 17, "bold"))
        self.clock_label.pack(anchor="w", pady=(3, 0))
        self.weather_label = tk.Label(live_top, text="Weather: Loading...",
                                      bg=WHITE, fg=TEXT,
                                      font=("Helvetica", 17, "bold"))
        self.weather_label.pack(side="right", padx=10)

        tk.Frame(live, bg=BORDER, height=1).pack(fill="x", pady=9)
        ftp = tk.Frame(live, bg=WHITE)
        ftp.pack(fill="x")
        tk.Label(ftp, text="FTPS SERVER", bg=WHITE, fg=MUTED,
                 font=("Helvetica", 9, "bold")).pack(side="left")
        self.ftp_status = tk.Label(
            ftp, text="● Checking...", bg=WHITE, fg="#D97706",
            font=("Helvetica", 10, "bold")
        )
        self.ftp_status.pack(side="left", padx=10)
        self.ftp_checked = tk.Label(
            ftp, text="Automatic monitoring",
            bg=WHITE, fg=MUTED, font=("Helvetica", 8)
        )
        self.ftp_checked.pack(side="left", padx=7)

        # Tracking status and action toolbar
        bar = tk.Frame(
            outer, bg=WHITE, padx=13, pady=11,
            highlightbackground=BORDER, highlightthickness=1
        )
        bar.pack(fill="x", pady=(0, 15))

        status_area = tk.Frame(bar, bg=WHITE)
        status_area.pack(side="left", fill="y")

        self.status = tk.Label(
            status_area, text="● TRACKING PAUSED",
            bg=WHITE, fg="#B45309",
            font=("Helvetica", 10, "bold")
        )
        self.status.pack(anchor="w")

        self.active_label = tk.Label(
            status_area,
            text="Tracking is temporarily paused. No activity is being recorded.",
            bg=WHITE, fg=MUTED,
            font=("Helvetica", 9)
        )
        self.active_label.pack(anchor="w", pady=(4, 0))

        actions = tk.Frame(bar, bg=WHITE)
        actions.pack(side="right")

        self.start_btn = ttk.Button(
            actions, text="▶  Start tracking",
            style="Primary.TButton", command=self.toggle
        )
        self.start_btn.pack(side="right", padx=(6, 0))

        ttk.Button(
            actions, text="Export CSV",
            style="Accent.TButton", command=self.export_csv
        ).pack(side="right", padx=5)

        ttk.Button(
            actions, text="Categories",
            command=self.show_categories
        ).pack(side="right", padx=5)

        ttk.Button(
            actions, text="✉ Mail Tracker",
            style="Accent.TButton", command=self.show_mail_tracker
        ).pack(side="right", padx=5)

        ttk.Button(
            actions, text="Statistics",
            style="Accent.TButton", command=self.show_statistics
        ).pack(side="right", padx=5)

        ttk.Button(
            actions, text="Screenshots ▾",
            command=lambda: self.show_screenshot_menu(self.root, self.chart)
        ).pack(side="right", padx=5)

        # KPI summary cards across the full dashboard width
        section_heading = tk.Frame(outer, bg=BG)
        section_heading.pack(fill="x", pady=(0, 8))

        tk.Label(
            section_heading, text="DAILY OVERVIEW",
            bg=BG, fg=TEXT,
            font=("Helvetica", 11, "bold")
        ).pack(side="left")

        tk.Label(
            section_heading, text="Your activity at a glance",
            bg=BG, fg=MUTED,
            font=("Helvetica", 9)
        ).pack(side="right")

        cards = tk.Frame(outer, bg=BG)
        cards.pack(fill="x", pady=(0, 16))
        self.kpi_labels = {}

        card_specs = [
            ("TRACKED TIME", "tracked", "#2563EB", "Total time recorded", "#EFF6FF", "#BFDBFE"),
            ("APPS USED", "apps", "#0F766E", "Unique applications", "#ECFDF5", "#A7F3D0"),
            ("INTERVALS", "sessions", "#7C3AED", "Recorded sessions", "#F5F3FF", "#DDD6FE"),
            ("TOP APP", "top", "#D97706", "Most-used application", "#FFFBEB", "#FDE68A"),
        ]

        for index, (name, key, accent, description, tint, edge) in enumerate(card_specs):
            card = tk.Frame(
                cards, bg=tint,
                highlightbackground=edge,
                highlightthickness=2,
                bd=1,
                relief="raised"
            )
            card.grid(
                row=0, column=index,
                sticky="nsew", padx=5, pady=5
            )

            tk.Frame(card, bg=accent, height=5).pack(fill="x")

            body = tk.Frame(card, bg=tint, padx=15, pady=12)
            body.pack(fill="both", expand=True)

            tk.Label(
                body, text=name,
                bg=tint, fg=accent,
                font=("Helvetica", 11, "bold"),
                anchor="center", justify="center"
            ).pack(fill="x")

            value = tk.Label(
                body, text="—",
                bg=tint, fg=TEXT,
                font=("Helvetica", 24, "bold"),
                anchor="center", justify="center"
            )
            value.pack(fill="x", pady=(5, 2))
            self.kpi_labels[key] = value

            tk.Label(
                body, text=description,
                bg=tint, fg="#64748B",
                font=("Helvetica", 10),
                anchor="center", justify="center"
            ).pack(fill="x")

        for column in range(4):
            cards.grid_columnconfigure(column, weight=1)
        cards.grid_rowconfigure(0, weight=1)

        # Two-column lower dashboard
        content = tk.Frame(outer, bg=BG)
        content.pack(fill="both", expand=True)

        overview_col = tk.Frame(content, bg=BG)
        overview_col.pack(side="left", fill="both", expand=True, padx=(0, 8))

        activity_col = tk.Frame(content, bg=BG)
        activity_col.pack(side="left", fill="both", expand=True, padx=(8, 0))

        # Overview analytics panel
        analytics = tk.Frame(
            overview_col, bg=WHITE,
            highlightbackground=BORDER,
            highlightthickness=1
        )
        analytics.pack(fill="both", expand=True, pady=(0, 20))

        analytics_head = tk.Frame(analytics, bg=WHITE, padx=17, pady=13)
        analytics_head.pack(fill="x")

        tk.Label(
            analytics_head, text="ACTIVITY ANALYTICS",
            bg=WHITE, fg=TEXT,
            font=("Helvetica", 11, "bold")
        ).pack(anchor="w")

        tk.Label(
            analytics_head, text="Time distribution across your most-used applications",
            bg=WHITE, fg=MUTED,
            font=("Helvetica", 9)
        ).pack(anchor="w", pady=(4, 0))

        tk.Frame(analytics, bg=BORDER, height=1).pack(fill="x")

        self.chart = tk.Canvas(
            analytics, bg=WHITE, height=320,
            highlightthickness=0, bd=0
        )
        self.chart.pack(fill="both", expand=True, padx=17, pady=12)

        # Activity log beside daily overview
        activity = tk.Frame(
            activity_col, bg=WHITE,
            highlightbackground=BORDER,
            highlightthickness=1
        )
        activity.pack(fill="both", expand=True)

        activity_head = tk.Frame(activity, bg=WHITE, padx=17, pady=12)
        activity_head.pack(fill="x")

        self.activity_title = tk.Label(
            activity_head, text="ACTIVITY LOG",
            bg=WHITE, fg=TEXT,
            font=("Helvetica", 11, "bold")
        )
        self.activity_title.pack(anchor="w")

        tk.Label(
            activity_head, text="Recent recorded activity for the selected date",
            bg=WHITE, fg=MUTED,
            font=("Helvetica", 9)
        ).pack(anchor="w", pady=(4, 0))

        tk.Frame(activity, bg=BORDER, height=1).pack(fill="x")

        table_area = tk.Frame(activity, bg=WHITE)
        table_area.pack(fill="both", expand=True, padx=12, pady=16)

        cols = ("start", "app", "category", "duration")
        ttk.Style(self.root).configure(
            "Activity.Treeview.Heading",
            font=("Helvetica", 10, "bold")
        )
        self.tree = ttk.Treeview(
            table_area, columns=cols,
            show="headings", height=10,
            style="Activity.Treeview"
        )
        self.tree.tag_configure("activity_even", background="#F1F7FF", foreground="#243B53")
        self.tree.tag_configure("activity_odd", background="#FFFFFF", foreground="#243B53")

        for col, label, width in [
            ("start", "START TIME", 90),
            ("app", "APPLICATION", 150),
            ("category", "CATEGORY", 105),
            ("duration", "DURATION", 85),
        ]:
            self.tree.heading(col, text=label, anchor="w")
            self.tree.column(col, width=width, minwidth=width, anchor="w", stretch=True)

        scrollbar = ttk.Scrollbar(
            table_area, orient="vertical",
            command=self.tree.yview
        )
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Fixed bottom footer outside the scrollable dashboard
        footer = tk.Frame(
            self.root, bg=WHITE,
            highlightbackground=BORDER,
            highlightthickness=1,
            padx=18, pady=12
        )
        footer.pack(side="bottom", fill="x")

        tk.Label(
            footer,
            text="LOCAL ACTIVITY DATA  •  Developer: Saiful Quadir  •  Version 2.00  •  Copyright © Saiful Quadir. All Rights Reserved.",
            bg=WHITE, fg="#334155",
            font=("Helvetica", 11, "bold")
        ).pack(side="left", padx=(4, 12))

        ttk.Button(
            footer, text="EXIT",
            style="Danger.TButton",
            padding=(24, 12),
            command=self.close
        ).pack(side="right", padx=4)

    def _update_live_clock(self):
        if self._closing:
            return
        now = datetime.now(ZoneInfo("America/Toronto"))
        self.clock_label.config(text=now.strftime("%A, %b %d  •  %I:%M:%S %p"))
        self._clock_after = self.root.after(1000, self._update_live_clock)

    def _animate_logo(self):
        if self._closing:
            return

        # Neon pulse animation with two luminous rings.
        phase = getattr(self, "_logo_phase", 0)
        self._logo_phase = (phase + 1) % 4

        neon_palette = [
            ("#B7FF00", "#F4FF9A", "#55FFB2"),
            ("#D7FF42", "#FFF7A8", "#78FFC1"),
            ("#9EFF24", "#E9FF83", "#35F5A5"),
            ("#E5FF00", "#FFF3A0", "#68FFB8"),
        ]
        ring_color, core_color, halo_color = neon_palette[self._logo_phase]

        self.logo.delete("all")
        self.logo.create_oval(
            1, 1, 53, 53, outline=halo_color, width=2
        )
        self.logo.create_oval(
            5, 5, 49, 49, outline=ring_color, width=3
        )
        self.logo.create_oval(
            12, 12, 42, 42, fill="#173D36",
            outline=core_color, width=2
        )
        self.logo.create_text(
            27, 27, text="D", fill=core_color,
            font=("Helvetica", 17, "bold")
        )

        self._logo_after = self.root.after(260, self._animate_logo)

    def _start_weather(self):
        if self._closing or self._weather_busy:
            return
        self._weather_busy = True
        threading.Thread(target=self._weather_worker, daemon=True).start()

    def _weather_worker(self):
        try:
            url = ("https://api.open-meteo.com/v1/forecast?"
                   "latitude=43.6532&longitude=-79.3832&"
                   "current=temperature_2m,weather_code&timezone=America%2FToronto")
            req = urllib.request.Request(url, headers={"User-Agent": "DailyActivityTracker"})
            with urllib.request.urlopen(req, timeout=8) as response:
                data = json.loads(response.read().decode("utf-8"))
            c = data["current"]
            code = c["weather_code"]
            descriptions = {
                0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Cloudy",
                45: "Fog", 48: "Fog", 51: "Drizzle", 53: "Drizzle",
                55: "Drizzle", 61: "Rain", 63: "Rain", 65: "Heavy rain",
                71: "Snow", 73: "Snow", 75: "Heavy snow",
                80: "Rain showers", 81: "Rain showers", 82: "Heavy showers",
                95: "Thunderstorm", 96: "Thunderstorm", 99: "Thunderstorm"
            }
            result = f"{c['temperature_2m']}°C  •  {descriptions.get(code, 'Current conditions')}"
        except Exception:
            result = "Unavailable"
        self.live_queue.put(("weather", result))

    def _start_ftp_monitor(self):
        if self._closing or self._ftp_busy:
            return
        self._ftp_busy = True
        self.ftp_status.config(text="● Checking...", fg="#D97706")
        threading.Thread(
            target=self._ftp_worker, daemon=True
        ).start()

    def _ftp_worker(self):
        online = False
        detail = "FTPS connection failed"
        ftp = None
        try:
            ftp = ftplib.FTP_TLS()
            ftp.connect("192.168.2.162", 21, timeout=5)
            response = ftp.auth()
            online = response.startswith("234")
            detail = "FTPS TLS handshake successful" if online else "Unexpected TLS response"
        except Exception as exc:
            detail = type(exc).__name__
        finally:
            if ftp is not None:
                try:
                    ftp.close()
                except Exception:
                    pass
        self.live_queue.put(("ftp", online, detail))

    def _blink_ftp_indicator(self):
        if self._closing or self._ftp_online:
            self._ftp_blink_after = None
            return
        self._ftp_blink_on = not self._ftp_blink_on
        self.ftp_status.config(
            fg="#DC2626" if self._ftp_blink_on else "#FFFFFF"
        )
        self._ftp_blink_after = self.root.after(
            600, self._blink_ftp_indicator
        )

    def _set_ftp_indicator(self, online):
        self._ftp_online = online
        if self._ftp_blink_after:
            try:
                self.root.after_cancel(self._ftp_blink_after)
            except tk.TclError:
                pass
            self._ftp_blink_after = None

        if online:
            self.ftp_status.config(text="● Online", fg="#15803D")
        else:
            self.ftp_status.config(text="● Offline", fg="#DC2626")
            self._ftp_blink_on = True
            self._blink_ftp_indicator()

    def _poll_live_queue(self):
        if self._closing:
            return
        try:
            while True:
                item = self.live_queue.get_nowait()
                if item[0] == "weather":
                    self._weather_busy = False
                    self.weather_label.config(text="Toronto: " + item[1])
                    self._weather_after = self.root.after(
                        900000, self._start_weather
                    )
                elif item[0] == "ftp":
                    self._ftp_busy = False
                    online, detail = item[1], item[2]
                    self._set_ftp_indicator(online)
                    self.ftp_checked.config(
                        text=datetime.now().strftime("Checked %I:%M:%S %p")
                        + " • " + detail
                    )
                    self._ftp_after = self.root.after(
                        30000, self._start_ftp_monitor
                    )
        except queue.Empty:
            pass
        self._queue_after = self.root.after(
            250, self._poll_live_queue
        )

    def show_screenshot_menu(self, parent, chart_canvas=None):
        menu = tk.Menu(parent, tearoff=0)
        menu.add_command(label="Save visible screen (PNG)…", command=lambda: self.capture_screen(parent, "visible"))
        menu.add_command(label="Save full screen (PNG)…", command=lambda: self.capture_screen(parent, "full"))
        if chart_canvas is not None:
            menu.add_command(label="Export chart (PostScript)…", command=lambda: self.export_chart(chart_canvas))
        menu.add_separator()
        menu.add_command(label="Open screenshot folder", command=lambda: self.open_screenshot_folder())
        try:
            menu.tk_popup(parent.winfo_pointerx(), parent.winfo_pointery())
        finally:
            menu.grab_release()

    def capture_screen(self, parent, capture_kind):
        """Capture the current macOS display; bring the intended app page to front first."""
        target = filedialog.asksaveasfilename(parent=parent, title="Save screenshot", defaultextension=".png",
            initialfile=f"daily_activity_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png",
            filetypes=[("PNG image", "*.png")])
        if not target:
            return
        try:
            parent.lift()
            parent.focus_force()
            parent.update_idletasks()
            # macOS screencapture captures the display as shown, including the selected page/window.
            capture_args = ["screencapture", "-i", "-x", target] if capture_kind == "visible" else ["screencapture", "-x", target]
            result = subprocess.run(capture_args, capture_output=True, text=True, timeout=60)
            if result.returncode != 0 or not Path(target).exists():
                raise RuntimeError(result.stderr.strip() or "macOS screenshot capture failed.")
            messagebox.showinfo("Screenshot saved", f"Saved PNG screenshot:\n{target}\n\nTo share it, use the Share menu in Finder.", parent=parent)
            try:
                subprocess.Popen(["open", "-R", target])
            except Exception:
                pass
        except Exception as exc:
            messagebox.showerror("Screenshot failed", str(exc), parent=parent)

    def export_chart(self, chart_canvas):
        target = filedialog.asksaveasfilename(parent=self.root, title="Export chart", defaultextension=".ps",
            initialfile=f"activity_chart_{self.view_day.isoformat()}.ps", filetypes=[("PostScript chart", "*.ps")])
        if target:
            try:
                chart_canvas.postscript(file=target, colormode="color")
                messagebox.showinfo("Chart exported", f"Chart saved to:\n{target}\n\nOpen it in Preview to view or convert it to PDF.", parent=self.root)
                subprocess.Popen(["open", "-R", target])
            except Exception as exc:
                messagebox.showerror("Chart export failed", str(exc), parent=self.root)

    def open_screenshot_folder(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["open", str(ROOT)])

    def change_day(self, offset):
        self.view_day += timedelta(days=offset)
        self.refresh()

    def show_today(self):
        self.view_day = date.today()
        self.refresh()

    def toggle(self):
        if not self.running:
            self.running = True
            self.started_at = time.time()
            self.status.config(text="● TRACKING ACTIVE", foreground="#15803D")
            self.active_label.config(
                text="Monitoring and recording the application currently in use."
            )
            self.start_btn.config(text="Ⅱ  Pause tracking")
            self.last_error = ""
            self.tick()
        else:
            self.stop_session()
            self.running = False
            if self.after_id:
                self.root.after_cancel(self.after_id)
                self.after_id = None
            self.status.config(text="● TRACKING PAUSED", foreground="#B45309")
            self.start_btn.config(text="▶  Resume tracking")
            self.active_label.config(
                text="Tracking is temporarily paused. No activity is being recorded."
            )
            self.refresh()

    def tick(self):
        if not self.running:
            return
        try:
            app = current_app()
            now = datetime.now()
            if self.active_app != app:
                self.stop_session()
                self.active_app = app
                self.session_start = now
            self.status.config(text="● TRACKING ACTIVE", foreground="#15803D")
            self.active_label.config(
                text=f"Currently recording activity for: {app}"
            )
            self.last_error = ""
        except Exception as e:
            self.stop_session()
            self.active_app = None
            self.status.config(text="● PERMISSION NEEDED", foreground="#DC2626")
            self.active_label.config(
                text="macOS permission is required to identify the active application."
            )
            self.last_error = str(e)
        self.refresh()
        self.after_id = self.root.after(POLL_SECONDS * 1000, self.tick)

    def stop_session(self):
        if self.active_app and self.session_start:
            end = datetime.now()
            start = self.session_start
            # Split at midnight so daily reports remain accurate.
            cursor = start
            while cursor.date() < end.date():
                midnight = datetime.combine(cursor.date() + timedelta(days=1), datetime.min.time())
                self._save_session(cursor, midnight)
                cursor = midnight
            if end > cursor:
                self._save_session(cursor, end)
        self.active_app = None
        self.session_start = None

    def _save_session(self, start, end):
        seconds = max(0, int((end - start).total_seconds()))
        if seconds < 1:
            return
        app = self.active_app or "Unknown"
        category = self.categories.get(app, "Other")
        append_row({"date": start.date().isoformat(), "start_time": start.strftime("%H:%M:%S"),
                    "end_time": end.strftime("%H:%M:%S"), "duration_seconds": seconds,
                    "app": app, "category": category, "device": "Mac", "activity": category})

    def refresh(self):
        rows = read_day(self.view_day)
        totals = defaultdict(int)
        for r in rows:
            try:
                totals[r["app"]] += int(float(r["duration_seconds"]))
            except Exception:
                pass

        # Include the live session only when viewing today.
        live_today = (
            self.running and self.active_app and self.session_start
            and self.view_day == date.today()
            and self.session_start.date() == date.today()
        )
        if live_today:
            totals[self.active_app] += max(0, int((datetime.now() - self.session_start).total_seconds()))

        self.day_label.config(text=self.view_day.strftime("%A, %B %d, %Y"))
        self.activity_title.config(text=f"ACTIVITY — {self.view_day.strftime('%b %d, %Y').upper()}")
        tracked = sum(totals.values())
        self.kpi_labels["tracked"].config(text=self.format_time(tracked))
        self.kpi_labels["apps"].config(text=str(len(totals)))
        self.kpi_labels["sessions"].config(
            text=f"{len(rows) + (1 if live_today else 0)} sessions",
            font=("Helvetica", 17, "bold")
        )
        top = max(totals, key=totals.get) if totals else "—"
        self.kpi_labels["top"].config(text=top if len(top) < 13 else top[:12] + "…")
        self.draw_chart(totals)

        for item in self.tree.get_children():
            self.tree.delete(item)
        for r in rows[-100:][::-1]:
            try:
                dur = self.format_time(int(float(r["duration_seconds"])))
            except Exception:
                dur = "—"
            row_tag = "activity_even" if len(self.tree.get_children()) % 2 == 0 else "activity_odd"
            self.tree.insert(
                "", "end",
                values=(r.get("start_time", "")[:5], r.get("app", ""), r.get("category", ""), dur),
                tags=(row_tag,)
            )

    def show_statistics(self):
        existing = getattr(self, "_statistics_window", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.deiconify()
                    existing.lift()
                    existing.focus_force()
                    return
            except Exception:
                pass

        win = tk.Toplevel(self.root)
        self._statistics_window = win
        win.title("Activity Statistics")
        win.geometry("1050x720")
        win.minsize(820, 580)
        win.configure(bg="#F4F7FB")
        win.transient(self.root)

        def close_window():
            self._statistics_window = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", close_window)

        outer = tk.Frame(win, bg="#F4F7FB", padx=22, pady=18)
        outer.pack(fill="both", expand=True)

        heading = tk.Frame(outer, bg="#F4F7FB")
        heading.pack(fill="x", pady=(0, 12))

        tk.Label(
            heading, text="ACTIVITY STATISTICS",
            bg="#F4F7FB", fg="#172B4D",
            font=("Helvetica", 19, "bold")
        ).pack(anchor="w")

        tk.Label(
            heading, text="Historical activity from your existing daily CSV records",
            bg="#F4F7FB", fg="#64748B",
            font=("Helvetica", 10)
        ).pack(anchor="w", pady=(4, 0))

        controls = tk.Frame(
            outer, bg="white", padx=13, pady=11,
            highlightbackground="#DCE4EF", highlightthickness=1
        )
        controls.pack(fill="x", pady=(0, 13))

        tk.Label(
            controls, text="VIEW",
            bg="white", fg="#64748B",
            font=("Helvetica", 9, "bold")
        ).pack(side="left", padx=(0, 9))

        mode_var = tk.StringVar(value="Daily")

        daily_button = tk.Button(
            controls, text="Daily Hours", width=12,
            command=lambda: switch_mode("Daily"),
            bg="#1E40AF", fg="white",
            activebackground="#1D4ED8", activeforeground="white",
            relief="raised", overrelief="sunken", bd=0,
            padx=8, pady=6,
            font=("Helvetica", 9, "bold"),
            cursor="hand2"
        )
        daily_button.pack(side="left", padx=(0, 5))
        daily_button.bind(
            "<Enter>", lambda event: daily_button.config(bg="#1D4ED8")
        )
        daily_button.bind(
            "<Leave>",
            lambda event: daily_button.config(
                bg="#1E40AF" if mode_var.get() == "Daily" else "#DBEAFE"
            )
        )

        monthly_button = tk.Button(
            controls, text="Monthly Active Days", width=17,
            command=lambda: switch_mode("Monthly"),
            bg="#0F766E", fg="white",
            activebackground="#14B8A6", activeforeground="white",
            relief="raised", overrelief="sunken", bd=0,
            padx=8, pady=6,
            font=("Helvetica", 9, "bold"),
            cursor="hand2"
        )
        monthly_button.pack(side="left", padx=(0, 15))
        monthly_button.bind(
            "<Enter>", lambda event: monthly_button.config(bg="#14B8A6")
        )
        monthly_button.bind(
            "<Leave>",
            lambda event: monthly_button.config(
                bg="#0F766E" if mode_var.get() == "Monthly" else "#CCFBF1"
            )
        )

        tk.Label(
            controls, text="PERIOD",
            bg="white", fg="#64748B",
            font=("Helvetica", 9, "bold")
        ).pack(side="left", padx=(0, 8))

        period_var = tk.StringVar(value="7 Days")
        period_box = ttk.Combobox(
            controls, textvariable=period_var, state="readonly",
            values=("3 Days", "5 Days", "7 Days", "10 Days",
                    "30 Days", "45 Days", "60 Days", "90 Days",
                    "6 Months", "1 Year"),
            width=12
        )
        period_box.pack(side="left")

        import calendar

        today = date.today()
        month_options = []
        for offset in range(36):
            month_index = today.year * 12 + today.month - 1 - offset
            year, month_zero = divmod(month_index, 12)
            month_options.append(date(year, month_zero + 1, 1).strftime("%B %Y"))

        month_var = tk.StringVar(value=today.strftime("%B %Y"))
        month_box = ttk.Combobox(
            controls, textvariable=month_var, state="readonly",
            values=month_options, width=16
        )

        summary = tk.Frame(outer, bg="#F4F7FB")
        summary.pack(fill="x", pady=(0, 13))

        card_titles = []
        cards = []
        card_styles = [
            ("#DCEBFF", "#28558C", "#173E70", "#A9C9F2"),
            ("#D9F5E6", "#23744B", "#145C39", "#A5DFC0"),
            ("#F0E4FF", "#7045A0", "#542B82", "#D2B7F0"),
            ("#FFE9D7", "#A85D25", "#854315", "#F2C6A4"),
        ]
        for index, title in enumerate(("TOTAL TRACKED", "DAILY AVERAGE", "ACTIVE DAYS", "HIGHEST ACTIVITY DAY")):
            card = tk.Frame(
                summary, bg=card_styles[index][0], padx=13, pady=12, relief="raised", bd=2,
                highlightbackground=card_styles[index][3], highlightthickness=2
            )
            card.pack(side="left", fill="both", expand=True, padx=(0, 8))

            title_label = tk.Label(
                card, text=title, bg=card_styles[index][0], fg=card_styles[index][1],
                font=("Helvetica", 10, "bold"), anchor="center"
            )
            title_label.pack(fill="x")
            card_titles.append(title_label)

            value = tk.Label(
                card, text="Loading...", bg=card_styles[index][0], fg=card_styles[index][2],
                font=("Helvetica", 22, "bold"), anchor="center"
            )
            value.pack(fill="x", pady=(7, 0))
            cards.append(value)

        graph_frame = tk.Frame(
            outer, bg="white", padx=12, pady=12,
            highlightbackground="#DCE4EF", highlightthickness=1
        )
        graph_frame.pack(fill="both", expand=True)

        graph_heading = tk.Frame(graph_frame, bg="white")
        graph_heading.pack(fill="x", pady=(0, 8))

        graph_title = tk.Label(
            graph_heading, text="DAILY TRACKED HOURS",
            bg="white", fg="#172B4D",
            font=("Helvetica", 10, "bold")
        )
        graph_title.pack(side="left")

        range_label = tk.Label(
            graph_heading, text="", bg="white", fg="#0F766E",
            font=("Helvetica", 10, "bold")
        )
        range_label.pack(side="right")

        chart_area = tk.Frame(graph_frame, bg="white")
        chart_area.pack(fill="both", expand=True)

        chart = tk.Canvas(
            chart_area, bg="white", highlightthickness=0,
            height=360
        )
        horizontal = ttk.Scrollbar(
            chart_area, orient="horizontal", command=chart.xview
        )
        chart.configure(xscrollcommand=horizontal.set)
        chart.pack(fill="both", expand=True)
        horizontal.pack(fill="x", pady=(5, 0))

        footer = tk.Frame(outer, bg="#F4F7FB")
        footer.pack(fill="x", pady=(9, 0))

        tk.Label(
            footer,
            text="Source: Existing activity CSV files  •  Missing days are shown as zero",
            bg="#F4F7FB", fg="#64748B",
            font=("Helvetica", 8)
        ).pack(side="left")


        def get_day_seconds(day):
            seconds = 0
            for row in read_day(day):
                try:
                    seconds += max(
                        0, int(float(row.get("duration_seconds", 0)))
                    )
                except (TypeError, ValueError):
                    pass

            if (
                day == date.today()
                and self.running
                and self.active_app
                and self.session_start
                and self.session_start.date() == date.today()
            ):
                seconds += max(
                    0, int((datetime.now() - self.session_start).total_seconds())
                )

            return seconds

        def subtract_months(day, months):
            month_index = day.year * 12 + day.month - 1 - months
            year, month_zero = divmod(month_index, 12)
            month = month_zero + 1
            last_day = calendar.monthrange(year, month)[1]
            return date(year, month, min(day.day, last_day))

        def selected_dates(label):
            current_day = date.today()
            if label == "6 Months":
                start = subtract_months(current_day, 6)
            elif label == "1 Year":
                start = subtract_months(current_day, 12)
            else:
                count = int(label.split()[0])
                start = current_day - timedelta(days=count - 1)

            count = (current_day - start).days + 1
            return [start + timedelta(days=i) for i in range(count)]

        def update_cards(titles, values):
            for label, value_label, title, value in zip(
                card_titles, cards, titles, values
            ):
                label.config(text=title)
                value_label.config(text=value, font=("Helvetica", 22, "bold"))

        def draw_daily_graph(daily_totals):
            graph_title.config(text="DAILY TRACKED HOURS")
            chart.delete("all")
            chart.update_idletasks()

            visible_width = max(650, chart.winfo_width())
            height = max(300, chart.winfo_height())
            left, right, top, bottom = 58, 22, 48, 52
            plot_height = height - top - bottom
            # Up to 30 bars fit the visible frame.
            # Beyond 30, preserve readable bar spacing and expand the canvas.
            minimum_bar_step = 19
            available_plot_width = max(1, visible_width - left - right)

            if len(daily_totals) <= 30:
                plot_width = available_plot_width
            else:
                plot_width = max(
                    available_plot_width,
                    len(daily_totals) * minimum_bar_step
                )

            bar_step = plot_width / max(1, len(daily_totals))
            bar_width = min(15, max(2, bar_step * 0.68)) if daily_totals else 0
            chart_width = left + plot_width + right

            legend_items = (
                ("0h", "#E9A0A0"),
                ("1–4h", "#78C99A"),
                ("4–6h", "#3C9D70"),
                ("6–8h", "#3186D8"),
                ("8–10h", "#7956C8"),
                ("10–12h", "#E49A32"),
                (">12h", "#D94B55"),
            )

            legend_x = left
            for label, color in legend_items:
                chart.create_rectangle(
                    legend_x, 10, legend_x + 12, 22,
                    fill=color, outline=""
                )
                chart.create_text(
                    legend_x + 17, 16,
                    text=label, anchor="w",
                    fill="#334155",
                    font=("Helvetica", 11, "bold")
                )
                legend_x += 80 if label != "10–12h" else 90

            maximum = max(
                (seconds for _, seconds in daily_totals),
                default=0
            )
            scale_max = max(
                3600,
                ((maximum + 3599) // 3600) * 3600
            )

            def activity_color(seconds):
                hours = seconds / 3600
                if seconds <= 0:
                    return "#E9A0A0"
                if hours <= 4:
                    return "#78C99A"
                if hours <= 6:
                    return "#3C9D70"
                if hours <= 8:
                    return "#3186D8"
                if hours <= 10:
                    return "#7956C8"
                if hours <= 12:
                    return "#E49A32"
                return "#D94B55"

            for tick in range(5):
                value = scale_max * tick / 4
                y = top + plot_height - (plot_height * tick / 4)
                chart.create_line(
                    left, y, chart_width - right, y,
                    fill="#E8EDF4", dash=(2, 3)
                )
                chart.create_text(
                    left - 9, y,
                    text=f"{value / 3600:.0f}h",
                    anchor="e", fill="#334155",
                    font=("Helvetica", 10, "bold")
                )

            label_interval = max(1, len(daily_totals) // 12)

            for index, (day, seconds) in enumerate(daily_totals):
                x = left + index * bar_step + (bar_step - bar_width) / 2
                bar_height = (seconds / scale_max) * plot_height
                y = top + plot_height - bar_height

                chart.create_rectangle(
                    x, y, x + bar_width, top + plot_height,
                    fill=activity_color(seconds),
                    outline=""
                )

                if index % label_interval == 0 or index == len(daily_totals) - 1:
                    chart.create_text(
                        x + bar_width / 2,
                        top + plot_height + 17,
                        text=day.strftime("%b %d"),
                        fill="#64748B",
                        font=("Helvetica", 8),
                        angle=45
                    )

            if not any(seconds for _, seconds in daily_totals):
                chart.create_text(
                    visible_width / 2,
                    top + plot_height / 2,
                    text="No activity recorded in this period",
                    fill="#94A3B8",
                    font=("Helvetica", 12)
                )

            chart.configure(scrollregion=(0, 0, chart_width, height))
            chart.xview_moveto(0)

        screenshot_state = {"path": None}

        def capture_statistics_screenshot():
            """Render the complete Statistics chart directly with Pillow.

            This deliberately avoids PostScript/EPS and macOS print rendering,
            which can fail for wide, scrollable Canvas charts.
            """
            import tempfile
            from PIL import Image, ImageDraw, ImageFont

            win.update_idletasks()
            chart.update_idletasks()

            if mode_var.get() == "Monthly":
                month_start = datetime.strptime(month_var.get(), "%B %Y").date()
                month_end_day = calendar.monthrange(month_start.year, month_start.month)[1]
                month_end = date(month_start.year, month_start.month, month_end_day)
                effective_end = min(month_end, date.today())
                days = [
                    month_start + timedelta(days=i)
                    for i in range((effective_end - month_start).days + 1)
                ] if effective_end >= month_start else []
                bar_step, bar_width, bottom = 27, 17, 55
                empty_message = "No activity recorded in this month"
            else:
                days = selected_dates(period_var.get())
                bar_step, bar_width, bottom = 25, 15, 52
                empty_message = "No activity recorded in this period"

            daily_totals = [(day, get_day_seconds(day)) for day in days]
            if mode_var.get() != "Monthly":
                daily_totals = [
                    (day, seconds)
                    for day, seconds in daily_totals
                    if seconds > 0
                ]

            visible_width = max(650, chart.winfo_width())
            height = max(300, chart.winfo_height())
            left, right, top = 58, 22, 48
            plot_height = max(100, height - top - bottom)

            if mode_var.get() == "Monthly":
                plot_width = max(
                    visible_width - left - right,
                    len(daily_totals) * bar_step
                )
                full_width = max(1, left + plot_width + right)
            else:
                minimum_bar_step = 19
                available_plot_width = max(1, visible_width - left - right)

                if len(daily_totals) <= 30:
                    plot_width = available_plot_width
                else:
                    plot_width = max(
                        available_plot_width,
                        len(daily_totals) * minimum_bar_step
                    )

                bar_step = plot_width / max(1, len(daily_totals))
                bar_width = min(15, max(2, bar_step * 0.68)) if daily_totals else 0
                full_width = max(1, left + plot_width + right)
            chart_bottom = top + plot_height

            maximum = max((seconds for _, seconds in daily_totals), default=0)
            scale_max = max(3600, ((maximum + 3599) // 3600) * 3600)

            def activity_color(seconds):
                hours = seconds / 3600
                if seconds <= 0:
                    return "#E9A0A0"
                if hours <= 4:
                    return "#78C99A"
                if hours <= 6:
                    return "#3C9D70"
                if hours <= 8:
                    return "#3186D8"
                if hours <= 10:
                    return "#7956C8"
                if hours <= 12:
                    return "#E49A32"
                return "#D94B55"

            def load_font(size, bold=False):
                candidates = (
                    ["/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                     "/System/Library/Fonts/Supplemental/Arial.ttf"] if bold else
                    ["/System/Library/Fonts/Supplemental/Arial.ttf"]
                )
                for candidate in candidates:
                    try:
                        return ImageFont.truetype(candidate, size)
                    except OSError:
                        continue
                return ImageFont.load_default()

            font_title = load_font(23, True)
            font_subtitle = load_font(12)
            font_label = load_font(12, True)
            font_value = load_font(20, True)
            font_axis = load_font(10, True)
            font_date = load_font(8)
            font_legend = load_font(11, True)

            header_height = 174
            report = Image.new("RGB", (full_width, height + header_height + 50), "#F4F7FB")
            draw = ImageDraw.Draw(report)
            draw.text((24, 18), "ACTIVITY STATISTICS", fill="#172B4D", font=font_title)
            draw.text(
                (24, 52),
                f"{graph_title.cget('text')}  |  {range_label.cget('text')}",
                fill="#64748B", font=font_subtitle
            )

            card_count = max(1, len(cards))
            gap = 10
            card_left = 24
            card_width = max(120, (full_width - 48 - gap * (card_count - 1)) // card_count)
            card_top, card_bottom = 82, 158
            for index, (title_widget, value_widget) in enumerate(zip(card_titles, cards)):
                x0 = card_left + index * (card_width + gap)
                x1 = min(full_width - 24, x0 + card_width)
                draw.rounded_rectangle(
                    (x0, card_top, x1, card_bottom), radius=8,
                    fill=card_styles[index][0], outline=card_styles[index][3], width=2
                )
                draw.text((x0 + 12, card_top + 12), title_widget.cget("text"),
                          fill=card_styles[index][1], font=font_label)
                draw.text((x0 + 12, card_top + 35), value_widget.cget("text"),
                          fill=card_styles[index][2], font=font_value)

            # Draw the entire chart area, including all off-screen bars.
            chart_top = header_height
            legend_items = (
                ("0h", "#E9A0A0"), ("1–4h", "#78C99A"),
                ("4–6h", "#3C9D70"), ("6–8h", "#3186D8"),
                ("8–10h", "#7956C8"), ("10–12h", "#E49A32"),
                (">12h", "#D94B55")
            )
            legend_x = left
            for label, color in legend_items:
                draw.rectangle((legend_x, chart_top + 10, legend_x + 12, chart_top + 22), fill=color)
                draw.text((legend_x + 17, chart_top + 9), label, fill="#334155", font=font_legend)
                legend_x += 90 if label == "10–12h" else 80

            for tick in range(5):
                value = scale_max * tick / 4
                y = top + plot_height - (plot_height * tick / 4)
                draw.line((left, chart_top + y, full_width - right, chart_top + y),
                          fill="#E8EDF4", width=1)
                draw.text((left - 9, chart_top + y - 6), f"{value / 3600:.0f}h",
                          fill="#334155", font=font_axis, anchor="rm")

            label_interval = max(1, len(daily_totals) // 12)
            for index, (day, seconds) in enumerate(daily_totals):
                x = left + index * bar_step + (bar_step - bar_width) / 2
                bar_height = (seconds / scale_max) * plot_height
                y = top + plot_height - bar_height
                draw.rectangle(
                    (int(x), int(chart_top + y), int(x + bar_width), int(chart_top + chart_bottom)),
                    fill=activity_color(seconds)
                )
                if mode_var.get() == "Monthly" or index % label_interval == 0 or index == len(daily_totals) - 1:
                    label = str(day.day) if mode_var.get() == "Monthly" else day.strftime("%b %d")
                    if mode_var.get() == "Monthly":
                        draw.text((x + bar_width / 2, chart_top + chart_bottom + 17), label,
                                  fill="#64748B", font=font_date, anchor="mm")
                    else:
                        # Rotate daily date labels to match the on-screen chart.
                        label_img = Image.new("RGBA", (72, 18), (255, 255, 255, 0))
                        label_draw = ImageDraw.Draw(label_img)
                        label_draw.text((1, 1), label, fill="#64748B", font=font_date)
                        label_img = label_img.rotate(45, expand=True)
                        report.paste(label_img, (int(x + bar_width / 2 - 5),
                                                 int(chart_top + chart_bottom + 10)), label_img)

            if not any(seconds for _, seconds in daily_totals):
                draw.text((full_width / 2, chart_top + top + plot_height / 2),
                          empty_message, fill="#94A3B8", font=load_font(12), anchor="mm")

            fd, image_path = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            try:
                report.save(image_path, format="PNG", optimize=True)
                with Image.open(image_path) as check_image:
                    check_image.verify()
                with Image.open(image_path) as check_image:
                    if check_image.width != full_width:
                        raise RuntimeError("The complete chart width was not preserved in the exported image.")
            except Exception:
                Path(image_path).unlink(missing_ok=True)
                raise

            old_path = screenshot_state.get("path")
            if old_path:
                try:
                    Path(old_path).unlink(missing_ok=True)
                except OSError:
                    pass
            screenshot_state["path"] = image_path
            return image_path

        def save_statistics_screenshot():
            from PIL import Image

            image_path = capture_statistics_screenshot()
            if not image_path:
                return

            destination = filedialog.asksaveasfilename(
                parent=win,
                title="Save Statistics Screenshot",
                initialfile="Activity_Statistics_Screenshot",
                defaultextension=".png",
                filetypes=[
                    ("PNG Image", "*.png"),
                    ("JPEG Image", "*.jpg"),
                    ("PDF Document", "*.pdf")
                ]
            )

            if not destination:
                return

            try:
                with Image.open(image_path) as image:
                    extension = Path(destination).suffix.lower()

                    if extension in (".jpg", ".jpeg"):
                        image.convert("RGB").save(
                            destination, "JPEG", quality=95
                        )
                    elif extension == ".pdf":
                        image.convert("RGB").save(
                            destination, "PDF", resolution=150.0
                        )
                    else:
                        image.save(destination, "PNG")

                messagebox.showinfo(
                    "Screenshot Saved",
                    f"Screenshot saved successfully:\n{destination}",
                    parent=win
                )
            except Exception as exc:
                messagebox.showerror(
                    "Save Error", str(exc), parent=win
                )

        def preview_statistics_screenshot():
            from PIL import Image, ImageTk

            image_path = capture_statistics_screenshot()
            if not image_path:
                return

            try:
                original_image = Image.open(image_path).convert("RGB")

                preview = tk.Toplevel(win)
                preview.title("Activity Statistics - Full Preview")
                preview.geometry("1100x760")
                preview.minsize(700, 500)
                preview.configure(bg="#F4F7FB")
                # Keep Preview independent for native macOS Full Screen.

                zoom_state = {"value": 1.0}

                toolbar = tk.Frame(preview, bg="#EAF0F7", padx=10, pady=8)
                toolbar.pack(fill="x")

                tk.Label(
                    toolbar,
                    text="FULL STATISTICS PREVIEW",
                    bg="#EAF0F7",
                    fg="#172B4D",
                    font=("Helvetica", 10, "bold")
                ).pack(side="left", padx=(2, 16))

                zoom_label = tk.Label(
                    toolbar,
                    text="100%",
                    bg="#EAF0F7",
                    fg="#0F766E",
                    font=("Helvetica", 10, "bold"),
                    width=7
                )
                zoom_label.pack(side="left", padx=5)

                body = tk.Frame(preview, bg="#F4F7FB")
                body.pack(fill="both", expand=True, padx=10, pady=8)

                preview_canvas = tk.Canvas(
                    body,
                    bg="#CBD5E1",
                    highlightthickness=0
                )
                horizontal_bar = ttk.Scrollbar(
                    body, orient="horizontal",
                    command=preview_canvas.xview
                )
                vertical_bar = ttk.Scrollbar(
                    body, orient="vertical",
                    command=preview_canvas.yview
                )

                preview_canvas.configure(
                    xscrollcommand=horizontal_bar.set,
                    yscrollcommand=vertical_bar.set
                )

                preview_canvas.grid(row=0, column=0, sticky="nsew")
                vertical_bar.grid(row=0, column=1, sticky="ns")
                horizontal_bar.grid(row=1, column=0, sticky="ew")

                body.grid_rowconfigure(0, weight=1)
                body.grid_columnconfigure(0, weight=1)

                def render_preview():
                    zoom = zoom_state["value"]
                    new_width = max(1, int(original_image.width * zoom))
                    new_height = max(1, int(original_image.height * zoom))

                    resized = original_image.resize(
                        (new_width, new_height),
                        Image.Resampling.LANCZOS
                    )
                    photo = ImageTk.PhotoImage(resized)

                    preview_canvas.delete("all")
                    preview_canvas.create_image(
                        0, 0, image=photo, anchor="nw"
                    )
                    preview_canvas.image = photo
                    preview_canvas.configure(
                        scrollregion=(0, 0, new_width, new_height)
                    )
                    zoom_label.config(text=f"{zoom * 100:.0f}%")

                def change_zoom(amount):
                    zoom_state["value"] = min(
                        3.0,
                        max(0.25, zoom_state["value"] + amount)
                    )
                    render_preview()

                def fit_height():
                    preview.update_idletasks()
                    available_height = max(
                        250, preview_canvas.winfo_height() - 20
                    )
                    zoom_state["value"] = min(
                        1.0,
                        available_height / original_image.height
                    )
                    render_preview()
                    preview_canvas.xview_moveto(0)
                    preview_canvas.yview_moveto(0)

                def toggle_fullscreen():
                    current_state = bool(preview.attributes("-fullscreen"))
                    preview.attributes("-fullscreen", not current_state)

                def exit_fullscreen(event=None):
                    preview.attributes("-fullscreen", False)

                def make_toolbar_control(
                    text, command, bg, fg="white", active_bg=None,
                    side="left", padx=2, pady=5, font_size=11,
                    bold=True, width=None
                ):
                    control = tk.Label(
                        toolbar,
                        text=text,
                        bg=bg,
                        fg=fg,
                        padx=padx,
                        pady=pady,
                        font=("Helvetica", font_size, "bold" if bold else "normal"),
                        cursor="hand2",
                        highlightthickness=0
                    )
                    if width is not None:
                        control.configure(width=width)

                    control.bind("<Button-1>", lambda event: command())
                    control.bind(
                        "<Enter>",
                        lambda event: control.configure(bg=active_bg or bg)
                    )
                    control.bind(
                        "<Leave>",
                        lambda event: control.configure(bg=bg)
                    )
                    control.pack(side=side, padx=2)
                    return control

                make_toolbar_control(
                    "⌕ −", lambda: change_zoom(-0.20),
                    "#FACC15", "#422006", "#EAB308",
                    font_size=13, width=3
                )

                make_toolbar_control(
                    "⌕ +", lambda: change_zoom(0.20),
                    "#16A34A", "white", "#15803D",
                    font_size=13, width=3
                )

                make_toolbar_control(
                    "↕", fit_height,
                    "#2563EB", "white", "#1D4ED8",
                    font_size=13, width=3
                )

                make_toolbar_control(
                    "Full Screen", toggle_fullscreen,
                    "#7C3AED", "white", "#6D28D9",
                    side="right", padx=10, pady=5, font_size=10
                )

                preview.bind("<Escape>", exit_fullscreen)

                def close_preview():
                    original_image.close()
                    preview.destroy()

                preview.protocol("WM_DELETE_WINDOW", close_preview)

                make_toolbar_control(
                    "✕ Exit", close_preview,
                    "#DC2626", "white", "#B91C1C",
                    side="right", padx=9, pady=5, font_size=9
                )

                preview.update_idletasks()
                render_preview()

            except Exception as exc:
                messagebox.showerror(
                    "Preview Error", str(exc), parent=win
                )


        def make_footer_control(text, command, bg, hover_bg):
            control = tk.Label(
                footer,
                text=text,
                bg=bg,
                fg="#172B4D",
                padx=10,
                pady=6,
                font=("Helvetica", 9, "bold"),
                cursor="hand2",
                highlightthickness=0
            )
            control.bind("<Button-1>", lambda event: command())
            control.bind("<Enter>", lambda event: control.configure(bg=hover_bg))
            control.bind("<Leave>", lambda event: control.configure(bg=bg))
            control.pack(side="right", padx=3)
            return control

        # Red: Exit
        make_footer_control("✕ Exit", close_window, "#F87171", "#EF4444")

        # Green: Preview
        make_footer_control(
            "Preview", preview_statistics_screenshot, "#86EFAC", "#4ADE80"
        )

        # Blue: Screenshot
        make_footer_control(
            "Screenshot", save_statistics_screenshot, "#93C5FD", "#60A5FA"
        )
        def get_day_seconds(day):
            seconds = 0
            for row in read_day(day):
                try:
                    seconds += max(
                        0, int(float(row.get("duration_seconds", 0)))
                    )
                except (TypeError, ValueError):
                    pass

            if (
                day == date.today()
                and self.running
                and self.active_app
                and self.session_start
                and self.session_start.date() == date.today()
            ):
                seconds += max(
                    0, int((datetime.now() - self.session_start).total_seconds())
                )

            return seconds

        def subtract_months(day, months):
            month_index = day.year * 12 + day.month - 1 - months
            year, month_zero = divmod(month_index, 12)
            month = month_zero + 1
            last_day = calendar.monthrange(year, month)[1]
            return date(year, month, min(day.day, last_day))

        def selected_dates(label):
            current_day = date.today()
            if label == "6 Months":
                start = subtract_months(current_day, 6)
            elif label == "1 Year":
                start = subtract_months(current_day, 12)
            else:
                count = int(label.split()[0])
                start = current_day - timedelta(days=count - 1)

            count = (current_day - start).days + 1
            return [start + timedelta(days=i) for i in range(count)]

        def update_cards(titles, values):
            for label, value_label, title, value in zip(
                card_titles, cards, titles, values
            ):
                label.config(text=title)
                value_label.config(text=value, font=("Helvetica", 22, "bold"))

        def draw_daily_graph(daily_totals):
            graph_title.config(text="DAILY TRACKED HOURS")
            chart.delete("all")
            chart.update_idletasks()

            visible_width = max(650, chart.winfo_width())
            height = max(300, chart.winfo_height())
            left, right, top, bottom = 58, 22, 48, 52
            plot_height = height - top - bottom
            # Up to 30 bars fit the visible frame.
            # Beyond 30, preserve readable bar spacing and expand the canvas.
            minimum_bar_step = 19
            available_plot_width = max(1, visible_width - left - right)

            if len(daily_totals) <= 30:
                plot_width = available_plot_width
            else:
                plot_width = max(
                    available_plot_width,
                    len(daily_totals) * minimum_bar_step
                )

            bar_step = plot_width / max(1, len(daily_totals))
            bar_width = min(15, max(2, bar_step * 0.68)) if daily_totals else 0
            chart_width = left + plot_width + right

            legend_items = (
                ("0h", "#E9A0A0"),
                ("1–4h", "#78C99A"),
                ("4–6h", "#3C9D70"),
                ("6–8h", "#3186D8"),
                ("8–10h", "#7956C8"),
                ("10–12h", "#E49A32"),
                (">12h", "#D94B55"),
            )

            legend_x = left
            for label, color in legend_items:
                chart.create_rectangle(
                    legend_x, 10, legend_x + 12, 22,
                    fill=color, outline=""
                )
                chart.create_text(
                    legend_x + 17, 16,
                    text=label, anchor="w",
                    fill="#334155",
                    font=("Helvetica", 11, "bold")
                )
                legend_x += 80 if label != "10–12h" else 90

            maximum = max(
                (seconds for _, seconds in daily_totals),
                default=0
            )
            scale_max = max(
                3600,
                ((maximum + 3599) // 3600) * 3600
            )

            def activity_color(seconds):
                hours = seconds / 3600
                if seconds <= 0:
                    return "#E9A0A0"
                if hours <= 4:
                    return "#78C99A"
                if hours <= 6:
                    return "#3C9D70"
                if hours <= 8:
                    return "#3186D8"
                if hours <= 10:
                    return "#7956C8"
                if hours <= 12:
                    return "#E49A32"
                return "#D94B55"

            for tick in range(5):
                value = scale_max * tick / 4
                y = top + plot_height - (plot_height * tick / 4)
                chart.create_line(
                    left, y, chart_width - right, y,
                    fill="#E8EDF4", dash=(2, 3)
                )
                chart.create_text(
                    left - 9, y,
                    text=f"{value / 3600:.0f}h",
                    anchor="e", fill="#334155",
                    font=("Helvetica", 10, "bold")
                )

            label_interval = max(1, len(daily_totals) // 12)

            for index, (day, seconds) in enumerate(daily_totals):
                x = left + index * bar_step + (bar_step - bar_width) / 2
                bar_height = (seconds / scale_max) * plot_height
                y = top + plot_height - bar_height

                chart.create_rectangle(
                    x, y, x + bar_width, top + plot_height,
                    fill=activity_color(seconds),
                    outline=""
                )

                if index % label_interval == 0 or index == len(daily_totals) - 1:
                    chart.create_text(
                        x + bar_width / 2,
                        top + plot_height + 17,
                        text=day.strftime("%b %d"),
                        fill="#64748B",
                        font=("Helvetica", 8),
                        angle=45
                    )

            if not any(seconds for _, seconds in daily_totals):
                chart.create_text(
                    visible_width / 2,
                    top + plot_height / 2,
                    text="No activity recorded in this period",
                    fill="#94A3B8",
                    font=("Helvetica", 12)
                )

            chart.configure(scrollregion=(0, 0, chart_width, height))
            chart.xview_moveto(0)


        def draw_monthly_graph(month_start, daily_totals):
            graph_title.config(text="MONTHLY TRACKED HOURS")
            chart.delete("all")
            chart.update_idletasks()

            visible_width = max(650, chart.winfo_width())
            height = max(300, chart.winfo_height())
            left, right, top, bottom = 58, 22, 48, 55
            plot_height = height - top - bottom

            days_count = len(daily_totals)
            bar_step, bar_width = 27, 17
            plot_width = max(
                visible_width - left - right,
                days_count * bar_step
            )
            chart_width = left + plot_width + right

            # Activity colors are based on actual tracked hours.
            legend_items = (
                ("0h", "#E9A0A0"),
                ("1–4h", "#78C99A"),
                ("4–6h", "#3C9D70"),
                ("6–8h", "#3186D8"),
                ("8–10h", "#7956C8"),
                ("10–12h", "#E49A32"),
                (">12h", "#D94B55"),
            )

            legend_x = left
            for label, color in legend_items:
                chart.create_rectangle(
                    legend_x, 10, legend_x + 12, 22,
                    fill=color, outline=""
                )
                chart.create_text(
                    legend_x + 17, 16,
                    text=label, anchor="w",
                    fill="#334155",
                    font=("Helvetica", 11, "bold")
                )
                legend_x += 80 if label != "10–12h" else 90

            maximum = max(
                (seconds for _, seconds in daily_totals),
                default=0
            )
            scale_max = max(
                3600,
                ((maximum + 3599) // 3600) * 3600
            )

            for tick in range(5):
                value = scale_max * tick / 4
                y = top + plot_height - (plot_height * tick / 4)

                chart.create_line(
                    left, y, chart_width - right, y,
                    fill="#E8EDF4", dash=(2, 3)
                )
                chart.create_text(
                    left - 9, y,
                    text=f"{value / 3600:.0f}h",
                    anchor="e", fill="#334155",
                    font=("Helvetica", 10, "bold")
                )

            def activity_color(seconds):
                hours = seconds / 3600
                if seconds <= 0:
                    return "#E9A0A0"
                if hours <= 4:
                    return "#78C99A"
                if hours <= 6:
                    return "#3C9D70"
                if hours <= 8:
                    return "#3186D8"
                if hours <= 10:
                    return "#7956C8"
                if hours <= 12:
                    return "#E49A32"
                return "#D94B55"

            for index, (day, seconds) in enumerate(daily_totals):
                x = left + index * bar_step + (bar_step - bar_width) / 2
                bar_height = (seconds / scale_max) * plot_height
                y = top + plot_height - bar_height

                chart.create_rectangle(
                    x, y, x + bar_width, top + plot_height,
                    fill=activity_color(seconds),
                    outline=""
                )

                chart.create_text(
                    x + bar_width / 2,
                    top + plot_height + 17,
                    text=str(day.day),
                    fill="#64748B",
                    font=("Helvetica", 8)
                )

            if not any(seconds for _, seconds in daily_totals):
                chart.create_text(
                    visible_width / 2,
                    top + plot_height / 2,
                    text="No activity recorded in this month",
                    fill="#94A3B8",
                    font=("Helvetica", 12)
                )

            chart.configure(scrollregion=(0, 0, chart_width, height))
            chart.xview_moveto(0)

        def render_statistics(event=None):
            if mode_var.get() == "Monthly":
                month_start = datetime.strptime(
                    month_var.get(), "%B %Y"
                ).date()
                last_day = calendar.monthrange(
                    month_start.year, month_start.month
                )[1]
                month_end = date(
                    month_start.year, month_start.month, last_day
                )

                # Do not count future dates in the current month as inactive.
                effective_end = min(month_end, date.today())
                tracking_start = date(2026, 9, 30)
                effective_start = max(month_start, tracking_start)
                days = [
                    effective_start + timedelta(days=i)
                    for i in range((effective_end - effective_start).days + 1)
                ] if effective_end >= effective_start else []

                daily_totals = [(day, get_day_seconds(day)) for day in days]
                total = sum(seconds for _, seconds in daily_totals)
                active_days = sum(1 for _, seconds in daily_totals if seconds > 0)
                inactive_days = len(daily_totals) - active_days
                active_rate = (
                    active_days / len(daily_totals) * 100
                    if daily_totals else 0
                )

                update_cards(
                    ("TOTAL TRACKED", "ACTIVE DAYS", "INACTIVE DAYS", "ACTIVE RATE"),
                    (
                        self.format_time(total),
                        str(active_days),
                        str(inactive_days),
                        f"{active_rate:.1f}%"
                    )
                )

                range_label.config(
                    text=(
                        f"{month_start.strftime('%B %Y')}  •  "
                        f"{len(daily_totals)} elapsed days  |  "
                        f"Days Worked: {active_days}  •  "
                        f"Days Without Activity: {inactive_days}"
                    )
                )
                draw_monthly_graph(month_start, daily_totals)

            else:
                days = selected_dates(period_var.get())
                daily_totals = [(day, get_day_seconds(day)) for day in days]
                total = sum(seconds for _, seconds in daily_totals)
                average = total / len(daily_totals) if daily_totals else 0
                active_days = sum(1 for _, seconds in daily_totals if seconds > 0)
                highest = max(daily_totals, key=lambda item: item[1]) if daily_totals else None

                highest_text = (
                    f"{highest[0].strftime('%b %d')} • {self.format_time(highest[1])}"
                    if highest and highest[1] > 0 else "No activity yet"
                )

                update_cards(
                    ("TOTAL TRACKED", "DAILY AVERAGE", "ACTIVE DAYS", "HIGHEST ACTIVITY DAY"),
                    (
                        self.format_time(total),
                        self.format_time(int(average)),
                        f"{active_days} / {len(daily_totals)} days",
                        highest_text
                    )
                )

                range_label.config(
                    text=f"{days[0].strftime('%b %d, %Y')} – {days[-1].strftime('%b %d, %Y')}"
                )
                draw_daily_graph([
                    (day, seconds)
                    for day, seconds in daily_totals
                    if seconds > 0
                ])

        def switch_mode(mode):
            mode_var.set(mode)

            if mode == "Monthly":
                period_box.pack_forget()
                month_box.pack(side="left")
                daily_button.config(bg="#DBEAFE", fg="#1E40AF")
                monthly_button.config(bg="#0F766E", fg="white")
            else:
                month_box.pack_forget()
                period_box.pack(side="left")
                monthly_button.config(bg="#CCFBF1", fg="#115E59")
                daily_button.config(bg="#1E40AF", fg="white")

            render_statistics()

        period_box.bind("<<ComboboxSelected>>", render_statistics)
        month_box.bind("<<ComboboxSelected>>", render_statistics)
        chart.bind("<Configure>", lambda event: render_statistics())

        daily_button.config(bg="#1E40AF", fg="white")
        monthly_button.config(bg="#CCFBF1", fg="#115E59")

        render_statistics()
        win.lift()
        win.focus_force()

    @staticmethod
    def format_time(seconds):
        seconds = max(0, int(seconds))
        h, rem = divmod(seconds, 3600)
        m = rem // 60
        return f"{h}h {m:02d}m" if h else f"{m}m"

    def draw_chart(self, totals):
        c = self.chart
        c.delete("all")
        width = max(300, c.winfo_width())
        height = max(200, c.winfo_height())
        if not totals:
            c.create_text(width / 2, height / 2, text="Your app activity will appear here.", fill="#98A2B3", font=("Helvetica", 11))
            return
        items = sorted(totals.items(), key=lambda x: x[1], reverse=True)[:7]
        maxv = max(v for _, v in items) or 1
        labelw = 112
        rightpad = 48
        chartw = max(80, width - labelw - rightpad)
        y = 20
        bar_h = 22
        rowh = min(38, (height - 35) / max(1, len(items)))
        bar_colors = ["#4786E8", "#14B8A6", "#8B5CF6", "#F59E0B", "#EC4899", "#06B6D4", "#84CC16"]
        for index, (app, val) in enumerate(items):
            c.create_text(
                4, y + bar_h / 2,
                text=app[:17],
                anchor="w", fill="#334155",
                font=("Helvetica", 9, "bold")
            )
            bw = chartw * val / maxv
            c.create_rectangle(
                labelw, y, labelw + bw, y + bar_h,
                fill=bar_colors[index % len(bar_colors)],
                outline="",
                width=0
            )
            c.create_text(
                labelw + bw + 5, y + bar_h / 2,
                text=self.format_time(val),
                anchor="w", fill="#334155",
                font=("Helvetica", 10, "bold")
            )
            y += rowh

    def export_csv(self):
        source = csv_path(self.view_day)
        if not source.exists():
            messagebox.showinfo("Nothing to export", f"No activity has been saved for {self.view_day.isoformat()} yet.")
            return
        target = filedialog.asksaveasfilename(
            defaultextension=".csv",
            initialfile=f"activity_{self.view_day.isoformat()}.csv",
            filetypes=[("CSV files", "*.csv")],
        )
        if target:
            import shutil
            shutil.copy2(source, target)
            messagebox.showinfo("Export complete", "Your CSV file has been exported.")

    def show_categories(self):
        win = tk.Toplevel(self.root)
        win.title("App categories")
        win.geometry("440x440")
        win.configure(bg="#F4F7FB")
        ttk.Label(win, text="Categorize your apps", font=("Helvetica", 15, "bold")).pack(anchor="w", padx=18, pady=(18, 5))
        ttk.Label(win, text="Choose a category for each app. New apps default to Other.", style="Sub.TLabel").pack(anchor="w", padx=18, pady=(0, 12))
        frame = ttk.Frame(win, padding=(18, 0))
        frame.pack(fill="both", expand=True)
        canvas = tk.Canvas(frame, bg="#F4F7FB", highlightthickness=0)
        sb = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        vars_ = {}
        apps = sorted(set(self.categories) | {r["app"] for r in read_day(self.view_day)})
        for app in apps:
            row = ttk.Frame(inner)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=app, width=22).pack(side="left")
            v = tk.StringVar(value=self.categories.get(app, "Other"))
            vars_[app] = v
            ttk.Combobox(row, textvariable=v, values=CATEGORIES, state="readonly", width=17).pack(side="right")

        def save():
            for a, v in vars_.items():
                self.categories[a] = v.get()
            CONFIG_FILE.write_text(json.dumps(self.categories, indent=2), encoding="utf-8")
            messagebox.showinfo("Saved", "App categories saved.")
            win.destroy()
            self.refresh()

        ttk.Button(win, text="Save categories", command=save).pack(anchor="e", padx=18, pady=16)

    def show_mail_tracker(self):
        config = load_mail_config()
        win = tk.Toplevel(self.root)
        win.title("Mail Tracker — Yahoo + Gmail")
        win.geometry("980x710")
        win.minsize(760, 560)
        win.configure(bg="#F4F7FB")

        top = ttk.Frame(win, padding=16)
        top.pack(fill="x")
        ttk.Label(top, text=f"Mail Tracker  •  v{APP_VERSION}", font=("Helvetica", 17, "bold")).grid(row=0, column=0, columnspan=8, sticky="w", pady=(0, 10))
        ttk.Label(top, text="Provider").grid(row=1, column=0, sticky="w")
        provider = tk.StringVar(value="Yahoo")
        provider_combo = ttk.Combobox(top, textvariable=provider, values=list(MAIL_PROVIDERS), state="readonly", width=10)
        provider_combo.grid(row=1, column=1, sticky="w", padx=(6, 14))
        ttk.Label(top, text="Email address").grid(row=1, column=2, sticky="w")
        email_var = tk.StringVar()
        email_entry = ttk.Entry(top, textvariable=email_var, width=28)
        email_entry.grid(row=1, column=3, sticky="w", padx=6)
        ttk.Label(top, text="Saved account").grid(row=1, column=6, sticky="w", padx=(8, 0))
        accounts = load_mail_accounts()
        account_var = tk.StringVar()
        account_combo = ttk.Combobox(top, textvariable=account_var, values=[f"{a['provider']} — {a['email']}" for a in accounts], state="readonly", width=26)
        account_combo.grid(row=1, column=7, sticky="w", padx=6)
        ttk.Label(top, text="Time range").grid(row=2, column=0, sticky="w", pady=(10, 0))
        range_var = tk.StringVar(value="Today")
        ttk.Combobox(top, textvariable=range_var, values=MAIL_RANGES, state="readonly", width=15).grid(row=2, column=1, sticky="w", padx=(6, 14), pady=(10, 0))
        ttk.Label(top, text="App password").grid(row=2, column=2, sticky="w", pady=(10, 0))
        password_var = tk.StringVar()
        password_entry = ttk.Entry(top, textvariable=password_var, show="•", width=28)
        password_entry.grid(row=2, column=3, sticky="w", padx=6, pady=(10, 0))
        ttk.Button(top, text="Scan mail", style="Primary.TButton", command=lambda: scan()).grid(row=1, column=4, rowspan=2, padx=(8, 0))
        ttk.Button(top, text="Save account", style="Accent.TButton", command=lambda: save_account()).grid(row=2, column=6, padx=(8, 0), pady=(10, 0))
        ttk.Button(top, text="Remove", style="Danger.TButton", command=lambda: remove_account()).grid(row=2, column=7, padx=6, pady=(10, 0))
        ttk.Button(top, text="Categories", command=lambda: manage_categories()).grid(row=3, column=0, padx=(0, 8), pady=(10, 0), sticky="w")
        ttk.Button(top, text="Screenshots ▾", command=lambda: self.show_screenshot_menu(win)).grid(row=3, column=1, padx=6, pady=(10, 0), sticky="w")
        ttk.Label(top, text="Account names are stored in JSON; passwords are stored in macOS Keychain.", style="Sub.TLabel").grid(row=3, column=2, columnspan=6, sticky="w", pady=(10, 0))

        summary_frame = ttk.Frame(win, padding=(16, 0, 16, 8))
        summary_frame.pack(fill="x")
        summary_label = ttk.Label(summary_frame, text="Choose an account, then scan. Email bodies are not downloaded.", style="Sub.TLabel")
        summary_label.pack(anchor="w")
        progress = ttk.Progressbar(summary_frame, mode="determinate", maximum=100)
        progress.pack(fill="x", pady=(6, 0))
        body = ttk.Frame(win, padding=(16, 0, 16, 12))
        body.pack(fill="both", expand=True)
        pane_left = ttk.Frame(body)
        pane_left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        ttk.Label(pane_left, text="EMAIL COUNT BY CATEGORY", font=("Helvetica", 10, "bold")).pack(anchor="w", pady=(4, 5))
        cat_tree = ttk.Treeview(pane_left, columns=("category", "count"), show="headings", height=12)
        cat_tree.heading("category", text="SENDER CATEGORY")
        cat_tree.heading("count", text="EMAILS")
        cat_tree.column("category", width=205, anchor="w")
        cat_tree.column("count", width=70, anchor="center")
        cat_tree.pack(fill="both", expand=True)
        pane_right = ttk.Frame(body)
        pane_right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        ttk.Label(pane_right, text="SENDER BREAKDOWN / MANUAL FIX", font=("Helvetica", 10, "bold")).pack(anchor="w", pady=(4, 5))
        sender_tree = ttk.Treeview(pane_right, columns=("sender", "category", "count"), show="headings", height=12)
        for col, label, width in [("sender", "SENDER", 210), ("category", "CATEGORY", 145), ("count", "COUNT", 55)]:
            sender_tree.heading(col, text=label)
            sender_tree.column(col, width=width, anchor="w")
        sender_tree.pack(fill="both", expand=True)
        mail_footer = ttk.Frame(win, padding=(16, 0, 16, 10))
        mail_footer.pack(fill="x")
        ttk.Label(mail_footer, text=f"Developer: {DEVELOPER_NAME}  •  Version {APP_VERSION}  •  Header-only scan", style="Sub.TLabel").pack(side="left")
        ttk.Button(mail_footer, text="Exit mail window", style="Danger.TButton", command=win.destroy).pack(side="right")
        editbar = ttk.Frame(pane_right)
        editbar.pack(fill="x", pady=(8, 0))
        chosen_category = tk.StringVar(value=(config["categories"] or ["Others"])[0])
        category_combo = ttk.Combobox(editbar, textvariable=chosen_category, values=config["categories"], state="readonly", width=20)
        category_combo.pack(side="left")
        ttk.Button(editbar, text="Reclassify selected sender", command=lambda: reclassify()).pack(side="left", padx=(6, 0))
        state = {"sender_rows": [], "last_scan": None}

        def account_changed(_event=None):
            label = account_var.get()
            for account in accounts:
                if label == f"{account['provider']} — {account['email']}":
                    provider.set(account["provider"])
                    email_var.set(account["email"])
                    try:
                        password_var.set(keychain_get_password(account["email"]))
                    except Exception:
                        password_var.set("")
                    break

        account_combo.bind("<<ComboboxSelected>>", account_changed)

        def save_account():
            address = email_var.get().strip()
            secret = password_var.get()
            selected_provider = provider.get()
            if not address or "@" not in address or not secret:
                messagebox.showwarning("Account details required", "Enter a valid email address and its provider app password before saving.", parent=win)
                return
            try:
                keychain_set_password(address, secret)
                accounts[:] = [a for a in accounts if a.get("email", "").lower() != address.lower()]
                accounts.append({"provider": selected_provider, "email": address})
                save_mail_accounts(accounts)
                labels = [f"{a['provider']} — {a['email']}" for a in accounts]
                account_combo.configure(values=labels)
                account_var.set(f"{selected_provider} — {address}")
                summary_label.config(text=f"Saved {address} in account list; password stored in macOS Keychain.")
            except Exception as exc:
                messagebox.showerror("Could not save account", f"Account was not saved.\n\n{exc}", parent=win)

        def remove_account():
            label = account_var.get()
            account = next((a for a in accounts if label == f"{a['provider']} — {a['email']}"), None)
            if not account:
                messagebox.showinfo("Select account", "Choose a saved account first.", parent=win)
                return
            if not messagebox.askyesno("Remove saved account", f"Remove {account['email']} from this app and delete its saved Keychain password?", parent=win):
                return
            try:
                keychain_delete_password(account["email"])
                accounts.remove(account)
                save_mail_accounts(accounts)
                account_combo.configure(values=[f"{a['provider']} — {a['email']}" for a in accounts])
                account_var.set("")
                email_var.set("")
                password_var.set("")
                summary_label.config(text="Saved account removed.")
            except Exception as exc:
                messagebox.showerror("Could not remove account", str(exc), parent=win)

        def update_provider_from_account():
            # Never infer the server from an email suffix; use the saved provider selection.
            selected = account_var.get()
            for account in accounts:
                if selected == f"{account['provider']} — {account['email']}":
                    provider.set(account["provider"])
                    return

        provider_combo.bind("<<ComboboxSelected>>", lambda _e: update_provider_from_account())

        def refresh_category_values():
            cats = config["categories"]
            category_combo.configure(values=cats)
            if chosen_category.get() not in cats and cats:
                chosen_category.set(cats[0])

        def manage_categories():
            dlg = tk.Toplevel(win)
            dlg.title("Manage mail categories")
            dlg.geometry("390x420")
            dlg.transient(win)
            frame = ttk.Frame(dlg, padding=14)
            frame.pack(fill="both", expand=True)
            ttk.Label(frame, text="Mail sender categories", font=("Helvetica", 13, "bold")).pack(anchor="w", pady=(0, 8))
            lb = tk.Listbox(frame, height=12, exportselection=False)
            lb.pack(fill="both", expand=True)
            def refill(select_name=None):
                lb.delete(0, "end")
                for cat in config["categories"]:
                    lb.insert("end", cat)
                if select_name in config["categories"]:
                    idx = config["categories"].index(select_name)
                    lb.selection_set(idx)
                    lb.activate(idx)
            entry = ttk.Entry(frame)
            entry.pack(fill="x", pady=8)
            def selected():
                picks = lb.curselection()
                return lb.get(picks[0]) if picks else None
            def add_cat():
                name = entry.get().strip()
                if not name:
                    messagebox.showwarning("Category required", "Enter a category name.", parent=dlg); return
                if name not in config["categories"]:
                    config["categories"].append(name)
                    save_mail_config(config)
                entry.delete(0, "end"); refill(name); refresh_category_values()
            def rename_cat():
                old, new = selected(), entry.get().strip()
                if not old or not new:
                    messagebox.showwarning("Select and rename", "Select a category and enter its new name.", parent=dlg); return
                if new != old and new in config["categories"]:
                    messagebox.showwarning("Already exists", "That category already exists.", parent=dlg); return
                config["categories"][config["categories"].index(old)] = new
                config["sender_rules"] = {k: (new if v == old else v) for k, v in config["sender_rules"].items()}
                save_mail_config(config); entry.delete(0, "end"); refill(new); refresh_category_values()
            def remove_cat():
                old = selected()
                if not old:
                    return
                if len(config["categories"]) <= 1:
                    messagebox.showwarning("Cannot remove", "At least one category is required.", parent=dlg); return
                if not messagebox.askyesno("Remove category", f"Remove '{old}'? Existing sender rules using it will be cleared.", parent=dlg): return
                config["categories"].remove(old)
                config["sender_rules"] = {k: v for k, v in config["sender_rules"].items() if v != old}
                save_mail_config(config); refill(); refresh_category_values()
            buttons = ttk.Frame(frame); buttons.pack(fill="x")
            ttk.Button(buttons, text="Add", command=add_cat).pack(side="left")
            ttk.Button(buttons, text="Rename selected", command=rename_cat).pack(side="left", padx=5)
            ttk.Button(buttons, text="Remove selected", command=remove_cat).pack(side="left")
            refill()

        def reclassify():
            selected = sender_tree.selection()
            if not selected:
                messagebox.showinfo("Select a sender", "Select a sender row first.", parent=win); return
            item = sender_tree.item(selected[0])
            sender = item["values"][0]
            category = chosen_category.get()
            if category not in config["categories"]:
                return
            addr = parseaddr(sender)[1].lower()
            domain = addr.rsplit("@", 1)[-1] if "@" in addr else ""
            config["sender_rules"][addr or sender.lower()] = category
            if domain:
                # Save by full address for precision; a domain rule can be added manually if desired.
                pass
            save_mail_config(config)
            for row in state["sender_rows"]:
                if row["sender"] == sender:
                    row["category"] = category
            render_rows(state["sender_rows"])
            summary_label.config(text="Sender category saved locally. Rescan to refresh totals.")

        def render_rows(sender_rows):
            for tree in (cat_tree, sender_tree):
                for item in tree.get_children(): tree.delete(item)
            counts = {c: 0 for c in config["categories"]}
            for row in sender_rows:
                counts[row["category"]] = counts.get(row["category"], 0) + row["count"]
                sender_tree.insert("", "end", values=(row["sender"], row["category"], row["count"]))
            for cat in config["categories"]:
                cat_tree.insert("", "end", values=(cat, counts.get(cat, 0)))
            cat_tree.insert("", "end", values=("TOTAL", sum(r["count"] for r in sender_rows)))
            refresh_category_values()

        scan_queue = queue.Queue()
        scan_in_progress = {"value": False}

        def scan():
            if scan_in_progress["value"]:
                return
            address = email_var.get().strip()
            password = password_var.get()
            selected_provider = provider.get()
            selected_range = range_var.get()
            if not address or not password:
                messagebox.showwarning("Credentials required", "Enter your email address and provider app password, or select a saved account.", parent=win)
                return
            if selected_provider not in MAIL_PROVIDERS:
                messagebox.showwarning("Choose provider", "Choose Yahoo or Gmail before scanning.", parent=win)
                return
            host, port = MAIL_PROVIDERS[selected_provider]
            start_day, end_day = mail_date_window(selected_range)
            since, before = start_day.strftime("%d-%b-%Y"), end_day.strftime("%d-%b-%Y")
            scan_in_progress["value"] = True
            progress.configure(mode="indeterminate", value=0)
            progress.start(12)
            summary_label.config(text=f"Connecting to {selected_provider}…")
            password_entry.configure(state="disabled")
            scan_thread = threading.Thread(target=scan_worker, args=(address, password, selected_provider, selected_range, host, port, since, before, start_day, end_day), daemon=True)
            scan_thread.start()
            win.after(100, poll_scan_queue)

        def scan_worker(address, password, selected_provider, selected_range, host, port, since, before, start_day, end_day):
            mail = None
            sender_counts = {}
            try:
                mail = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=25)
                mail.login(address, password)
                status, _ = mail.select("INBOX", readonly=True)
                if status != "OK":
                    raise RuntimeError("Could not open the inbox.")
                status, data = mail.search(None, "SINCE", since, "BEFORE", before)
                if status != "OK":
                    raise RuntimeError("Email search failed.")
                ids = data[0].split() if data and data[0] else []
                batches = [ids[i:i + 25] for i in range(0, len(ids), 25)]
                self._queue_mail_progress(scan_queue, 0, len(ids), "Email search complete")
                for batch_number, batch_ids in enumerate(batches, start=1):
                    message_set = ",".join(msg_id.decode("ascii") for msg_id in batch_ids)
                    status, fetched = mail.fetch(message_set, "(BODY.PEEK[HEADER.FIELDS (FROM DATE SUBJECT)])")
                    if status == "OK":
                        for part in fetched:
                            if not (isinstance(part, tuple) and isinstance(part[1], bytes)):
                                continue
                            msg = email.message_from_bytes(part[1])
                            sender = str(email.header.make_header(email.header.decode_header(msg.get("From", "Unknown sender"))))
                            subject = str(email.header.make_header(email.header.decode_header(msg.get("Subject", ""))))
                            try:
                                parsed = parsedate_to_datetime(msg.get("Date", ""))
                                if not (start_day <= parsed.date() < end_day):
                                    continue
                            except Exception:
                                pass
                            category = classify_mail_sender(sender, subject, config["sender_rules"], config["categories"])
                            key = sender or "Unknown sender"
                            if key not in sender_counts:
                                sender_counts[key] = {"sender": key, "category": category, "count": 0}
                            sender_counts[key]["count"] += 1
                    self._queue_mail_progress(scan_queue, min(batch_number * 25, len(ids)), len(ids), f"Fetched headers: {min(batch_number * 25, len(ids))} / {len(ids)}")
                rows = sorted(sender_counts.values(), key=lambda r: (-r["count"], r["sender"].lower()))
                scan_queue.put(("done", {"rows": rows, "provider": selected_provider, "range": selected_range, "total": sum(r["count"] for r in rows), "address": address}))
            except Exception as exc:
                scan_queue.put(("error", (selected_provider, str(exc))))
            finally:
                if mail is not None:
                    try: mail.close()
                    except Exception: pass
                    try: mail.logout()
                    except Exception: pass
                # Worker only returns its result; UI-thread poll clears the password field.

        def poll_scan_queue():
            try:
                while True:
                    kind, payload = scan_queue.get_nowait()
                    if kind == "progress":
                        done, total, message = payload
                        progress.stop()
                        progress.configure(mode="determinate", maximum=max(1, total), value=done)
                        summary_label.config(text=message if total else "No emails found in this date range.")
                    elif kind == "done":
                        scan_in_progress["value"] = False
                        progress.stop()
                        progress.configure(mode="determinate", maximum=100, value=100)
                        state["sender_rows"] = payload["rows"]
                        state["last_scan"] = payload["range"]
                        render_rows(state["sender_rows"])
                        summary_label.config(text=f"{payload['provider']} • {payload['range']} • {payload['total']} emails from {len(payload['rows'])} distinct senders")
                        password_var.set("")
                        password_entry.configure(state="normal")
                    elif kind == "error":
                        scan_in_progress["value"] = False
                        progress.stop()
                        progress.configure(mode="determinate", value=0)
                        password_var.set("")
                        password_entry.configure(state="normal")
                        prov, error = payload
                        summary_label.config(text="Scan failed. Check the selected provider and app password.")
                        messagebox.showerror("Mail scan failed", f"Could not scan {prov} mail.\n\n{error}\n\nCheck IMAP access and use a provider app password.", parent=win)
            except queue.Empty:
                pass
            if scan_in_progress["value"] and win.winfo_exists():
                win.after(100, poll_scan_queue)

        def _unused_placeholder():
            pass


    @staticmethod
    def _queue_mail_progress(target_queue, done, total, message):
        target_queue.put(("progress", (done, total, message)))

    def close(self):
        self._closing = True
        self.running = False
        for job in (self.after_id, self._clock_after, self._logo_after,
                    self._queue_after, self._weather_after,
                    self._ftp_after, self._ftp_blink_after):
            if job:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
        if self.after_id:
            try:
                self.root.after_cancel(self.after_id)
            except tk.TclError:
                pass
            self.after_id = None
        self.stop_session()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = Tracker(root)
    root.mainloop()
