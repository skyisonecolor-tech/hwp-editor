@echo off
chcp 65001 > nul
REM 한글 없이 화면만 시험해 보는 모드 (실제 파일은 만들지 않음)
cd /d "%~dp0"
python main.py --fake
