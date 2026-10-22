"""Exception types raised by ATE.

``ATEError`` is the single base class for every error ATE raises, so callers can
catch any ATE failure with one ``except``. Specific subclasses are added as the
tickets that need them resolve; this file exists now so the hierarchy has a home
and so early code can raise :class:`ATEError` without inventing feature-specific
names prematurely.
"""

from __future__ import annotations


class ATEError(Exception):
    """Base class for all errors raised by ATE."""
