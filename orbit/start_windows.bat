@echo off
rem Launch ORBITA: .venv in the repository root, shared by the server and the core.
cd /d "%~dp0.."
if not exist .venv\Scripts\python.exe py -3 -m venv .venv
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto fail
.venv\Scripts\python.exe orbit\app.py --open-browser
goto end
:fail
echo Install Python 3.11 or newer and try again.
:end
pause
