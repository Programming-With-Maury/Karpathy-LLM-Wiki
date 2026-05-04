#!/usr/bin/env python3
"""Copy the sanitized demo workspace into workspaces/demo."""

from __future__ import annotations

import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "examples" / "demo-workspace"
TARGET = ROOT / "workspaces" / "demo"


def main() -> None:
    if not SOURCE.exists():
        raise SystemExit("examples/demo-workspace is missing.")
    if TARGET.exists():
        raise SystemExit("workspaces/demo already exists. Remove it or choose a fresh workspace name.")
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SOURCE, TARGET)
    print(f"Demo workspace copied to {TARGET.relative_to(ROOT)}")
    print("Start the app, then select the demo workspace from the Workspaces panel.")


if __name__ == "__main__":
    main()
