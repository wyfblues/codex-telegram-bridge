#!/usr/bin/env python3
"""Preserve the existing machine callback and enqueue one minimal Telegram notice."""
import json
import subprocess
import sys
import bridge


def main():
    args = sys.argv[1:]
    try:
        bridge.notify(json.loads(args[-1]))
    except Exception:
        pass
    # Even invalid/unknown notifications still reach the original callback.
    try:
        original = json.loads((bridge.ROOT / 'original-notify.json').read_text())
        if original:
            subprocess.run(original + args, timeout=8, check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


if __name__ == '__main__':
    main()
