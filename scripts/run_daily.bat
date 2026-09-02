@echo off
chcp 65001 >nul
REM ============================================================
REM  日本株 急騰レーダー 日次バッチ (Windows タスクスケジューラ用)
REM  平日の引け後(例: 16:30)に実行する想定。
REM
REM  2026-09-02: 以前はここで predict/train/track を含むフルパイプライン
REM  (surge_radar.cli daily) を実行していたが、GitHub Actions の daily.yml
REM  も同じ本番DBに対して独立に同じことをしており、どちらが最後に走るかで
REM  本番データが決まる不安定な二重パイプライン状態になっていた。
REM  predictions への書き込みは daily.yml だけが担当するよう一本化し、
REM  このローカルタスクは kabutan/みんかぶ(クラウドIPからブロックされる
REM  2ソース)の追加取得だけに専念する。
REM ============================================================
setlocal
cd /d "%~dp0\.."
set PYTHONIOENCODING=utf-8

".venv\Scripts\python.exe" scripts\materials_local_extra.py >> "data\logs\daily_%date:~0,4%%date:~5,2%%date:~8,2%.log" 2>&1

endlocal
