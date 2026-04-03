from __future__ import annotations

import importlib.util
from pathlib import Path

SRC_MAIN_PATH = Path(__file__).resolve().parent / "src" / "main.py"


def _load_entrypoint():
    spec = importlib.util.spec_from_file_location("sliv_appwrite_src", SRC_MAIN_PATH)
    if not spec or not spec.loader:
        raise RuntimeError(f"Failed to load Appwrite entrypoint from {SRC_MAIN_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "main"):
        raise RuntimeError(f"Appwrite entrypoint at {SRC_MAIN_PATH} does not define main(context)")
    return module.main


main = _load_entrypoint()
