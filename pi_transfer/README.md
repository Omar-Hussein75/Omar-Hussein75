# PI Mismatch Transfer: reliable redesign

## Why the old script missed files

| # | Problem in the old script | What happens |
|---|---|---|
| 1 | `paramiko.Transport(...)` has **no connect, banner or I/O timeouts** and no keepalive | On a network drop, the script can **hang forever**. The process is still "running", so Task Scheduler never restarts it, and nothing is sent until someone notices. This is the most likely cause of the "stops working" reports. |
| 2 | Task Scheduler defaults: **"Stop the task if it runs longer than 3 days"**, "Run only when user is logged on", battery/idle conditions, and no restart on failure | The task is killed or never starts after a reboot or logoff, so files pile up silently. |
| 3 | It only ever processes the **newest** file (`max(mtime)`) | While new files keep arriving, older ones starve. A newest file that can't be processed (locked or bad) **blocks every other file**. |
| 4 | It picks up a file the producer may **still be writing**. `read(1)` doesn't prove the file is complete | A half-written JSON file is uploaded, the size check passes because it reads the size afterwards, and the file is archived. **Data loss.** |
| 5 | It uploads directly to the **final remote name** | The remote consumer can read a partially uploaded file. |
| 6 | Upload and archive are one step | If `E:\...\Out` is unavailable (a SharePoint sync lock, or a drive not mapped for that session), the move fails **after** a successful upload, and the file is re-uploaded every 5 s. |
| 7 | Output only goes to `print()`; SMTP doesn't work | There are no logs under Task Scheduler and no alerts. Failures are invisible until users complain. |
| 8 | Password hard-coded; host key never verified; nothing prevents two instances running | Security risk; two copies can race on the same file. |

## New architecture

**The existing folders behave exactly as before.** Whatever else is linked to them sees no difference:

| Existing location | Behaviour (unchanged from the old script) |
|---|---|
| `C:\PI_MISMATCH_TOOL` | Producers drop `*.json` here. Files are never renamed or modified, and they leave **only after they are confirmed on the remote**, so "gone from the inbox" still means "sent". |
| Remote `/project/PI_MISMATCH_TOOL/requests` | Receives `<name>.json`, nothing else. No temporary or extra files. |
| `E:\SharePoint_PI_Mismatch_Tool\Out` | Receives each sent file. On a name clash: `<stem>_<YYYYmmdd_HHMMSS>.json`, as before. |
| `E:\SharePoint_PI_Mismatch_Tool` | Not touched. |

Everything new lives only in **new** folders: `C:\PI_Transfer` (the code) and `C:\PI_MISMATCH_TOOL_STATE` (logs, status, archive backlog).

```
                 ┌─────────────── Windows Service (WinSW) ────────────────┐
 producer ──► C:\PI_MISMATCH_TOOL ──upload + verify──► remote requests/
                 │      (inbox = the queue)    │
                 │                             └─ then move ──► E:\…\Out
                 │                                  (if Out is unavailable: wait in
                 │                                   STATE\uploaded, moved later)
                 │  STATE\status.json heartbeat · STATE\logs\ · sent-YYYY-MM.jsonl
                 └────────────────────────────────────────────────────────┘
                              ▲ reads the folders and the heartbeat directly
 Task Scheduler, every 5 min: healthcheck.py ──► Event Log / Teams webhook
                                              └─► restarts the service if it is hung or down
```

**Design principles**

* **The inbox is the queue.** A file stays in `C:\PI_MISMATCH_TOOL` until the remote confirms it. A crash, power loss or reboot at any point leaves unsent files where they were, and the next run sends them. There is no in-memory state to lose.
* **A file is only sent when its writer is finished**: its size and mtime are unchanged across two scans, it hasn't been touched for `settle_seconds`, and no other process has it open for writing. The check doesn't modify the file.
* **Every upload is verified** by comparing the remote size with the local size (SHA-256 optional), before the file leaves the inbox.
* **Retries never stop.** Each file has its own exponential backoff (5 s → 10 s → … → 5 min cap). A connection failure triggers a global backoff instead of hammering the server. Files are sent oldest first, and one bad file never blocks the others.
* **Invalid JSON is not sent and not moved.** It stays in the inbox untouched, is re-checked automatically, and raises an alert. Set `validate_json = false` to send everything, like the old script.
* **An unavailable `Out` folder never blocks or repeats uploads.** The file waits in `STATE\uploaded` and is moved to `Out` when the folder is back.
* **Self-healing in three layers:**
  1. Connect and I/O timeouts plus SSH keepalive, so calls fail instead of hanging.
  2. An in-process hang watchdog: if the main loop makes no progress for 15 min, the process exits.
  3. The Windows Service Manager restarts it on any exit, starts it at boot, and needs no logged-on user. On top of that, the independent healthcheck restarts the service if the heartbeat goes stale.
