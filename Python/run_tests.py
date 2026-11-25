"""
Runs every offline test (no internet, Ollama or Telegram needed).
    python Python\run_tests.py
"""

import sys
import unittest
from pathlib import Path

if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    suite = unittest.defaultTestLoader.discover(str(here), pattern="selftest_*.py")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    print("\nALL TESTS PASSED" if result.wasSuccessful() else "\nSOME TESTS FAILED")
    raise SystemExit(0 if result.wasSuccessful() else 1)
