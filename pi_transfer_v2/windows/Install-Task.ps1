<#
.SYNOPSIS
    Registers the "PI Transfer" scheduled task for the CURRENT user.
    No administrator rights needed.

.DESCRIPTION
    Runs Send-PiFiles.ps1 every minute, forever, plus once at logon.
    Fixes the Task Scheduler defaults that silently stop long-running jobs:
      - no 3-day execution limit (each run lasts ~1 minute; limit 5 min)
      - no new instance while one is running
      - runs on battery, catches up after missed starts, restarts on failure

    -RunWhenLoggedOff asks for your password so the task also runs when you
    are logged off. That needs the "Log on as a batch job" right, which
    standard users often lack; if Windows refuses, the script tells you and
    you can re-run it without the switch (then it runs while you are logged on).

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\Install-Task.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File .\Install-Task.ps1 -RunWhenLoggedOff
#>
param(
    [string]$ScriptPath = (Join-Path $PSScriptRoot 'Send-PiFiles.ps1'),
    [string]$TaskName = 'PI Transfer',
    [switch]$RunWhenLoggedOff
)
$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $ScriptPath)) { throw "Not found: $ScriptPath" }
$user = "$env:USERDOMAIN\$env:USERNAME"

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $ScriptPath) `
    -WorkingDirectory (Split-Path $ScriptPath)

# Every minute, indefinitely (no -RepetitionDuration = forever on Windows 10 / Server 2016+).
$every = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 1)
$logon = New-ScheduledTaskTrigger -AtLogOn -User $user

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

$params = @{
    TaskName    = $TaskName
    Action      = $action
    Settings    = $settings
    Description = 'Sends PI Mismatch JSON files to the RHEL server (Send-PiFiles.ps1).'
    Force       = $true
}

if ($RunWhenLoggedOff) {
    $cred = Get-Credential -UserName $user -Message 'Your Windows password (stored by Task Scheduler)'
    $params.User = $user
    $params.Password = $cred.GetNetworkCredential().Password
    $params.Trigger = @($every)
    try {
        Register-ScheduledTask @params | Out-Null
        Write-Host "Registered '$TaskName' (runs whether or not you are logged on)."
    } catch {
        Write-Warning "Windows refused 'run when logged off' for this account: $($_.Exception.Message)"
        Write-Warning 'Re-run without -RunWhenLoggedOff; the server will alert if you are logged off.'
        exit 1
    }
} else {
    $params.Principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive
    try {
        $params.Trigger = @($every, $logon)
        Register-ScheduledTask @params | Out-Null
    } catch {
        # Some policies do not allow logon triggers for standard users; the
        # every-minute trigger alone is enough.
        $params.Trigger = @($every)
        Register-ScheduledTask @params | Out-Null
    }
    Write-Host "Registered '$TaskName' (runs every minute while you are logged on)."
}

Start-ScheduledTask -TaskName $TaskName
Write-Host 'Started. Check: Get-ScheduledTaskInfo -TaskName ''PI Transfer'''
Write-Host "Logs: $env:LOCALAPPDATA\PI_Transfer\logs"
