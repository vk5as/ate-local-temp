"""Run ``python -m ate`` — used by the PDF helper subprocess (ticket 22) and for local CLI use."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
