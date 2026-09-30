#!/bin/zsh
set -eu
script_dir="${0:A:h}"
exec python3 "$script_dir/bridge.py" setup
