# AI Career Agent - registers the Windows scheduled tasks (no admin rights needed).
# Run from the project folder:   powershell -ExecutionPolicy Bypass -File schedule_tasks.ps1
# Optional:                      ... -File schedule_tasks.ps1 -Time 20:30
param([string]$Time = "21:00")

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { Write-Host "Run setup.bat first (.venv not found)."; exit 1 }

# 1) Daily job search + CV + Telegram
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1)
$daily = New-ScheduledTaskAction -Execute "$root\run_daily.bat" -WorkingDirectory $root
$dailyTrigger = New-ScheduledTaskTrigger -Daily -At $Time
Register-ScheduledTask -TaskName "AI Career Agent" -Action $daily -Trigger $dailyTrigger `
    -Settings $settings -Description "Daily job search at $Time" -Force | Out-Null

# 2) The Telegram bot: answers buttons and /status instantly, sends deadline reminders.
#    Runs in the background without a window. The 10-minute trigger only (re)starts it
#    after a log-in or a crash; a second copy is never started.
Unregister-ScheduledTask -TaskName "AI Career Agent Inbox" -Confirm:$false -ErrorAction SilentlyContinue
Stop-ScheduledTask -TaskName "AI Career Agent Bot" -ErrorAction SilentlyContinue      # restart with the newest code
Remove-Item (Join-Path $root "Database\bot_heartbeat.txt") -ErrorAction SilentlyContinue
$bot = New-ScheduledTaskAction -Execute $pythonw -Argument '"Python\telegram_inbox.py" --listen' -WorkingDirectory $root
$botTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 10)
$botSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
               -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "AI Career Agent Bot" -Action $bot -Trigger $botTrigger `
    -Settings $botSettings -Description "Telegram buttons, /status and deadline reminders" -Force | Out-Null
Start-ScheduledTask -TaskName "AI Career Agent Bot"

Start-Sleep -Seconds 3
Get-ScheduledTask -TaskName "AI Career Agent*" | Select-Object TaskName, State
Write-Host "`nDone. Daily run at $Time; the bot is running (log: Logs\bot.log)."
Write-Host "Remove everything with: Unregister-ScheduledTask -TaskName 'AI Career Agent*'"
