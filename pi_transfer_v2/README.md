# PI Mismatch Transfer, v2: thin Windows sender, smart RHEL server

Designed for these constraints: a **jump machine we don't own** (standard user, no admin, nothing to install) and a **RHEL server we fully control**.

## The idea

Keep the Windows side as dumb as possible and put everything else on RHEL.

```
 Windows jump machine (standard user, nothing installed)          RHEL server (ours)
 ─────────────────────────────────────────────────────           ──────────────────────────────────────
 SharePoint ─► C:\PI_MISMATCH_TOOL\*.json                         /project/PI_MISMATCH_TOOL_TRANSFER/
                    │                                                incoming/  name.json.part ──┐
 Task Scheduler ─► Send-PiFiles.ps1 (every minute, ~50 s, exits)     heartbeat/ <PC>.json         │ atomic
   built-in sftp.exe + SSH key ─────────────────────────────────►                                │ rename
                    │                                              /project/PI_MISMATCH_TOOL/     │
                    └─ after confirmed: move to E:\…\Out            requests/name.json ◄──────────┘
                                                                   systemd timer (5 min): pi-transfer-check
                                                                     ├─ heartbeat stale?  backlog?  errors?
                                                                     └─► email (from RHEL) / Teams / journal
```

| | Windows (jump machine) | RHEL (server) |
|---|---|---|
| Runs | `Send-PiFiles.ps1`: one ~50 s pass, started every minute by Task Scheduler | `pi-transfer-check`: every 5 min by a systemd timer |
| Needs | Windows PowerShell 5.1 and the **built-in** OpenSSH client (`sftp.exe`). No admin, no install, no Python. | python3 (standard library only) |
| State | none. The inbox is the queue | heartbeat files plus a small alert-state file |
| Job | send finished files, archive them, send a heartbeat | detect problems, alert, clean up |

## Why this is reliable

* **Nothing long-running on Windows.** The old always-on script could hang or die and stay that way. Now every run is short and independent: Task Scheduler starts a fresh one every minute, a stuck transfer is killed after 120 s, and a run that hangs anyway is ended by the task's 5-minute limit. There is nothing to "keep alive".
* **Nothing is lost.** A file stays in `C:\PI_MISMATCH_TOOL` until the server has confirmed it; `sftp -b` returns success only if both the upload and the rename succeed. After a crash, reboot, logoff or network outage, the next run just sends whatever is still there.
* **No partial files, either way.** A file is sent only when it has been unchanged between two checks, is at least 5 s old, and nobody else has it open. A private copy is what gets uploaded, so the file can't change mid-transfer. It is uploaded as `.part` into a separate staging folder and then **atomically renamed** into `requests/`. The consumer only ever sees complete files.
* **Retries are automatic and never stop.** A failed file stays in the inbox and is retried on the next run (every minute). If the server is unreachable, the run stops early instead of hammering it.
* **Monitoring happens from the side you own.** The Windows machine uploads a heartbeat every minute. RHEL alerts if it stops arriving, which covers every Windows-side failure at once: machine down, rebooted, user logged off, task disabled, password or key expired, network down. RHEL also alerts if files wait more than 15 min, or if the Out folder is unavailable. Alerts go out by **email from RHEL** (it can reach an SMTP relay even though the jump machine can't), Teams, and the journal.
* **Secure by default.** It uses an SSH key, not a password. On the server, the key is restricted to **SFTP only** (no shell, no forwarding), and optionally to the jump machine's IP address.

## Existing folders are untouched

| Location | Behaviour |
|---|---|
| `C:\PI_MISMATCH_TOOL` | Files are only read. They leave only after the server confirms them, so "gone" still means "sent". |
| `E:\SharePoint_PI_Mismatch_Tool\Out` | Receives each sent file. On a name clash, `<stem>_<yyyyMMdd_HHmmss>.json`, as before. |
| `/project/PI_MISMATCH_TOOL/requests` | Receives only complete `<name>.json` files. No temporary files. |

New locations: `/project/PI_MISMATCH_TOOL_TRANSFER/` on RHEL (it **must be on the same filesystem** as `requests/` for the rename), and `%LOCALAPPDATA%\PI_Transfer` (logs) plus `%USERPROFILE%\.ssh` (key) on Windows.

---

## Setup

### RHEL (as root)

1. Create the transfer folders, owned by the account that owns `requests/`:
   ```bash
   install -d -o raidprd -g raidprd /project/PI_MISMATCH_TOOL_TRANSFER/incoming /project/PI_MISMATCH_TOOL_TRANSFER/heartbeat
   ```
   Check that they're on the same filesystem as `requests/` (the two numbers must match):
   ```bash
   stat -c %d /project/PI_MISMATCH_TOOL/requests /project/PI_MISMATCH_TOOL_TRANSFER/incoming
   ```
2. Authorise the jump machine's key (you'll create it in the Windows section, step 2) **for SFTP only**. Add one line to `~raidprd/.ssh/authorized_keys`:
   ```
   restrict,command="internal-sftp -l INFO",from="JUMP_MACHINE_IP" ssh-ed25519 AAAA...key... pi-transfer
   ```
   `-l INFO` writes every upload and rename to `/var/log/secure` as a server-side audit trail. Remove `from="…"` if the jump machine's IP isn't fixed.
