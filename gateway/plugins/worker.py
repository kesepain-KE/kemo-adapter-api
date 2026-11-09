"""Isolated process entry. Executed only after the owner grants manifest permissions."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
entry = Path(sys.argv[2])
sys.path.insert(0, str(entry.parent))
spec = importlib.util.spec_from_file_location("kemo_advanced_plugin", entry)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

from gateway.plugins.sdk import run

run(module.create_plugin())
