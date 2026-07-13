#!/bin/bash

# --------------------------------------------
# PyLy launcher / bootstrapper
# --------------------------------------------

# Set the python command (use 'python' if you are on a system where that is the default)
PYTHON_EXE="python3"

# Resolve directory of this script (project root)
# This gets the absolute path to the directory containing the script
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# Always run the PyLy that sits next to this script. Putting its folder first on
# PYTHONPATH makes the working-tree package win over any older copy installed in
# a different Python's site-packages, which is what causes "unrecognized
# arguments" version-skew errors for newer flags (e.g. --layout).
export PYTHONPATH="$SCRIPT_DIR:$PYTHONPATH"

# Check if PyLy is importable (normally true via PYTHONPATH above)
if ! $PYTHON_EXE -c "import pyly" > /dev/null 2>&1; then
    echo "[PyLy] Not importable. Installing package (editable)..."
    echo "[PyLy] Path: $SCRIPT_DIR"

    $PYTHON_EXE -m pip install -e "$SCRIPT_DIR"

    if [ $? -ne 0 ]; then
        echo "[PyLy] Installation failed. Is $PYTHON_EXE installed and on PATH?"
        exit 1
    fi
fi

# Run PyLy CLI, forwarding all arguments ("$@" is the equivalent of %*)
$PYTHON_EXE -m pyly "$@"
