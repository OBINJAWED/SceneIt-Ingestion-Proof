"""Bounded subprocess boundary shared by private frame and proof still work."""
import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from .resources import shared_permit
from .processes import bounded_process

MAX_FRAME_BYTES = 1_000_000


def private_frame(object_path, generation, midpoint):
    with shared_permit("media", lease_seconds=75):
        with tempfile.TemporaryDirectory(prefix="sceneit-frame-") as directory:
            result = bounded_process(
                [sys.executable, "-m", "sceneit.media", object_path, str(generation),
                 str(midpoint), directory], timeout=60)
            frame = Path(directory) / "frame.jpg"
            if result or not frame.is_file() or not 0 < frame.stat().st_size <= MAX_FRAME_BYTES:
                raise RuntimeError("Source frame extraction failed")
            return frame.read_bytes()


def _extract(object_path, generation, midpoint, directory):
    from .private_storage import download_object
    source = Path(directory) / "source.mp4"
    download_object(object_path, source, max_bytes=200_000_000, generation=generation)
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-ss", str(midpoint), "-i", str(source),
         "-frames:v", "1", "-vf", "scale=640:-2", "-threads", "1",
         str(Path(directory) / "frame.jpg")],
        timeout=20, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Internal bounded private frame tool")
    parser.add_argument("object_path")
    parser.add_argument("generation", type=int)
    parser.add_argument("midpoint", type=float)
    parser.add_argument("directory")
    args = parser.parse_args()
    _extract(args.object_path, args.generation, args.midpoint, args.directory)