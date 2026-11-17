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
import os
import threading
import queue
from email.utils import parseaddr, parsedate_to_datetime

APP_NAME = "Daily Activity Tracker"
ROOT = Path.home() / "DailyActivityTracker"
DATA_DIR = ROOT / "data"
CONFIG_FILE = ROOT / "categories.json"
MAIL_CONFIG_FILE = ROOT / "mail_tracker.json"
MAIL_ACCOUNTS_FILE = ROOT / "mail_accounts.json"
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
    """Load explicitly user-approved saved mail credentials from a local JSON file."""
    try:
        data = json.loads(MAIL_ACCOUNTS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_mail_accounts(accounts):
    """Save local account JSON with owner-only file permissions where supported."""
    ROOT.mkdir(parents=True, exist_ok=True)
    MAIL_ACCOUNTS_FILE.write_text(json.dumps(accounts, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(MAIL_ACCOUNTS_FILE, 0o600)
    except OSError:
        pass


def mail_account_key(provider, address):
    return f"{provider}|{address.strip().lower()}"


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
        self.root.title(APP_NAME)
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
        self._style()
        self._build()
        self.refresh()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#F4F7FB")
        style.configure("TLabel", background="#F4F7FB", foreground="#172033", font=("Helvetica", 11))
        style.configure("Title.TLabel", font=("Helvetica", 22, "bold"), foreground="#14213D")
        style.configure("Sub.TLabel", font=("Helvetica", 10), foreground="#667085")
        style.configure("Kpi.TLabel", font=("Helvetica", 21, "bold"), foreground="#14213D")
        style.configure("KpiName.TLabel", font=("Helvetica", 9), foreground="#667085")
        style.configure("Treeview", rowheight=27, font=("Helvetica", 10), background="white", fieldbackground="white")
        style.configure("Treeview.Heading", font=("Helvetica", 10, "bold"))
        style.configure("Primary.TButton", font=("Helvetica", 10, "bold"), padding=(14, 8))
        style.configure("TButton", padding=(10, 7))

    def _build(self):
        outer = ttk.Frame(self.root, padding=22)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="Daily Activity Tracker", style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text="A simple, private view of how you spend your day on your Mac.", style="Sub.TLabel").pack(anchor="w", pady=(4, 12))

        daybar = ttk.Frame(outer)
        daybar.pack(fill="x", pady=(0, 10))
        ttk.Button(daybar, text="← Previous day", command=lambda: self.change_day(-1)).pack(side="left")
        self.day_label = ttk.Label(daybar, text="", style="Sub.TLabel")
        self.day_label.pack(side="left", padx=12)
        ttk.Button(daybar, text="Today", command=self.show_today).pack(side="left")
        ttk.Button(daybar, text="Next day →", command=lambda: self.change_day(1)).pack(side="left", padx=6)

        bar = ttk.Frame(outer)
        bar.pack(fill="x", pady=(0, 14))
        self.status = ttk.Label(bar, text="● Paused", foreground="#B45309")
        self.status.pack(side="left")
        self.active_label = ttk.Label(bar, text="No active app", style="Sub.TLabel")
        self.active_label.pack(side="left", padx=12)
        self.start_btn = ttk.Button(bar, text="▶  Start tracking", style="Primary.TButton", command=self.toggle)
        self.start_btn.pack(side="right")
        ttk.Button(bar, text="Export CSV", command=self.export_csv).pack(side="right", padx=8)
        ttk.Button(bar, text="Categories", command=self.show_categories).pack(side="right", padx=8)
        ttk.Button(bar, text="✉ Mail Tracker", command=self.show_mail_tracker).pack(side="right", padx=8)

        cards = ttk.Frame(outer)
        cards.pack(fill="x", pady=(0, 14))
        self.kpi_labels = {}
        for name, key in [("Tracked time", "tracked"), ("Apps used", "apps"), ("Intervals", "sessions"), ("Top app", "top")]:
            card = tk.Frame(cards, bg="white", highlightbackground="#E4EAF2", highlightthickness=1)
            card.pack(side="left", fill="both", expand=True, padx=(0, 9))
            tk.Label(card, text=name.upper(), bg="white", fg="#7A8497", font=("Helvetica", 9, "bold")).pack(anchor="w", padx=14, pady=(12, 3))
            value = tk.Label(card, text="—", bg="white", fg="#172033", font=("Helvetica", 19, "bold"))
            value.pack(anchor="w", padx=14, pady=(0, 12))
            self.kpi_labels[key] = value

        content = ttk.Frame(outer)
        content.pack(fill="both", expand=True)
        left = tk.Frame(content, bg="white", highlightbackground="#E4EAF2", highlightthickness=1)
        left.pack(side="left", fill="both", expand=True, padx=(0, 10))
        tk.Label(left, text="TIME BY APP", bg="white", fg="#344054", font=("Helvetica", 10, "bold")).pack(anchor="w", padx=14, pady=(13, 5))
        self.chart = tk.Canvas(left, bg="white", height=240, highlightthickness=0)
        self.chart.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        right = tk.Frame(content, bg="white", highlightbackground="#E4EAF2", highlightthickness=1)
        right.pack(side="left", fill="both", expand=True)
        self.activity_title = tk.Label(right, text="ACTIVITY", bg="white", fg="#344054", font=("Helvetica", 10, "bold"))
        self.activity_title.pack(anchor="w", padx=14, pady=(13, 5))
        cols = ("start", "app", "category", "duration")
        self.tree = ttk.Treeview(right, columns=cols, show="headings", height=10)
        for col, label, width in [("start", "START", 70), ("app", "APP", 130), ("category", "CATEGORY", 105), ("duration", "TIME", 70)]:
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=10, pady=(0, 12))
        footer = ttk.Label(outer, text="Local only • App names and time intervals • No keystrokes, screenshots or message contents", style="Sub.TLabel")
        footer.pack(anchor="w", pady=(12, 0))

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
            self.status.config(text="● Tracking", foreground="#15803D")
            self.start_btn.config(text="Ⅱ  Pause tracking")
            self.last_error = ""
            self.tick()
        else:
            self.stop_session()
            self.running = False
            if self.after_id:
                self.root.after_cancel(self.after_id)
                self.after_id = None
            self.status.config(text="● Paused", foreground="#B45309")
            self.start_btn.config(text="▶  Resume tracking")
            self.active_label.config(text="Tracking paused")
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
            self.active_label.config(text=f"Active: {app}")
            self.last_error = ""
        except Exception as e:
            self.stop_session()
            self.active_app = None
            self.active_label.config(text="Permission needed — see setup guide")
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
        self.kpi_labels["sessions"].config(text=str(len(rows) + (1 if live_today else 0)))
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
            self.tree.insert("", "end", values=(r.get("start_time", "")[:5], r.get("app", ""), r.get("category", ""), dur))

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
        rowh = min(31, (height - 25) / max(1, len(items)))
        for app, val in items:
            c.create_text(4, y + 8, text=app[:17], anchor="w", fill="#475467", font=("Helvetica", 9))
            bw = chartw * val / maxv
            c.create_rectangle(labelw, y, labelw + bw, y + 15, fill="#4786E8", outline="")
            c.create_text(labelw + bw + 5, y + 7, text=self.format_time(val), anchor="w", fill="#667085", font=("Helvetica", 8))
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
        accounts = load_mail_accounts()
        win = tk.Toplevel(self.root)
        win.title("Mail Tracker — Yahoo + Gmail")
        win.geometry("900x710")
        win.minsize(760, 580)
        win.configure(bg="#F4F7FB")

        top = ttk.Frame(win, padding=16)
        top.pack(fill="x")
        ttk.Label(top, text="Mail Tracker", font=("Helvetica", 17, "bold")).grid(row=0, column=0, columnspan=7, sticky="w", pady=(0, 10))
        ttk.Label(top, text="Saved account").grid(row=1, column=0, sticky="w")
        account_var = tk.StringVar(value="")
        account_combo = ttk.Combobox(top, textvariable=account_var, state="readonly", width=34)
        account_combo.grid(row=1, column=1, columnspan=3, sticky="w", padx=(6, 14))
        ttk.Button(top, text="Remove saved account", command=lambda: remove_saved_account()).grid(row=1, column=4, columnspan=2, padx=(6, 0), sticky="w")
        ttk.Label(top, text="Provider").grid(row=2, column=0, sticky="w", pady=(10, 0))
        provider = tk.StringVar(value="Yahoo")
        ttk.Combobox(top, textvariable=provider, values=list(MAIL_PROVIDERS), state="readonly", width=10).grid(row=2, column=1, sticky="w", padx=(6, 14), pady=(10, 0))
        ttk.Label(top, text="Email address").grid(row=2, column=2, sticky="w", pady=(10, 0))
        email_var = tk.StringVar()
        ttk.Entry(top, textvariable=email_var, width=28).grid(row=2, column=3, sticky="w", padx=6, pady=(10, 0))
        ttk.Label(top, text="Time range").grid(row=3, column=0, sticky="w", pady=(10, 0))
        range_var = tk.StringVar(value="Today")
        ttk.Combobox(top, textvariable=range_var, values=MAIL_RANGES, state="readonly", width=15).grid(row=3, column=1, sticky="w", padx=(6, 14), pady=(10, 0))
        ttk.Label(top, text="App password").grid(row=3, column=2, sticky="w", pady=(10, 0))
        password_var = tk.StringVar()
        ttk.Entry(top, textvariable=password_var, show="•", width=28).grid(row=3, column=3, sticky="w", padx=6, pady=(10, 0))
        scan_button = ttk.Button(top, text="Scan mail", style="Primary.TButton")
        scan_button.grid(row=2, column=4, rowspan=2, padx=(10, 0))
        ttk.Button(top, text="Manage categories", command=lambda: manage_categories()).grid(row=2, column=5, rowspan=2, padx=(8, 0))
        ttk.Label(top, text="Saved passwords are stored in a local JSON file only if you approve. The file is not encrypted.", style="Sub.TLabel").grid(row=4, column=0, columnspan=7, sticky="w", pady=(10, 0))

        summary_frame = ttk.Frame(win, padding=(16, 0, 16, 5))
        summary_frame.pack(fill="x")
        summary_label = ttk.Label(summary_frame, text="Choose an account or enter credentials, then scan.", style="Sub.TLabel")
        summary_label.pack(anchor="w")
        progress = ttk.Progressbar(summary_frame, mode="indeterminate", maximum=100)
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
        editbar = ttk.Frame(pane_right)
        editbar.pack(fill="x", pady=(8, 0))
        chosen_category = tk.StringVar(value=(config["categories"] or ["Others"])[0])
        category_combo = ttk.Combobox(editbar, textvariable=chosen_category, values=config["categories"], state="readonly", width=20)
        category_combo.pack(side="left")
        ttk.Button(editbar, text="Reclassify selected sender", command=lambda: reclassify()).pack(side="left", padx=(6, 0))
        ttk.Label(win, text="Email headers only • No message bodies are downloaded or displayed • Local summary", style="Sub.TLabel").pack(anchor="w", padx=16, pady=(0, 12))
        state = {"sender_rows": [], "last_scan": None, "busy": False, "events": queue.Queue()}

        def refresh_saved_accounts():
            labels = []
            for key, item in accounts.items():
                labels.append(f"{item.get('provider', 'Yahoo')} — {item.get('email', key)}")
            account_combo.configure(values=labels)
            if account_var.get() not in labels:
                account_var.set("")

        def selected_saved_key():
            label = account_var.get()
            for key, item in accounts.items():
                if label == f"{item.get('provider', 'Yahoo')} — {item.get('email', key)}":
                    return key
            return None

        def remove_saved_account():
            key = selected_saved_key()
            if not key:
                messagebox.showinfo("Select account", "Choose a saved account first.", parent=win)
                return
            if messagebox.askyesno("Remove saved account", "Remove this saved email and password from the local JSON file?", parent=win):
                accounts.pop(key, None)
                save_mail_accounts(accounts)
                refresh_saved_accounts()
                password_var.set("")
                summary_label.config(text="Saved account removed.")

        def on_saved_account_selected(_event=None):
            key = selected_saved_key()
            if not key:
                return
            item = accounts[key]
            provider.set(item.get("provider", "Yahoo"))
            email_var.set(item.get("email", ""))
            password_var.set(item.get("password", ""))
            # Choosing a saved account fills credentials and starts the scan automatically.
            win.after(150, scan)

        account_combo.bind("<<ComboboxSelected>>", on_saved_account_selected)
        refresh_saved_accounts()

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
            config["sender_rules"][addr or sender.lower()] = category
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

        def finish_scan(event):
            kind = event[0]
            if kind == "progress":
                summary_label.config(text=event[1])
                if event[2] is not None:
                    progress.stop()
                    progress.configure(mode="determinate", maximum=max(1, event[3]))
                    progress["value"] = event[2]
                return
            state["busy"] = False
            scan_button.configure(state="normal")
            progress.stop()
            if kind == "success":
                rows, provider_name, range_name = event[1], event[2], event[3]
                state["sender_rows"] = rows
                state["last_scan"] = range_name
                render_rows(rows)
                total = sum(r["count"] for r in rows)
                summary_label.config(text=f"{provider_name} • {range_name} • {total} emails from {len(rows)} distinct senders")
                password_var.set("")
            else:
                summary_label.config(text="Scan failed. The password was not saved unless you approved saving it.")
                messagebox.showerror("Mail scan failed", f"Could not scan {event[1]} mail.\n\n{event[2]}\n\nCheck IMAP access and use a provider app password.", parent=win)
                password_var.set("")

        def poll_events():
            if not win.winfo_exists():
                return
            try:
                while True:
                    event = state["events"].get_nowait()
                    finish_scan(event)
            except queue.Empty:
                pass
            if win.winfo_exists():
                win.after(100, poll_events)

        def scan():
            if state["busy"]:
                return
            address = email_var.get().strip()
            password = password_var.get()
            if not address or not password:
                messagebox.showwarning("Credentials required", "Enter your email address and provider app password.", parent=win); return
            provider_name = provider.get()
            if provider_name not in MAIL_PROVIDERS:
                messagebox.showwarning("Provider required", "Choose Yahoo or Gmail.", parent=win); return
            key = mail_account_key(provider_name, address)
            if key not in accounts:
                if messagebox.askyesno("Save email credentials?", "Save this email ID and app password in a local JSON file so you can select it next time?\n\nThe JSON file is not encrypted. Anyone with access to your Mac user account may be able to read it.", parent=win):
                    accounts[key] = {"provider": provider_name, "email": address, "password": password}
                    try:
                        save_mail_accounts(accounts)
                        refresh_saved_accounts()
                        account_var.set(f"{provider_name} — {address}")
                    except OSError as exc:
                        messagebox.showerror("Could not save account", f"The scan can continue, but credentials could not be saved.\n\n{exc}", parent=win)
            start_day, end_day = mail_date_window(range_var.get())
            since = start_day.strftime("%d-%b-%Y")
            before = end_day.strftime("%d-%b-%Y")
            host, port = MAIL_PROVIDERS[provider_name]
            selected_range = range_var.get()
            state["busy"] = True
            scan_button.configure(state="disabled")
            progress.configure(mode="indeterminate", maximum=100, value=0)
            progress.start(10)
            summary_label.config(text=f"Connecting to {provider_name} and searching {selected_range.lower()}…")
            # Copy all inputs before the worker starts; only the main thread updates Tk widgets.
            rules = dict(config["sender_rules"])
            categories = list(config["categories"])

            def worker():
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
                    total_batches = max(1, (len(ids) + 24) // 25)
                    state["events"].put(("progress", f"Found {len(ids)} matching messages; scanning headers…", 0, total_batches))
                    for batch_index, offset in enumerate(range(0, len(ids), 25), start=1):
                        batch_ids = ids[offset:offset + 25]
                        message_set = ",".join(msg_id.decode("ascii") for msg_id in batch_ids)
                        status, fetched = mail.fetch(message_set, "(BODY.PEEK[HEADER.FIELDS (FROM DATE SUBJECT)])")
                        if status != "OK":
                            state["events"].put(("progress", f"Fetching batch {batch_index}/{total_batches}; Yahoo/Gmail skipped a failed batch…", batch_index, total_batches))
                            continue
                        for part in fetched:
                            if not (isinstance(part, tuple) and isinstance(part[1], bytes)):
                                continue
                            raw = part[1]
                            msg = email.message_from_bytes(raw)
                            sender = str(email.header.make_header(email.header.decode_header(msg.get("From", "Unknown sender"))))
                            subject = str(email.header.make_header(email.header.decode_header(msg.get("Subject", ""))))
                            try:
                                parsed = parsedate_to_datetime(msg.get("Date", ""))
                                msg_day = parsed.date()
                                if not (start_day <= msg_day < end_day):
                                    continue
                            except Exception:
                                pass
                            category = classify_mail_sender(sender, subject, rules, categories)
                            key_sender = sender or "Unknown sender"
                            if key_sender not in sender_counts:
                                sender_counts[key_sender] = {"sender": key_sender, "category": category, "count": 0}
                            sender_counts[key_sender]["count"] += 1
                        state["events"].put(("progress", f"Scanning batch {batch_index}/{total_batches} • {min(offset + len(batch_ids), len(ids))} of {len(ids)} messages…", batch_index, total_batches))
                    rows = sorted(sender_counts.values(), key=lambda r: (-r["count"], r["sender"].lower()))
                    state["events"].put(("success", rows, provider_name, selected_range))
                except Exception as exc:
                    state["events"].put(("error", provider_name, str(exc)))
                finally:
                    if mail is not None:
                        try: mail.close()
                        except Exception: pass
                        try: mail.logout()
                        except Exception: pass

            threading.Thread(target=worker, daemon=True).start()

        scan_button.configure(command=scan)
        win.after(100, poll_events)

    def close(self):
        self.running = False
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
