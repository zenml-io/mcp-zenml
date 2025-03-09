#!/usr/bin/env bash
set -x

# Initialize default source directories
default_src="."
# Initialize SRC as an empty string
SRC=""



# If no source directories were provided, use the default
if [ -z "$SRC" ]; then
    SRC="$default_src"
fi

# autoflake replacement: removes unused imports and variables
ruff check $SRC --select F401,F841 --fix --exclude "__init__.py" --isolated

# sorts imports
ruff check $SRC --select I --fix --ignore D
ruff format $SRC
