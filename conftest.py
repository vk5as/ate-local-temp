"""Make the src-layout importable when running pytest without an editable install.

Under CI the package is installed with ``pip install -e .`` (see
``.github/workflows/ci.yml``), which puts ``src/`` on ``sys.path`` itself. This
file only exists so tests can run straight from a checkout; it is harmless once
the package is installed, where ``import ate`` resolves through the editable
install regardless of this path tweak.
"""

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
