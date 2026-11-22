@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..\..") do set "REPO_ROOT=%%~fI"

if exist "%REPO_ROOT%\.venv\Scripts\python.exe" (
    "%REPO_ROOT%\.venv\Scripts\python.exe" "%SCRIPT_DIR%server.py" --open
    goto :eof
)

where uv >nul 2>nul
if %errorlevel%==0 (
    cd /d "%REPO_ROOT%"
    uv run python "%SCRIPT_DIR%server.py" --open
    goto :eof
)

python "%SCRIPT_DIR%server.py" --open
endlocal
