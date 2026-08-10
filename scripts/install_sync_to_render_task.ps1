# Instala tarea de Windows: sync PC → Render cada 15 minutos.
# Ejecutar una vez en PowerShell (como el usuario que usa el ERP):
#   powershell -ExecutionPolicy Bypass -File scripts\install_sync_to_render_task.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Script = Join-Path $Root "scripts\sync_db_to_render.py"
$TaskName = "AndesAutoParts-SyncDbToRender"

if (-not (Test-Path $Python)) {
  Write-Error "No se encontro $Python. Activa el venv del proyecto primero."
}
if (-not (Test-Path $Script)) {
  Write-Error "No se encontro $Script"
}

$Action = New-ScheduledTaskAction -Execute $Python -Argument "`"$Script`"" -WorkingDirectory $Root
# Repetir 15 min durante ~10 años (MaxValue rompe el XML de Task Scheduler en Windows).
$Trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 3650)
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Description "Sincroniza andes.db del PC hacia Render (app movil) cada 15 minutos. Solo PC -> nube." | Out-Null

Write-Host "OK: tarea '$TaskName' instalada (cada 15 min)."
Write-Host "Probar ahora: & `"$Python`" `"$Script`" --force"
Write-Host "Quitar: Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
