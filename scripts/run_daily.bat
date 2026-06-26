@echo off
REM ============================================================
REM  日本株 急騰レーダー 日次バッチ (Windows タスクスケジューラ用)
REM  平日の引け後(例: 16:30)に実行する想定。
REM ============================================================
setlocal
cd /d "%~dp0\.."
set PYTHONIOENCODING=utf-8

REM 全銘柄(3000円以下)対象。初回や時間短縮したい場合は --limit を付ける。
".venv\Scripts\python.exe" -m surge_radar.cli daily >> "data\logs\daily_%date:~0,4%%date:~5,2%%date:~8,2%.log" 2>&1

endlocal
