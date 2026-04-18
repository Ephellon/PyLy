#!/bin/bash

# --------------------------------------------
# PyLy launcher / bootstrapper
# --------------------------------------------

# Set the python command (use 'python' if you are on a system where that is the default)
PYTHON_EXE="python3"

# Resolve directory of this script (project root)
# This gets the absolute path to the directory containing the script
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# Check if PyLy is importable
if ! $PYTHON_EXE -c "import pyly" > /dev/null 2>&1; then
    echo "[PyLy] Not installed. Installing package..."
    echo "[PyLy] Path: $SCRIPT_DIR"

    # Change to script directory to ensure pip install . works
    cd "$SCRIPT_DIR"
    $PYTHON_EXE -m pip install .

    if [ $? -ne 0 ]; then
        echo "[PyLy] Installation failed."
        exit 1
    fi
fi

# Run PyLy CLI, forwarding all arguments ("$@" is the equivalent of %*)
$PYTHON_EXE -m pyly "$@"
