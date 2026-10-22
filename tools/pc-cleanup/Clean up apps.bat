@echo off
title Clean up apps
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0cleanup.ps1"
