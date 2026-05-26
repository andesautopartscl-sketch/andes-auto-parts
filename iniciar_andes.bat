@echo off
chcp 65001 >nul
title Andes Auto Parts - servidor local
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  echo Usando entorno virtual: .venv
  ".venv\Scripts\python.exe" run.py
) else (
  echo No hay .venv\Scripts\python.exe - usando python del PATH
  python run.py
)

echo.
echo El servidor se detuvo o hubo un error.
pause
