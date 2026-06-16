@echo off
cd /d %~dp0
python -m pip install -r requirements.txt
python -m uvicorn app:app --host 0.0.0.0 --port 8080 --reload
pause
