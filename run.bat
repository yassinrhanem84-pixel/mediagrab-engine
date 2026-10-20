@echo off
cd /d "%~dp0"
python -m pip install -q -U -r requirements.txt
start "" http://localhost:5000
python app.py
pause
