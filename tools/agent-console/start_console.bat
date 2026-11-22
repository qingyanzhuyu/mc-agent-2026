@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
for /f "usebackq delims=" %%i in (`wsl wslpath -a "%SCRIPT_DIR%start_console.sh"`) do set "WSL_SCRIPT=%%i"
wsl bash -lc "'%WSL_SCRIPT%'"
start "" "http://127.0.0.1:8795/"
endlocal
