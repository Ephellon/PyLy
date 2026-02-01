@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM --------------------------------------------
REM PyLy launcher / bootstrapper
REM --------------------------------------------

REM Resolve directory of this .bat (project root)
set "SCRIPT_DIR=%~dp0"
REM Remove trailing backslash
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

REM Check if PyLy is importable
python -c "import pyly" >NUL 2>&1
if errorlevel 1 (
    echo [PyLy] Not installed. Installing package...
    echo [PyLy] Path: %SCRIPT_DIR%
    python -m pip install .
    if errorlevel 1 (
        echo [PyLy] Installation failed.
        exit /b 1
    )
)

REM Run PyLy CLI, forwarding all arguments
python -m pyly %*
