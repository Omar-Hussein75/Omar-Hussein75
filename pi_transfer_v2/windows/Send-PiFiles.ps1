<#
.SYNOPSIS
    PI Mismatch sender - Windows jump machine side.

.DESCRIPTION
    One short, stateless pass:
      1. finds finished *.json files in the inbox (oldest first)
      2. sends each one to the RHEL server with the sftp.exe that ships
         with Windows (upload to a staging name, then atomic rename into
         the requests folder, so the server never sees a partial file)
      3. only after the server confirmed it, moves it to the Out folder
         (same behaviour and same name-clash pattern as the old script)
      4. uploads a small heartbeat file; the RHEL server raises the
         alerts if it stops arriving or reports problems

    Task Scheduler starts it every minute. Each run polls every
    PollSeconds for RunSeconds and then exits, so nothing long-running
    can hang, leak or silently die. A file that fails is simply retried
    by the next pass - it never leaves the inbox until it is sent.

    Nothing to install: Windows PowerShell 5.1 + the built-in OpenSSH
    client (Windows 10 1809+ / Server 2019+). No admin rights needed.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\Send-PiFiles.ps1 -Once -Verbose
#>
[CmdletBinding()]
param(
    # ---- existing folders (used exactly as before) -------------------
    [string]$InboxDir   = 'C:\PI_MISMATCH_TOOL',
    [string]$ArchiveDir = 'E:\SharePoint_PI_Mismatch_Tool\Out',

    # ---- RHEL server --------------------------------------------------
    [string]$RemoteHost = 'SFTP_HOST_OR_IP',
    [int]   $RemotePort = 22,
    [string]$RemoteUser = 'SFTP_USERNAME',
    # Existing folder: receives <name>.json only, as before.
    [string]$RemoteDir  = '/project/PI_MISMATCH_TOOL/requests',
    # NEW folders, created once on the server (same filesystem as RemoteDir).
    [string]$RemoteStagingDir   = '/project/PI_MISMATCH_TOOL_TRANSFER/incoming',
    [string]$RemoteHeartbeatDir = '/project/PI_MISMATCH_TOOL_TRANSFER/heartbeat',

    # ---- local (NEW, inside your own profile) -------------------------
    [string]$KeyFile = (Join-Path $env:USERPROFILE '.ssh\pi_transfer_ed25519'),
    [string]$WorkDir = (Join-Path $env:LOCALAPPDATA 'PI_Transfer'),
    [string]$SftpExe = 'sftp.exe',

    # ---- timing -------------------------------------------------------
    [int]$RunSeconds         = 50,   # Task Scheduler restarts us every 60 s
    [int]$PollSeconds        = 5,
    [int]$SettleSeconds      = 5,    # file untouched this long before it is sent
    [int]$SftpTimeoutSeconds = 120,  # a stuck transfer is killed after this
    [int]$LogRetentionDays   = 30,

    [switch]$Once                    # one pass (for testing), then exit
)

$ErrorActionPreference = 'Stop'
$ScriptVersion = '2.0'

$LogDir         = Join-Path $WorkDir 'logs'
$UploadDir      = Join-Path $WorkDir 'upload'             # snapshot copy that is sent
$NotArchivedDir = Join-Path $WorkDir 'sent_not_archived'  # sent, Out folder was unavailable
foreach ($d in @($LogDir, $UploadDir, $NotArchivedDir)) {
    if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}

# ======================================================================
# Logging
# ======================================================================

function Write-Log {
    param([string]$Level, [string]$Message)
    $line = '{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    $file = Join-Path $LogDir ('transfer-{0}.log' -f (Get-Date -Format 'yyyyMMdd'))
    try { Add-Content -LiteralPath $file -Value $line -Encoding UTF8 } catch { }
    Write-Verbose $line
}

# ======================================================================
# Single instance: an exclusive handle the OS releases if we die
# ======================================================================

