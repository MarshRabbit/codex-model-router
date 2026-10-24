#!/usr/bin/env python3
"""Run the bundled Codex router without installing Python dependencies."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "skills" / "codex-model-router" / "scripts"))
from routerlib.cli import main

if __name__ == "__main__":
    sys.exit(main())
