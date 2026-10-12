"""Import this first: points %APPDATA% at a throwaway folder so tests never touch real user data."""

import atexit
import os
import shutil
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="clickwave_test_")
os.environ["APPDATA"] = _tmp
atexit.register(shutil.rmtree, _tmp, True)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
