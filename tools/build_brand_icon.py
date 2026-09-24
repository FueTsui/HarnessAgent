"""Encode the existing brand artwork into a Windows multi-resolution ICO."""
import hashlib
import json
import shutil
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "packaging/defaults/branding/web-app-manifest-512x512.png"
TARGET = ROOT / "packaging/windows/HarnessAgent.ico"
SIZES = [(size, size) for size in (16, 20, 24, 32, 40, 48, 64, 128, 256)]


def main():
    with Image.open(SOURCE) as source:
        artwork = source.convert("RGBA")
        if artwork.width != artwork.height or artwork.width < 256:
            raise ValueError("Brand artwork must be square and at least 256px")
        TARGET.parent.mkdir(parents=True, exist_ok=True)
        artwork.save(TARGET, format="ICO", sizes=SIZES)
    shutil.copyfile(SOURCE, ROOT / "desktop/brand.png")
    with Image.open(TARGET) as result:
        assert result.ico.sizes() == set(SIZES)
        for size in SIZES:
            expected = artwork.resize(size, Image.Resampling.LANCZOS)
            assert result.ico.getimage(size).convert("RGBA").tobytes() == expected.tobytes()
    print(json.dumps({"source": str(SOURCE), "icon": str(TARGET), "sizes": SIZES,
                      "sha256": hashlib.sha256(TARGET.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    main()
