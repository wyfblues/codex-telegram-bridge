#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
repo_dir=$(CDPATH= cd -- "$script_dir/.." && pwd -P)
exec "${CODEX_BRIDGE_PYTHON:-python3}" "$repo_dir/bridge.py" setup
