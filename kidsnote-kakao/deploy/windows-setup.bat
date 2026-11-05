@echo off
rem 키즈노트 → 카카오톡 브리지: Windows 처음 설정 (더블클릭)
rem  1) Python 확인  2) config.toml / .env 생성 + 카카오 인증 + 테스트 메시지
chcp 65001 >nul
cd /d "%~dp0.."

where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
%PY% -c "import sys; assert sys.version_info >= (3, 11)" 2>nul
if errorlevel 1 (
  echo [오류] Python 3.11 이상이 필요합니다. https://www.python.org/downloads/ 에서 설치하세요
  echo        설치할 때 "Add python.exe to PATH" 를 체크하세요.
  pause
  exit /b 1
)
%PY% -c "import zoneinfo; zoneinfo.ZoneInfo('Asia/Seoul')" 2>nul || %PY% -m pip install --quiet tzdata

%PY% -m kidsnote_kakao init
echo.
echo 다음: deploy\windows-autostart.bat 을 실행하면 로그인할 때마다 자동으로 켜집니다.
pause
