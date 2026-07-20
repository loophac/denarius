#!/usr/bin/env sh
set -eu

python3 -m pip install .
exec denarius "$@"
