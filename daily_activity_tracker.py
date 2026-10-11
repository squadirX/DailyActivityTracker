#!/usr/bin/env python3
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

APP_NAME = "Daily Activity Tracker"
ROOT = Path.home() / "DailyActivityTracker"
DATA_DIR = ROOT / "data"
CONFIG_FILE = ROOT / "categories.json"
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
        try: style.theme_use("clam")
        except tk.TclError: pass
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
        header = ttk.Frame(outer); header.pack(fill="x")
        ttk.Label(header, text="Daily Activity Tracker", style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text="A simple, private view of how you spend your day on your Mac.", style="Sub.TLabel").pack(anchor="w", pady=(4, 12))

        daybar = ttk.Frame(outer); daybar.pack(fill="x", pady=(0, 10))
        ttk.Button(daybar, text="← Previous day", command=lambda: self.change_day(-1)).pack(side="left")
        self.day_label = ttk.Label(daybar, text="", style="Sub.TLabel")
        self.day_label.pack(side="left", padx=12)
        ttk.Button(daybar, text="Today", command=self.show_today).pack(side="left")
        ttk.Button(daybar, text="Next day →", command=lambda: self.change_day(1)).pack(side="left", padx=6)

        bar = ttk.Frame(outer); bar.pack(fill="x", pady=(0, 14))
        self.status = ttk.Label(bar, text="● Paused", foreground="#B45309")
        self.status.pack(side="left")
        self.active_label = ttk.Label(bar, text="No active app", style="Sub.TLabel")
        self.active_label.pack(side="left", padx=12)
        self.start_btn = ttk.Button(bar, text="▶  Start tracking", style="Primary.TButton", command=self.toggle)
        self.start_btn.pack(side="right")
        ttk.Button(bar, text="Export CSV", command=self.export_csv).pack(side="right", padx=8)
        ttk.Button(bar, text="Categories", command=self.show_categories).pack(side="right", padx=8)

        cards = ttk.Frame(outer); cards.pack(fill="x", pady=(0, 14))
        self.kpi_labels = {}
        for name, key in [("Tracked time","tracked"),("Apps used","apps"),("Intervals","sessions"),("Top app","top")]:
            card = tk.Frame(cards, bg="white", highlightbackground="#E4EAF2", highlightthickness=1)
            card.pack(side="left", fill="both", expand=True, padx=(0, 9))
            tk.Label(card, text=name.upper(), bg="white", fg="#7A8497", font=("Helvetica", 9, "bold")).pack(anchor="w", padx=14, pady=(12, 3))
            value = tk.Label(card, text="—", bg="white", fg="#172033", font=("Helvetica", 19, "bold"))
            value.pack(anchor="w", padx=14, pady=(0, 12))
            self.kpi_labels[key] = value

        content = ttk.Frame(outer); content.pack(fill="both", expand=True)
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
        for col, label, width in [("start","START",70),("app","APP",130),("category","CATEGORY",105),("duration","TIME",70)]:
            self.tree.heading(col, text=label); self.tree.column(col, width=width, anchor="w")
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
                self.root.after_cancel(self.after_id); self.after_id = None
            self.status.config(text="● Paused", foreground="#B45309")
            self.start_btn.config(text="▶  Resume tracking")
            self.active_label.config(text="Tracking paused")
            self.refresh()

    def tick(self):
        if not self.running: return
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
        seconds = max(0, int((end-start).total_seconds()))
        if seconds < 1: return
        app = self.active_app or "Unknown"
        category = self.categories.get(app, "Other")
        append_row({"date":start.date().isoformat(),"start_time":start.strftime("%H:%M:%S"),
                    "end_time":end.strftime("%H:%M:%S"),"duration_seconds":seconds,
                    "app":app,"category":category,"device":"Mac","activity":category})

    def refresh(self):
        rows = read_day(self.view_day)
        totals = defaultdict(int)
        for r in rows:
            try: totals[r["app"]] += int(float(r["duration_seconds"]))
            except Exception: pass
        # Include the currently running session as a live estimate.
        self.day_label.config(text=self.view_day.strftime("%A, %B %d, %Y"))
        self.activity_title.config(text=f"ACTIVITY — {self.view_day.strftime('%b %d, %Y').upper()}")
        if self.running and self.active_app and self.session_start and self.view_day == date.today() and self.session_start.date() == date.today():
            totals[self.active_app] += max(0, int((datetime.now()-self.session_start).total_seconds()))
        tracked = sum(totals.values())
        self.kpi_labels["tracked"].config(text=self.format_time(tracked))
        self.kpi_labels["apps"].config(text=str(len(totals)))
        self.kpi_labels["sessions"].config(text=str(len(rows) + (1 if self.active_app else 0)))
        top = max(totals, key=totals.get) if totals else "—"
        self.kpi_labels["top"].config(text=top if len(top)<13 else top[:12]+"…")
        self.draw_chart(totals)
        for item in self.tree.get_children(): self.tree.delete(item)
        for r in rows[-100:][::-1]:
            try: dur = self.format_time(int(float(r["duration_seconds"])))
            except Exception: dur = "—"
            self.tree.insert("", "end", values=(r.get("start_time","")[:5], r.get("app",""), r.get("category",""), dur))

    @staticmethod
    def format_time(seconds):
        seconds = max(0, int(seconds))
        h, rem = divmod(seconds, 3600); m = rem // 60
        return f"{h}h {m:02d}m" if h else f"{m}m"

    def draw_chart(self, totals):
        c = self.chart; c.delete("all")
        width = max(300, c.winfo_width()); height = max(200, c.winfo_height())
        if not totals:
            c.create_text(width/2, height/2, text="Your app activity will appear here.", fill="#98A2B3", font=("Helvetica", 11))
            return
        items = sorted(totals.items(), key=lambda x:x[1], reverse=True)[:7]
        maxv = max(v for _,v in items) or 1
        labelw = 112; rightpad = 48; chartw = max(80, width-labelw-rightpad)
        y = 20; rowh = min(31, (height-25)/max(1,len(items)))
        for app, val in items:
            c.create_text(4, y+8, text=app[:17], anchor="w", fill="#475467", font=("Helvetica", 9))
            bw = chartw * val/maxv
            c.create_rectangle(labelw, y, labelw+bw, y+15, fill="#4786E8", outline="")
            c.create_text(labelw+bw+5, y+7, text=self.format_time(val), anchor="w", fill="#667085", font=("Helvetica", 8))
            y += rowh

    def export_csv(self):
        source = csv_path(self.view_day)
        if not source.exists():
            messagebox.showinfo("Nothing to export", "No activity has been saved today yet."); return
        target = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=f"activity_{self.view_day.isoformat()}.csv", filetypes=[("CSV files","*.csv")])
        if target:
            import shutil; shutil.copy2(source, target)
            messagebox.showinfo("Export complete", "Your CSV file has been exported.")

    def show_categories(self):
        win = tk.Toplevel(self.root); win.title("App categories"); win.geometry("440x440"); win.configure(bg="#F4F7FB")
        ttk.Label(win, text="Categorize your apps", font=("Helvetica", 15, "bold")).pack(anchor="w", padx=18, pady=(18,5))
        ttk.Label(win, text="Choose a category for each app. New apps default to Other.", style="Sub.TLabel").pack(anchor="w", padx=18, pady=(0,12))
        frame = ttk.Frame(win, padding=(18,0)); frame.pack(fill="both", expand=True)
        canvas = tk.Canvas(frame, bg="#F4F7FB", highlightthickness=0); sb=ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        inner=ttk.Frame(canvas); inner.bind("<Configure>", lambda e:canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0,0),window=inner,anchor="nw"); canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left",fill="both",expand=True); sb.pack(side="right",fill="y")
        vars_={}
        apps=sorted(set(self.categories)|{r["app"] for r in read_day(self.view_day)})
        for app in apps:
            row=ttk.Frame(inner); row.pack(fill="x",pady=3)
            ttk.Label(row,text=app,width=22).pack(side="left")
            v=tk.StringVar(value=self.categories.get(app,"Other")); vars_[app]=v
            ttk.Combobox(row,textvariable=v,values=CATEGORIES,state="readonly",width=17).pack(side="right")
        def save():
            for a,v in vars_.items(): self.categories[a]=v.get()
            CONFIG_FILE.write_text(json.dumps(self.categories,indent=2),encoding="utf-8")
            messagebox.showinfo("Saved","App categories saved."); win.destroy(); self.refresh()
        ttk.Button(win,text="Save categories",command=save).pack(anchor="e",padx=18,pady=16)

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
