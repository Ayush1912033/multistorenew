# POS File Distributor — Multi-Store Edition

Windows Tkinter application for distributing a selected file or folder update through each store's BO to its Ubuntu counters.

For **Folder Update**, choose a local folder and enter an existing parent folder path on the counters. The app creates a child folder using the selected folder's name (for example, local `UpdateA` copied to parent `/srv/pos` becomes `/srv/pos/UpdateA`) and merges the selected folder contents into it. Existing files in that destination are backed up before replacement.

## Requirements

- Python 3.10+
- `paramiko` for SSH/SFTP
- `openpyxl` for reading `.xlsx` store lists
- SQLite is included with standard Python builds through `sqlite3`; no separate SQLite installation/server is needed.

Install dependencies:

```powershell
py -m pip install -r requirements.txt
```

Run:

```powershell
py main.py
```

Use the Python launcher (`py`) for both commands so the dependencies are installed
for the same Python interpreter that runs the application. For a specific Python
version, use that version in both commands (for example, `py -3.12 -m pip install
-r requirements.txt` and `py -3.12 main.py`).

## Excel columns

The first worksheet must have these exact column headers:

- `StoreCode`
- `BO_IP` — valid IPv4 address
- `CounterCount` — positive integer

Counter IPs are generated from the same first three octets as the BO, starting at `.112`. For example, BO `10.12.22.12` with four counters generates `10.12.22.112` through `10.12.22.115`.

## Local data and persistence

The application creates `data/pos_distributor.db` on the Windows PC. It stores store/counter statuses, error details, and event history. The database is local; no database server is required. `config/config.json` stores connection settings. Treat it as sensitive because it includes the entered SSH credentials.

The database and logs are created beside `main.py`. Keep the project folder in a writable location. When distributing as an EXE later, configure the data directory to a stable writable location outside a temporary extraction directory.

## Scheduling behavior

- Up to 100 store workers; configurable from the UI.
- Each store has its own BO and generated counter list.
- Each store gets one attempt per run. Failed/offline counters remain marked with their result; there are no automatic retries. Use **Retry Failed** to manually retry unsuccessful counters.
- A store is completed only when every configured counter is successful; otherwise it is marked `Failed` with the counter results.
- BO staging is cleaned after all counters succeed. If any counter fails, staging is retained for inspection and is not retried automatically.
- Interrupted `Processing` stores are marked `Failed` on next application launch and are not run again automatically.
- Reset clears the current store/counter queues and event history; loading a new Excel workbook replaces the current job.

## Important operational notes

1. Test first with one non-production store and a small worker limit.
2. The existing BO shell scripts require `sshpass`, `ssh`, `scp`, `timeout`, and `sha256sum` on the BO Linux server.
3. SSH passwords are stored in `config/config.json` as in the original application design. Restrict access to this file and the project folder.
4. This first multi-store build preserves the original BO controller's transfer implementation and adds persistent staging support. Validate folder and file updates in a test environment before production rollout.

## Resume after closing the app

Open the app, verify the saved SSH credentials are present, set the worker limit, and click **Resume Saved Job** to start stores that are still `Pending`. Stores marked `Failed` are not attempted again. Click **Start / Load Excel** to create a new job.

A sample workbook is included at `templates/stores_template.xlsx`.
