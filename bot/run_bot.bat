@echo off
rem Runs Front Desk with the shared virtualenv in ..\server\.venv
cd /d "%~dp0"
"..\server\.venv\Scripts\python.exe" main.py
rem Keep the window open after a crash so the error can be read.
if errorlevel 1 pause
