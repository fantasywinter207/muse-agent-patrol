#!/usr/bin/env python3
"""Compatibility adapter to the same bounded, single-instance v3 tick."""
import sys
from patrol.index import main

if __name__ == "__main__":
    args=sys.argv[1:]
    prefix=[]
    while args and args[0] in ("--config","--db"):
        prefix.extend(args[:2]); args=args[2:]
    raise SystemExit(main(prefix+["tick","--ws"]+args))
