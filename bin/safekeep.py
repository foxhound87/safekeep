#!/usr/bin/env python3
"""Shim: launchd (plist) e i test invocano ancora `bin/safekeep.py`; la CLI
vive in `safekeep/cli.py` (SPEC.md §9)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from safekeep.cli import main

if __name__ == '__main__':
    sys.exit(main())
