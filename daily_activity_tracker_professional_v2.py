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
        style.configure("Primary.TButton", font=("Helvetica", 10, "bold"), padding=(14, 8), background="#2563EB", foreground="white")
        style.map("Primary.TButton", background=[("active", "#1D4ED8"), ("pressed", "#1E40AF")], foreground=[("active", "white")])
        style.configure("Accent.TButton", font=("Helvetica", 10, "bold"), padding=(11, 7), background="#0F766E", foreground="white")
        style.map("Accent.TButton", background=[("active", "#115E59"), ("pressed", "#134E4A")], foreground=[("active", "white")])
        style.configure("Danger.TButton", font=("Helvetica", 10, "bold"), padding=(11, 7), background="#DC2626", foreground="white")
        style.map("Danger.TButton", background=[("active", "#B91C1C"), ("pressed", "#991B1B")], foreground=[("active", "white")])
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
        ttk.Button(bar, text="Export CSV", style="Accent.TButton", command=self.export_csv).pack(side="right", padx=6)
        ttk.Button(bar, text="Categories", command=self.show_categories).pack(side="right", padx=6)
        ttk.Button(bar, text="✉ Mail Tracker", style="Accent.TButton", command=self.show_mail_tracker).pack(side="right", padx=6)
        ttk.Button(bar, text="Screenshots ▾", command=lambda: self.show_screenshot_menu(self.root, self.chart)).pack(side="right", padx=6)

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
        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(12, 0))
        ttk.Label(footer, text=f"Local only • Developer: {DEVELOPER_NAME} • Version {APP_VERSION}", style="Sub.TLabel").pack(side="left")
        ttk.Button(footer, text="Exit", style="Danger.TButton", command=self.close).pack(side="right")

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
