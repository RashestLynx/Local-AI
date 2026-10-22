@echo off
title Local AI - keep this window open (minimize it)
cd /d "%~dp0"
where py >nul 2>&1 && (py -3 app.py) || (python app.py)
pause
