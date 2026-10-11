@echo off
cd /d "%~dp0"

set "PY=%LocalAppData%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=py"

for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8765 " ^| findstr LISTENING') do taskkill /F /PID %%P >nul 2>&1
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8000 " ^| findstr LISTENING') do taskkill /F /PID %%P >nul 2>&1

start "MATPLATLIB" /min "%PY%" -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
timeout /t 2 /nobreak >nul
start http://127.0.0.1:8000/
