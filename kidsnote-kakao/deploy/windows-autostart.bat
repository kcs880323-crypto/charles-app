@echo off
rem 로그인할 때 브리지를 자동 실행하도록 작업 스케줄러에 등록 (다시 실행하면 덮어씀)
rem 해제: schtasks /Delete /TN kidsnote-kakao /F
chcp 65001 >nul
set "RUN=%~dp0windows-run.bat"
schtasks /Create /TN kidsnote-kakao /TR "\"%RUN%\"" /SC ONLOGON /RL LIMITED /F
if errorlevel 1 (
  echo [오류] 등록 실패. 관리자 권한 명령 프롬프트에서 다시 실행해 보세요.
  pause
  exit /b 1
)
schtasks /Run /TN kidsnote-kakao
echo 등록 완료. 지금 바로 실행했습니다. 로그: %~dp0..\data\bridge.log
echo 상태 확인: python -m kidsnote_kakao status
pause
