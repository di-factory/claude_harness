# devcell-test

A tiny library used to test Di-Factory's Dev Cell.

- Python 3.12, standard library only: never add dependencies.
- Tests: `python3 -m unittest -v`. Every fix comes with a test in `test_pagination.py` that
  fails before the fix and passes after it.
- Keep functions small and typed; docstrings say what, not how.
