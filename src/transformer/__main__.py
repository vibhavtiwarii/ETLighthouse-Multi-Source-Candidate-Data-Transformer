"""
src/transformer/__main__.py
────────────────────────────────────────────────────────────────────────────────
Makes the transformer package directly runnable via:

    python -m transformer run --csv ... --ats ... --notes-dir ... --config ... --out ...

Python executes this file when the package is invoked with ``-m``.
It simply delegates to the CLI's ``main()`` function, which handles
argument parsing, input validation, and pipeline dispatch.
"""

from src.transformer.cli import main

if __name__ == "__main__":
    main()
