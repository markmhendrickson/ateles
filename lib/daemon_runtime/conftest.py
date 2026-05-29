"""
Make this package's modules importable by bare name in tests (e.g.
`from drift import ...`), matching the repo's in-dir test convention, whether
pytest is invoked from here or from the repo root.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
