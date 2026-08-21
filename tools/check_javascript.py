"""Cross-platform JavaScript syntax gate used by Linux and Windows CI."""
from __future__ import annotations

import pathlib
import subprocess
import sys


def main() -> int:
    files = sorted(pathlib.Path("frontend").rglob("*.js"))
    if not files:
        print("No JavaScript files found", file=sys.stderr)
        return 1
    for path in files:
        subprocess.run(["node", "--check", str(path)], check=True)
    print(f"Checked {len(files)} JavaScript files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
