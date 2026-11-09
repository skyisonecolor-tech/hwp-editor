@echo off
chcp 65001 > nul
REM ===== 한글 쪽 편집기 exe 만들기 =====
REM  준비: Python 3.10 이상 설치 (설치할 때 "Add python.exe to PATH" 체크)
cd /d "%~dp0"

echo [1/3] 필요한 패키지 설치 중...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 goto :err

echo [2/3] 실행 파일 만드는 중... (몇 분 걸립니다)
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name HwpPageTool ^
  --hidden-import win32com.client --hidden-import pythoncom --hidden-import pywintypes ^
  --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtWebEngineWidgets ^
  --exclude-module PySide6.Qt3DCore --exclude-module PySide6.QtQuick --exclude-module PySide6.QtQml ^
  --exclude-module PySide6.QtMultimedia --exclude-module PySide6.QtCharts --exclude-module PySide6.QtDataVisualization ^
  main.py
if errorlevel 1 goto :err

echo [3/3] 완료!
echo   dist\HwpPageTool.exe 가 만들어졌습니다.
explorer dist
pause
exit /b 0

:err
echo.
echo 오류가 발생했습니다. 위 메시지를 확인하세요.
pause
exit /b 1
