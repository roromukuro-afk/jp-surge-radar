@echo off
REM ============================================================
REM  日本株 急騰レーダー Webサーバ起動
REM  LAN内スマホからもアクセス可能 (0.0.0.0 bind)
REM  スマホ用URL: http://<このPCのIPアドレス>:8012
REM ============================================================
setlocal
cd /d "%~dp0\.."
set PYTHONIOENCODING=utf-8

REM LAN IPを表示
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /C:"IPv4"') do (
    for /f "tokens=1" %%b in ("%%a") do (
        echo.
        echo   スマホからのアクセスURL: http://%%b:8012
        echo   PC用アクセスURL:         http://127.0.0.1:8012
        echo.
    )
)

".venv\Scripts\python.exe" -m surge_radar.cli serve --host 0.0.0.0 --port 8012
endlocal
