# Smart Disk Cleaner — macOS

A local-only macOS storage inspection and carefully controlled cache-cleanup tool.

## Package contents

This package contains exactly four files:

1. `SmartDiskCleaner_index.html` — the full dashboard and Mac Drive Workspace.
2. `SmartDiskCleaner_Backend.py` — the local Python backend.
3. `README.md` — these instructions.
4. `history.json` — genuine verification history (starts empty).

## Requirements

- macOS
- Python 3 (standard library only; no pip packages are required)
- A modern browser

The backend binds only to `127.0.0.1:8790`. It is not intended to be exposed to the internet.

## Run on your Mac

1. Extract the ZIP package into your project folder, for example:
   `/Users/x/Documents/SmartDiskCleaner`
2. Open Terminal.
3. Run:

   ```bash
   cd "/Users/x/Documents/SmartDiskCleaner"
   python3 SmartDiskCleaner_Backend.py
   ```

4. The backend will start and open the dashboard in your browser.
5. Keep the Terminal window open while using the dashboard.
6. When finished, return to Terminal and press **Control+C** to stop the local server.

If port 8790 is already in use, the server reports the issue; do not start duplicate instances.

## Ten-stage workflow

1. Live system and hardware check.
2. macOS storage category breakdown.
3. Detailed storage analysis.
4. Running application and file-safety review.
5. Cleanup candidate review.
6. Explicitly approved cleanup execution.
7. Before/after disk-space verification.
8. Genuine verification history.
9. Full-page workspace navigation back to the dashboard.
10. Final package and runtime validation.

## Measurement notes

- Disk capacity and free space are read from `df -k /` for the mounted root volume. This is not identical to the macOS Storage Settings category report.
- Applications, Documents, Mail, Developer, Photos, Music, TV & Media and Trash are measured from the listed folders. The UI labels these as measured folder totals, not official macOS category totals.
- Official macOS and System Data category totals are shown as **Unavailable** when they cannot be reliably obtained. They are never guessed or inferred from overlapping folders.
- RAM, swap, memory pressure, macOS version and hardware details use native macOS commands. If a command is unavailable or denied, the field is marked unavailable.
- Running application detection is best-effort. Open-file ownership and unsaved-work state are explicitly marked **Unable to Verify**; save your work and close applications yourself.
- RAM is monitored, not “cleaned” by force-closing applications.

## Cleanup safety

- Nothing is deleted automatically.
- The user must select a supported candidate and confirm the action in the dashboard.
- Python pip cache cleanup uses pip's supported `python -m pip cache purge` command and is blocked if its reported cache directory is outside the user's home folder.
- Chrome must already be closed by the user. The backend never force-quits it.
- Chrome cleanup targets only individually checked directories named `Cache`, `Code Cache`, `GPUCache` or `CacheStorage` beneath the Chrome cache root. It does not remove the Chrome cache root, profile, bookmarks, history, extensions or application data.
- Documents, Desktop, Downloads, projects, application support folders, backups, personal files and system folders are not cleanup targets.
- A cleanup candidate's size is potential cache size, not guaranteed recovered disk space.

## Verification and history

After an approved cleanup, run Final Verification. It measures the root volume again and compares free space before and after. The measured free-space change is not necessarily equal to bytes deleted: APFS snapshots, purgeable space and background activity can affect the result.

Only completed verification results are appended to `history.json`. The file starts as an empty JSON array; no sample records are included.

## Important

The GitHub package checks validate required files and JSON structure. They cannot test your physical Mac, permissions, actual cache contents or real disk changes. Those checks must be completed on your Mac before treating the application as fully verified.