3. Install the watchdog:
   ```bash
   install -m 0755 pi-transfer-check /usr/local/bin/pi-transfer-check
   ```
   ```bash
   install -d /etc/pi-transfer && install -m 0640 -g raidprd check.conf.example /etc/pi-transfer/check.conf
   ```
   Edit `/etc/pi-transfer/check.conf`: set `EXPECTED_SENDERS` (the jump machine's computer name) and `SMTP_HOST` + `MAIL_TO` and/or `WEBHOOK_URL`. Then:
   ```bash
   cp pi-transfer-check.service pi-transfer-check.timer /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now pi-transfer-check.timer
   ```
4. **Change the old `raidprd` password.** It's in a public GitHub file.

### Windows (as your normal user)

1. Check the built-in SFTP client is there:
   ```bash
   where.exe sftp
   ```
   It should print `C:\Windows\System32\OpenSSH\sftp.exe`. If it doesn't, see the note at the end.
2. Create the key and send the `.pub` file to the RHEL admin (RHEL step 2):
   ```bash
   ssh-keygen -t ed25519 -N "" -C pi-transfer -f "%USERPROFILE%\.ssh\pi_transfer_ed25519"
   ```
3. Trust the server's host key once. Type `yes` after checking the fingerprint, then `bye`:
   ```bash
   sftp -i "%USERPROFILE%\.ssh\pi_transfer_ed25519" SFTP_USERNAME@SFTP_HOST
   ```
4. Copy `Send-PiFiles.ps1` and `Install-Task.ps1` to a folder in your profile, for example `%USERPROFILE%\PI_Transfer`. Edit the settings block at the top of `Send-PiFiles.ps1` (`RemoteHost`, `RemoteUser`; the paths are already set).
5. Test one pass, with a test file in the inbox:
   ```bash
   powershell -NoProfile -ExecutionPolicy Bypass -File "%USERPROFILE%\PI_Transfer\Send-PiFiles.ps1" -Once -Verbose
   ```
6. **Disable the old Python task** in Task Scheduler and end its `python.exe`.
7. Register the new task. Try the first command; if Windows refuses it for your account, use the second:
   ```bash
   powershell -NoProfile -ExecutionPolicy Bypass -File "%USERPROFILE%\PI_Transfer\Install-Task.ps1" -RunWhenLoggedOff
   ```
   ```bash
   powershell -NoProfile -ExecutionPolicy Bypass -File "%USERPROFILE%\PI_Transfer\Install-Task.ps1"
   ```
   The first runs even while you're logged off. That needs the "Log on as a batch job" right, which many standard users don't have. The second runs while you're logged on; if you log off, RHEL alerts within 10 minutes.

### Verify

* Within a minute, `/project/PI_MISMATCH_TOOL_TRANSFER/heartbeat/<PC>.json` is updated every minute.
* `journalctl -u pi-transfer-check` shows `Healthy.`
* Disable the task for 15 minutes: you should get a "No heartbeat" alert, and a RESOLVED message after you re-enable it.

## Operations

| Question | Where to look |
|---|---|
| Is it working? | The heartbeat file on RHEL, or `journalctl -u pi-transfer-check` |
| Was file X received? | `grep X /var/log/secure` on RHEL, and `ls` in `requests/` |
| Why did a send fail? | `%LOCALAPPDATA%\PI_Transfer\logs\transfer-YYYYMMDD.log` on Windows, or `last_error` in the heartbeat |
| Sent but not in `Out`? | `%LOCALAPPDATA%\PI_Transfer\sent_not_archived`. Moved to `Out` automatically when it's reachable. |
| Rollback | Disable the "PI Transfer" task and re-enable the old one. Unsent files are still in the inbox. |

## Notes and limits

* **Logged-off gap:** if the task can only run while you're logged on, nothing is sent while you're logged off. You'll know within 10 minutes, and nothing is lost: everything is sent at the next logon. Ask the machine owner whether your account can get "Log on as a batch job"; that's the only way to close the gap on this machine.
* **Window flash:** a task that runs "only when logged on" may flash a console window briefly each minute. Running it "whether logged on or not" avoids that.
* **No `sftp.exe`** (older Windows): Python and paramiko are already on this machine for the old script, so the same logic can be done in Python. Ask and I'll provide it.
* **Best long-term option, if it ever becomes possible:** let RHEL read the SharePoint list directly (Microsoft Graph API, with an Entra app registration and outbound access to `graph.microsoft.com`). Then the jump machine isn't needed at all.