* **Monitoring without SMTP:** the Windows Event Log (works with any monitoring agent: SCOM, Zabbix, Splunk, etc.) and a Teams Workflows webhook (or any JSON webhook). Optionally, `HEALTH.txt` can be written to a **new** folder of your choice. Alerts are de-duplicated (on start, every hour while the problem lasts, and a "RESOLVED" message when it clears).
* **Audit trail:** `STATE\logs\sent-YYYY-MM.jsonl` records every confirmed transfer (time, size, SHA-256, remote path).
* **Optional atomic remote delivery:** if the server admin creates a **new** folder such as `/project/PI_MISMATCH_TOOL/staging`, set `remote_staging_dir`. Files are then uploaded there and renamed into `requests/`, so the remote consumer can never read a half-uploaded file. This is off by default, so `requests/` behaves exactly as today.

**Why a Windows service instead of a scheduled task?** Task Scheduler is a scheduler, not a supervisor: it doesn't notice a hung process, and its defaults kill long-running tasks after 3 days. A service gets boot start, restart on failure and no dependency on a logged-on user, which is what keeps it running 24/7. Task Scheduler is still the right tool for the *watchdog*, because that job is short and periodic.

## Deployment (Windows)

1. **Install Python 3.9+ for all users** (for example `C:\Program Files\Python312`), then:
   ```
   python -m pip install -r requirements.txt
   ```
2. **Copy this folder** to `C:\PI_Transfer`. Copy `config.example.ini` to `config.ini` and fill in the host, username and paths.
3. **Credentials** (pick one):
   * **Recommended:** SSH key auth. Run `ssh-keygen -t ed25519 -f C:\PI_Transfer\keys\id_ed25519`, add the `.pub` file to `~<sftp-user>/.ssh/authorized_keys` on the server, restrict the key file's ACL to the service account, and set `key_file`.
   * Password in Windows Credential Manager. Run this **as the service account**: `python -m keyring set pi_mismatch_sftp <username>`.
   * Or the `PI_SFTP_PASSWORD` system environment variable (the weakest option).
4. **Trust the server's host key** once, and check the fingerprint with the server admin:
   ```
   python transfer_service.py --trust-host-key
   ```
5. **Test one pass** in the foreground:
   ```
   python transfer_service.py --once --verbose
   ```
6. **Install the service** with [WinSW](https://github.com/winsw/winsw/releases). Download `WinSW-x64.exe`, rename it to `PITransferService.exe` next to `PITransferService.xml`, fix the Python path in the XML, then:
   ```
   PITransferService.exe install
   PITransferService.exe start
   ```
   Set the service to run as a dedicated account that can access the folders and the archive share.
7. **Install the watchdog** (in an elevated PowerShell):
   ```
   .\install_healthcheck_task.ps1
   ```
8. **Alerts:** in Teams, open the channel → **Workflows** → "Post to a channel when a webhook request is received", and put the URL in `webhook_url`. Whether or not you use Teams, point your existing Windows monitoring at Application Event Log source `PI_Mismatch_Transfer`, IDs 900 (error) and 800 (warning).
9. **Remove the old Task Scheduler job.**

> **E: drive note:** if `E:` is a *mapped network drive*, a service won't see it. Use the UNC path (`\\server\share\...`) instead. If it is a OneDrive/SharePoint *sync* folder, the sync client only runs while that user is logged on. The service still uploads normally; only archiving waits.

## Operations

| Task | How |
|---|---|
| Is it healthy? | `C:\PI_MISMATCH_TOOL_STATE\status.json` (or `HEALTH.txt`, if `publish_dir` is set) |
| Was file X sent? | Search `C:\PI_MISMATCH_TOOL_STATE\logs\sent-*.jsonl` |
| Logs | `C:\PI_MISMATCH_TOOL_STATE\logs\transfer.log` (rotated daily, kept 60 days) |
| An "invalid JSON" alert | Fix or replace the file in `C:\PI_MISMATCH_TOOL`; it is picked up automatically |
| Stop / start | `PITransferService.exe stop` / `start` (or `services.msc`) |
| Roll back | Stop the service and re-enable the old task. Unsent files are still in the inbox. Move any files in `STATE\uploaded` to `Out` by hand. |
