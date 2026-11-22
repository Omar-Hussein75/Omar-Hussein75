# Registers the watchdog as a scheduled task: every 5 minutes, forever,
# at boot too, whether or not anyone is logged on. Run as Administrator.
param(
    [string]$Python = "C:\Program Files\Python312\python.exe",
    [string]$AppDir = "C:\PI_Transfer"
)

$action  = New-ScheduledTaskAction -Execute $Python `
             -Argument "`"$AppDir\healthcheck.py`"" -WorkingDirectory $AppDir
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
             -RepetitionInterval (New-TimeSpan -Minutes 5)
$boot    = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
             -ExecutionTimeLimit (New-TimeSpan -Minutes 4) `
             -MultipleInstances IgnoreNew `
             -StartWhenAvailable `
             -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
             -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
# SYSTEM so it can restart the service and write the Event Log.
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName "PI Transfer Healthcheck" -Action $action `
    -Trigger @($trigger, $boot) -Settings $settings -Principal $principal -Force

# Create the Event Log source once (needs admin).
eventcreate /T INFORMATION /ID 100 /L APPLICATION /SO PI_Mismatch_Transfer /D "PI Transfer healthcheck installed." | Out-Null
Write-Host "Healthcheck task registered."
