"""Stable GUI entry point.

Keeps the project root on sys.path so the existing local jmcomic package and
utility modules continue to resolve when launched from any working directory.
"""
from pathlib import Path
import os
import sys


ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from unified_gui import main


if __name__ == '__main__':
    main()
