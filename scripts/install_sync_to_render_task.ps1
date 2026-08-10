# Instala tarea de Windows: sync PC → Render cada 15 minutos (SIN consola).
# Ejecutar una vez en PowerShell (como el usuario que usa el ERP):
#   powershell -ExecutionPolicy Bypass -File scripts\install_sync_to_render_task.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
# pythonw.exe = sin ventana negra. Fallback a python.exe si no existe.
$PythonW = Join-Path $Root ".venv\Scripts\pythonw.exe"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Exe = if (Test-Path $PythonW) { $PythonW } else { $Python }
$Script = Join-Path $Root "scripts\sync_db_to_render.py"
$TaskName = "AndesAutoParts-SyncDbToRender"

if (-not (Test-Path $Exe)) {
  Write-Error "No se encontro $Exe. Activa el venv del proyecto primero."
}
if (-not (Test-Path $Script)) {
  Write-Error "No se encontro $Script"
}

$Action = New-ScheduledTaskAction -Execute $Exe -Argument "`"$Script`"" -WorkingDirectory $Root
# Repetir 15 min durante ~10 años (MaxValue rompe el XML de Task Scheduler en Windows).
$Trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 3650)
$Settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable `
  -MultipleInstances IgnoreNew `
  -Hidden `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Description "Sincroniza andes.db del PC hacia Render (app movil) cada 15 minutos, sin consola. Solo PC -> nube." | Out-Null

Write-Host "OK: tarea '$TaskName' instalada (cada 15 min, sin ventana)."
Write-Host "Usa: $Exe"
Write-Host "Log: $Root\logs\sync_to_render.log"
Write-Host "Probar (con consola): & `"$Python`" `"$Script`" --force"
Write-Host "Quitar: Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
