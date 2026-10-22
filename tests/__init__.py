"""Make ``tests`` an importable package.

Ticket 02's seam tests use relative imports (``from ._fixtures import ...``) to share
on-disk helpers across modules without importing the app under test. This marker lets
pytest treat ``tests`` as a package; :file:`conftest.py` puts the repository root on
``sys.path`` so ``import tests.test_run_engine`` resolves. The existing seam-2 tests use
absolute imports (``from ate.storage ...``) and are unaffected by this.
"""
