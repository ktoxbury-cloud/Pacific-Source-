@echo off
REM Double-click to open the repair scheduler (Windows). Needs Python 3.10+ from python.org.
cd /d "%~dp0"
python -m kodiak_scheduler serve
pause
