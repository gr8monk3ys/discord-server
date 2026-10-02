@echo off
rem Runs Front Desk with the shared virtualenv in ..\server\.venv
cd /d "%~dp0"
"..\server\.venv\Scripts\python.exe" main.py
