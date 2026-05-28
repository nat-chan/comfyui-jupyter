#!/usr/bin/env python
"""Register a JupyterLab kernel that loads `comfyui_kernel` from this repo.

Run this with the Python interpreter you want the kernel to use — usually
your ComfyUI venv:

    /path/to/ComfyUI/venv/bin/python scripts/install_kernel.py

The script captures `sys.executable` and the repo root at install time
and bakes them into the generated kernel.json. PYTHONPATH points at the
repo root so the kernel process can `import comfyui_kernel` without us
needing to ship a wheel.

Re-run after moving the repo or switching ComfyUI venv to refresh the
kernelspec.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from jupyter_client.kernelspec import KernelSpecManager

KERNEL_NAME = "comfyui"
DISPLAY_NAME = "ComfyUI (Python)"

REPO_ROOT = Path(__file__).resolve().parent.parent

spec = {
    "argv": [sys.executable, "-m", "comfyui_kernel", "-f", "{connection_file}"],
    "display_name": DISPLAY_NAME,
    "language": "python",
    "metadata": {"debugger": False},
    "env": {"PYTHONPATH": str(REPO_ROOT)},
}

with tempfile.TemporaryDirectory() as tmp:
    tmp_dir = Path(tmp)
    (tmp_dir / "kernel.json").write_text(json.dumps(spec, indent=2) + "\n")
    # `install_kernel_spec` always replaces; recent jupyter_client deprecated
    # the explicit `replace=True` so we omit it.
    dest = KernelSpecManager().install_kernel_spec(
        str(tmp_dir),
        kernel_name=KERNEL_NAME,
        user=True,
    )

print(f"Installed kernel '{KERNEL_NAME}' at {dest}")
print(f"  python:     {sys.executable}")
print(f"  PYTHONPATH: {REPO_ROOT}")
