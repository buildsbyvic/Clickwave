"""
Clickwave - click sounds for the inputs you choose.

Run:      py clickwave.py
Build:    py -m PyInstaller --noconfirm Clickwave.spec

The app lives in the clickwave/ package; this file is just the launcher
(and the PyInstaller entry point).
"""

from clickwave.ui.app import main

if __name__ == "__main__":
    main()
