@echo off
rem 키즈노트 → 카카오톡 브리지 실행 (작업 스케줄러가 호출). 로그는 data\bridge.log
chcp 65001 >nul
cd /d "%~dp0.."
if not exist data mkdir data
where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8
%PY% -m kidsnote_kakao -c config.toml serve >> data\bridge.log 2>&1