try {
    $lockHandle = [System.IO.File]::Open((Join-Path $WorkDir 'sender.lock'),
        [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::None)
} catch {
    Write-Log 'INFO' 'Previous run still active; exiting.'
    exit 0
}

# ======================================================================
# sftp.exe wrapper: batch mode, strict host key, hard timeout
# ======================================================================

function Invoke-Sftp {
    param([string[]]$Commands)

    $batchFile = Join-Path $WorkDir 'batch.txt'
    [System.IO.File]::WriteAllLines($batchFile, $Commands)   # UTF-8, no BOM

    $sftpArgs = @(
        '-b', $batchFile,              # batch: stop + non-zero exit on first error
        '-i', $KeyFile,
        '-P', "$RemotePort",
        '-o', 'BatchMode=yes',         # never prompt (a prompt would hang forever)
        '-o', 'StrictHostKeyChecking=yes',
        '-o', 'IdentitiesOnly=yes',
        '-o', 'ConnectTimeout=20',
        '-o', 'ServerAliveInterval=15',  # detect a dead connection in ~45 s
        '-o', 'ServerAliveCountMax=3',
        "$RemoteUser@$RemoteHost"
    )

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $SftpExe
    $psi.Arguments = ($sftpArgs | ForEach-Object { '"' + $_ + '"' }) -join ' '
    $psi.WorkingDirectory = $UploadDir   # 'put' uses plain relative names
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true

    $proc = [System.Diagnostics.Process]::Start($psi)
    $stdout = $proc.StandardOutput.ReadToEndAsync()
    $stderr = $proc.StandardError.ReadToEndAsync()
    if (-not $proc.WaitForExit($SftpTimeoutSeconds * 1000)) {
        try { $proc.Kill() } catch { }
        throw "sftp timed out after $SftpTimeoutSeconds s"
    }
    $proc.WaitForExit()
    if ($proc.ExitCode -ne 0) {
        $msg = ($stderr.Result + ' ' + $stdout.Result).Trim() -replace '\s+', ' '
        throw "sftp exit code $($proc.ExitCode): $msg"
    }
}

function Send-OneFile {
    param([string]$LocalSnapshot, [string]$TargetDir, [string]$Name)
    # Upload to the staging folder, then rename into place. OpenSSH uses
    # the atomic posix-rename (overwrites an existing file of the same name).
    $part = "$RemoteStagingDir/$Name.part"
    Invoke-Sftp @(
        ('put "{0}" "{1}"' -f (Split-Path $LocalSnapshot -Leaf), $part),
        ('rename "{0}" "{1}/{2}"' -f $part, $TargetDir, $Name)
    )
}

# ======================================================================
# Inbox handling
# ======================================================================

$script:Seen = @{}   # name -> "size|mtime" at the previous poll

function Get-FileAgeSeconds([System.IO.FileInfo]$File) {
    # CreationTime matters too: a copied-in file keeps its old LastWriteTime.
    $newest = $File.LastWriteTimeUtc
    if ($File.CreationTimeUtc -gt $newest) { $newest = $File.CreationTimeUtc }
    return ((Get-Date).ToUniversalTime() - $newest).TotalSeconds
}

function Test-WriterFinished([string]$Path) {
    # Exclusive open fails while the producer (or anyone) still has the file
    # open. Nothing is written; content and timestamps stay as they are.
    try {
        $h = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read, [System.IO.FileShare]::None)
        $h.Close()
        return $true
    } catch { return $false }
}

function Get-ReadyFiles {
    if (-not (Test-Path -LiteralPath $InboxDir)) { throw "Inbox folder not found: $InboxDir" }
    $files = @(Get-ChildItem -LiteralPath $InboxDir -Filter '*.json' -File -ErrorAction Stop |
               Where-Object { $_.Extension -eq '.json' })
    $now = @{}
    $ready = New-Object System.Collections.ArrayList
    foreach ($f in $files) {
        $sig = '{0}|{1}' -f $f.Length, $f.LastWriteTimeUtc.Ticks
        $now[$f.Name] = $sig
        if ($script:Seen[$f.Name] -ne $sig) { continue }          # changed since last poll
        if ((Get-FileAgeSeconds $f) -lt $SettleSeconds) { continue }
        if (-not (Test-WriterFinished $f.FullName)) { continue }
        [void]$ready.Add($f)
    }
    $script:Seen = $now
    return @($ready | Sort-Object LastWriteTimeUtc)                 # oldest first
}

function Get-ArchiveDestination([string]$Dir, [System.IO.FileInfo]$File) {
    # Same name-clash pattern as the old script: <stem>_<yyyyMMdd_HHmmss>.json
    $dest = Join-Path $Dir $File.Name
    if (-not (Test-Path -LiteralPath $dest)) { return $dest }
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $dest = Join-Path $Dir ('{0}_{1}{2}' -f $File.BaseName, $stamp, $File.Extension)
    $i = 1
    while (Test-Path -LiteralPath $dest) {
        $dest = Join-Path $Dir ('{0}_{1}_{2}{3}' -f $File.BaseName, $stamp, $i, $File.Extension)
        $i++
    }
    return $dest
}

$script:Stats = @{ sent = 0; failed = 0; last_error = $null; archive_error = $null }

function Move-ToArchive([System.IO.FileInfo]$File) {
    try {
        if (-not (Test-Path -LiteralPath $ArchiveDir)) {
            New-Item -ItemType Directory -Path $ArchiveDir -Force | Out-Null
        }
        Move-Item -LiteralPath $File.FullName -Destination (Get-ArchiveDestination $ArchiveDir $File)
        return
    } catch {
        $script:Stats.archive_error = $_.Exception.Message
        Write-Log 'WARN' "Out folder unavailable ($($_.Exception.Message)); keeping $($File.Name) locally until it is back."
    }
    try {
        Move-Item -LiteralPath $File.FullName -Destination (Get-ArchiveDestination $NotArchivedDir $File)
    } catch {
        # Worst case the file is sent again next pass: a duplicate, never a loss.
        Write-Log 'ERROR' "Could not move sent file $($File.Name) out of the inbox: $($_.Exception.Message)"
    }
}

