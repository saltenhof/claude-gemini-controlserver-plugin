@echo off
cd /d "%~dp0"
pip install -r requirements.txt --quiet
python server.py
pause
