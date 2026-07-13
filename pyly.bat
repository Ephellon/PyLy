@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM --------------------------------------------
REM PyLy launcher / bootstrapper
REM --------------------------------------------

REM Resolve directory of this .bat (project root)
set "SCRIPT_DIR=%~dp0"
REM Remove trailing backslash
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

REM Always run the PyLy that sits next to THIS script. Putting its folder first
REM on PYTHONPATH makes the working-tree package win over any older copy a
REM previous run may have installed into another Python's site-packages. That
REM version skew is what makes flags the stale copy doesn't know about (e.g.
REM --layout) fail with "unrecognized arguments".
set "PYTHONPATH=%SCRIPT_DIR%;%PYTHONPATH%"

REM Check if PyLy is importable (normally true via PYTHONPATH above)
python -c "import pyly" >NUL 2>&1
if errorlevel 1 (
    echo [PyLy] Not importable. Installing package in editable mode...
    echo [PyLy] Path: %SCRIPT_DIR%
    python -m pip install -e "%SCRIPT_DIR%"
    if errorlevel 1 (
        echo [PyLy] Installation failed. Is Python installed and on PATH?
        exit /b 1
    )
)

REM Run PyLy CLI, forwarding all arguments
python -m pyly %*