function Send-ReadyFiles {
    $consecutiveFailures = 0
    foreach ($f in (Get-ReadyFiles)) {
        $snapshot = Join-Path $UploadDir 'current.json'
        try {
            # Send a private snapshot: the file cannot change mid-upload, and
            # odd characters in the name never reach sftp's local path parser.
            Copy-Item -LiteralPath $f.FullName -Destination $snapshot -Force
            Send-OneFile -LocalSnapshot $snapshot -TargetDir $RemoteDir -Name $f.Name
        } catch {
            $script:Stats.failed++
            $script:Stats.last_error = "$($f.Name): $($_.Exception.Message)"
            Write-Log 'ERROR' "Send failed for $($f.Name) (will retry): $($_.Exception.Message)"
            $consecutiveFailures++
            if ($consecutiveFailures -ge 2) { return $false }   # server/network problem: stop this pass
            continue
        }
        $consecutiveFailures = 0
        $script:Stats.sent++
        Write-Log 'INFO' "SENT $($f.Name) ($($f.Length) bytes)"
        Move-ToArchive $f
    }
    return $true
}

function Move-NotArchived {
    $pending = @(Get-ChildItem -LiteralPath $NotArchivedDir -File -ErrorAction SilentlyContinue)
    if ($pending.Count -eq 0) { $script:Stats.archive_error = $null; return }
    try {
        if (-not (Test-Path -LiteralPath $ArchiveDir)) {
            New-Item -ItemType Directory -Path $ArchiveDir -Force | Out-Null
        }
        foreach ($f in $pending) {
            Move-Item -LiteralPath $f.FullName -Destination (Get-ArchiveDestination $ArchiveDir $f)
        }
        Write-Log 'INFO' "Out folder is back; archived $($pending.Count) file(s)."
        $script:Stats.archive_error = $null
    } catch {
        $script:Stats.archive_error = $_.Exception.Message
    }
}

function Send-Heartbeat {
    $inbox = @()
    if (Test-Path -LiteralPath $InboxDir) {
        $inbox = @(Get-ChildItem -LiteralPath $InboxDir -Filter '*.json' -File -ErrorAction SilentlyContinue |
                   Where-Object { $_.Extension -eq '.json' })
    }
    $oldest = 0
    foreach ($f in $inbox) { $a = Get-FileAgeSeconds $f; if ($a -gt $oldest) { $oldest = $a } }

    $hb = [ordered]@{
        host                     = $env:COMPUTERNAME
        user                     = $env:USERNAME
        script_version           = $ScriptVersion
        time_local               = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
        inbox_count              = $inbox.Count
        inbox_oldest_age_seconds = [int]$oldest
        sent_this_run            = $script:Stats.sent
        failed_this_run          = $script:Stats.failed
        last_error               = $script:Stats.last_error
        not_archived_count       = @(Get-ChildItem -LiteralPath $NotArchivedDir -File -ErrorAction SilentlyContinue).Count
        archive_error            = $script:Stats.archive_error
    }
    $hbFile = Join-Path $UploadDir 'heartbeat.json'
    [System.IO.File]::WriteAllText($hbFile, ($hb | ConvertTo-Json))
    $name = if ($env:COMPUTERNAME) { $env:COMPUTERNAME } else { 'sender' }
    try {
        Send-OneFile -LocalSnapshot $hbFile -TargetDir $RemoteHeartbeatDir -Name "$name.json"
    } catch {
        Write-Log 'WARN' "Heartbeat upload failed: $($_.Exception.Message)"
    }
}

function Remove-OldLogs {
    $limit = (Get-Date).AddDays(-$LogRetentionDays)
    Get-ChildItem -LiteralPath $LogDir -Filter 'transfer-*.log' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $limit } |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

# ======================================================================
# Main
# ======================================================================

$exitCode = 0
try {
    $deadline = (Get-Date).AddSeconds($RunSeconds)
    $passes = 0
    while ($true) {
        $passes++
        try {
            $ok = Send-ReadyFiles
            Move-NotArchived
            if (-not $ok) { break }   # server unreachable: next run (1 min) retries
        } catch {
            $script:Stats.failed++
            $script:Stats.last_error = $_.Exception.Message
            Write-Log 'ERROR' $_.Exception.Message
        }
        # -Once still needs two polls: a file counts as ready only when unchanged between them.
        if ($Once -and $passes -ge 2) { break }
        if ((Get-Date).AddSeconds($PollSeconds) -gt $deadline -and -not $Once) { break }
        Start-Sleep -Seconds ([Math]::Max($PollSeconds, $(if ($Once) { $SettleSeconds } else { 0 })))
    }
    Send-Heartbeat
    Remove-OldLogs
    if ($script:Stats.failed -gt 0) { $exitCode = 1 }
} catch {
    Write-Log 'ERROR' "Unexpected: $($_.Exception.Message)"
    $exitCode = 1
} finally {
    $lockHandle.Dispose()
}
exit $exitCode
