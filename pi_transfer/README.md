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

```
                   ┌──────────── Windows Service (WinSW) ─────────────┐
 producer ──► inbox ─claim─► processing ─upload─► uploaded ─archive─► archive (E:\…\Out)
  (*.json)   C:\PI_MISMATCH_TOOL │                    │
                                 └─invalid JSON─► failed (quarantine)
                   │ status.json heartbeat, logs, sent-YYYY-MM.jsonl audit journal   │
                   └──────────────────────────────────────────────────────────────────┘
                                 ▲ reads folders + heartbeat directly
   Task Scheduler, every 5 min:  healthcheck.py ──► Event Log / Teams webhook / HEALTH.txt
                                                └─► restarts the service if the heartbeat is stale
```

**Design principles**

* **The file system is the queue.** Each file is always in exactly one folder. Moves between `inbox`, `processing` and `uploaded` are same-drive renames, which are atomic. A crash, power loss or reboot at any point leaves every file in a folder that the next run picks up. There is no in-memory state to lose.
* **A file is only claimed when its writer is finished**: its size and mtime are unchanged across two scans, it hasn't been touched for `settle_seconds`, and the rename succeeds (Windows refuses the rename while the producer has the file open).
* **Remote writes are atomic**: the file is uploaded as `name.json.partial`, its size is checked (optionally its SHA-256 too), and it is then renamed to `name.json`. The remote side never sees a partial file.
* **Retries never stop.** Each file has its own exponential backoff (5 s → 10 s → … → 5 min cap). A connection failure triggers a global backoff instead of hammering the server. Files are sent oldest first, and one bad file never blocks the others.
* **Permanent errors are quarantined, not retried forever**: a file that is still invalid JSON after a grace period goes to `failed\` with a `.reason.txt`, and an alert is raised.
* **Archiving is decoupled.** An unavailable archive drive never stops or repeats uploads; files wait in `uploaded\` and are moved when the drive comes back.
* **Self-healing in three layers:**
  1. Socket, connect and I/O timeouts plus SSH keepalive, so calls fail instead of hanging.
  2. An in-process hang watchdog: if the main loop makes no progress for 15 min, the process exits.
  3. The Windows Service Manager (WinSW) restarts it on any exit, starts it at boot, and needs no logged-on user. On top of that, the independent healthcheck restarts the service if the heartbeat goes stale.
* **Monitoring without SMTP:** the Windows Event Log (works with any monitoring agent: SCOM, Zabbix, Splunk, etc.), a Teams Workflows webhook (or any JSON webhook), and `HEALTH.txt` / `health.json` in the SharePoint folder so the team can see the status themselves. Alerts are de-duplicated (on start, every hour while the problem lasts, and a "RESOLVED" message when it clears).
* **Audit trail:** `logs\sent-YYYY-MM.jsonl` records every confirmed transfer (time, size, SHA-256, remote path). It answers "was my file sent?" in seconds.

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
| Is it healthy? | `HEALTH.txt` in the SharePoint folder, or `C:\PI_MISMATCH_TOOL_STATE\status.json` |
| Was file X sent? | Search `C:\PI_MISMATCH_TOOL_STATE\logs\sent-*.jsonl` |
| Logs | `C:\PI_MISMATCH_TOOL_STATE\logs\transfer.log` (rotated daily, kept 60 days) |
| A file was quarantined | Read `failed\<file>.reason.txt`, fix the file, then run `python transfer_service.py --requeue-failed` |
| Stop / start | `PITransferService.exe stop` / `start` (or `services.msc`) |

## Remote-side note

The consumer on the SFTP server should ignore `*.partial` files. (It already only sees complete `*.json` files, because the rename is atomic.)
