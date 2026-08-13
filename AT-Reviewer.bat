@echo off
rem AT-Reviewer 창 프로그램 실행 (더블클릭)
rem pythonw.exe 로 띄워야 뒤에 검은 콘솔 창이 남지 않는다.

cd /d "%~dp0"

if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" -m src.gui
    exit /b 0
)

echo [AT-Reviewer] 가상환경(.venv)을 찾지 못했습니다.
echo 아래 명령으로 먼저 준비하세요:
echo.
echo     python -m venv .venv
echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
echo.
pause
