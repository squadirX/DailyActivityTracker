# Daily Activity Tracker (macOS)

A small, local-first Python desktop app that records the **foreground application name** and the time spent in each app. It is designed to be simple and understandable.

## What it does
- Start / pause tracking with one button.
- Checks the active Mac application every 15 seconds.
- Saves app sessions to daily CSV files.
- Shows tracked time, app count, session count, top app, and a simple bar chart.
- Lets you assign categories and export today's CSV.
- Keeps data on your Mac in `~/DailyActivityTracker/data/`.

## What it does not do
- No keystrokes, screenshots, message contents, passwords, or browser page contents.
- Does not track iPhone/iPad or physical activities in this first version.
- Does not run invisibly or start automatically at login.

## Requirements
- macOS with Python 3 and Tkinter. Your Python installation must include Tkinter.
- No third-party Python packages required.

## Run
1. Unzip the project.
2. Double-click `launch.command`. If macOS blocks it, right-click > Open.
3. Or open Terminal and run:
   ```bash
   python3 daily_activity_tracker.py
   ```
4. Click **Start tracking**. Keep the tracker open while you want tracking to run.
5. Click **Pause tracking** when you want to stop.

## macOS permission
On first use, macOS may ask whether Terminal/Python can control **System Events**. Allow this for active-app detection. If app detection fails, open **System Settings → Privacy & Security → Automation** and enable System Events access for the app/terminal used to run Python. The exact wording varies by macOS version.

## Data
CSV files are created at:
`~/DailyActivityTracker/data/activity_YYYY-MM-DD.csv`

You can open them in Excel or Numbers. Category mappings are stored in:
`~/DailyActivityTracker/categories.json`

## Important accuracy note
The tracker samples the foreground app every 15 seconds. Durations are estimates, not forensic records. If the Mac sleeps, the app is closed, or tracking is paused, no activity is collected for that interval. Keep the app open and tracking enabled.

## Start at login (optional, later)
This starter version intentionally does not install a background service. Once the basic version works, login launch can be added as an explicit opt-in.
