@echo off
cd /d "%~dp0"
python mqb_receiver.py %*
if errorlevel 1 pause
