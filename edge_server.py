#!/usr/bin/env python3
"""Backward-compatible launcher. Prefer: python3 visionedge_server.py"""
from visionedge_server import *  # noqa: F401,F403

if __name__ == '__main__':
    main()
