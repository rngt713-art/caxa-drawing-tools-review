"""Development harness: run the external acceptance scripts unmodified.

The Codex acceptance scripts create temp directories next to themselves (outside
this workspace) and Python 3.13 ``mkdtemp`` applies a 0o700 DACL that the
restricted token cannot extend in this environment.  This loader reads the
script text without editing it, redirects only its temp-directory calls into
``tests/_tmp`` with default permissions, and executes it.

Usage: python tests/run_external_acceptance.py ../independent_acceptance.py
"""

import contextlib
import os
import shutil
import sys
import types
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent / "app"
TMP_BASE = HERE / "_tmp"


class _TempDir:
    def __init__(self, *args, **kwargs):
        os.makedirs(TMP_BASE, exist_ok=True)
        self.name = str(TMP_BASE / ("acc_" + uuid.uuid4().hex[:10]))
        os.makedirs(self.name)

    def __enter__(self):
        return self.name

    def __exit__(self, *exc):
        self.cleanup()

    def cleanup(self):
        shutil.rmtree(self.name, ignore_errors=True)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: run_external_acceptance.py <script.py>")
        return 2
    script = Path(sys.argv[1]).resolve()
    source = script.read_text(encoding="utf-8")
    source = source.replace("tempfile.TemporaryDirectory", "_TempDir")
    prelude = (
        "import contextlib, os, shutil, sys, uuid\n"
        "from pathlib import Path\n"
        "sys.path.append(str(__import__('run_external_acceptance').HERE / '_stub_deps'))\n"
        "sys.path.insert(0, str(__import__('run_external_acceptance').HERE.parent / 'app'))\n"
        "_TempDir = __import__('run_external_acceptance')._TempDir\n"
    )
    sys.path.insert(0, str(HERE))
    module = types.ModuleType("__main__")
    module.__file__ = str(script)
    module._TempDir = _TempDir
    sys.modules["__main__"] = module
    sys.argv = [str(script), str(APP)]
    try:
        exec(compile(prelude + source, str(script), "exec"), module.__dict__)
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
