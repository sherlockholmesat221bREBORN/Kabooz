#!/usr/bin/env python3
# scripts/make_dev_audio.py
"""
One-time script to bake a short audio clip into kabooz/data/dev_audio.opus.

Usage:
    python scripts/make_dev_audio.py /path/to/rickroll.mp3
    python scripts/make_dev_audio.py /path/to/rickroll.mp3 --start 43 --duration 4

Defaults to 4 seconds from the start. Outputs mono Opus at 32kbps — typically
~16KB, which is negligible package overhead. Opus at 32kbps sounds noticeably
better than MP3 at 64kbps for voice/music, which is why we use it here.

Requires ffmpeg with libopus support (standard in most builds).

After running:
    git add kabooz/data/dev_audio.opus
    git commit -m "chore: add dev audio clip"
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

DEST = Path(__file__).parent.parent / "kabooz" / "data" / "dev_audio.opus"


def main() -> None:
    parser = argparse.ArgumentParser(description="Bake a dev audio clip into the package.")
    parser.add_argument("source",      help="Source audio file (any format ffmpeg understands)")
    parser.add_argument("--start",    type=float, default=0.0, help="Start offset in seconds (default: 0)")
    parser.add_argument("--duration", type=float, default=4.0, help="Clip duration in seconds (default: 4)")
    args = parser.parse_args()

    if not shutil.which("ffmpeg"):
        print("ERROR: ffmpeg not found in PATH.", file=sys.stderr)
        sys.exit(1)

    source = Path(args.source)
    if not source.exists():
        print(f"ERROR: source file not found: {source}", file=sys.stderr)
        sys.exit(1)

    DEST.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(args.start),
        "-t",  str(args.duration),
        "-i",  str(source),
        "-ac", "1",             # mono — halves the size
        "-c:a", "libopus",
        "-b:a", "32k",          # 32kbps Opus ≈ 128kbps MP3 in perceived quality
        "-vbr", "on",
        "-compression_level", "10",
        "-map_metadata", "-1",  # strip all metadata from the clip
        str(DEST),
    ]

    print(f"Source:   {source}")
    print(f"Clip:     {args.start}s + {args.duration}s")
    print(f"Output:   {DEST}")
    print()

    result = subprocess.run(cmd, stderr=subprocess.PIPE)
    if result.returncode != 0:
        print("ERROR: ffmpeg failed:")
        print(result.stderr.decode(errors="replace"))
        sys.exit(1)

    size_kb = DEST.stat().st_size / 1024
    print(f"Done. {size_kb:.1f} KB")
    print()
    print("Add to pyproject.toml if not already present:")
    print('  [tool.setuptools.package-data]')
    print('  "kabooz" = ["data/*.opus"]')


if __name__ == "__main__":
    main()

