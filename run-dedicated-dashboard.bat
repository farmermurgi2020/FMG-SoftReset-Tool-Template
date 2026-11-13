@echo off
cd /d "%~dp0"
py -3 dedicated_web_ui.py
if errorlevel 1 pause
