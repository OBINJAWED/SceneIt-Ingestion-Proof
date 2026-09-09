"""Deterministic media-inspection tests with bounded tools mocked."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sceneit.inspect_media import MediaInspectionError, inspect_mp4


def completed(body=b"", returncode=0):
    return unittest.mock.Mock(stdout=body, stderr=b"", returncode=returncode)


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "source.bin"
        self.path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 100)
        self.metadata = {
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                       "duration": "12.5"},
            "streams": [
                {"codec_type": "video", "codec_name": "h264",
                 "pix_fmt": "yuv420p", "width": 1280, "height": 720,
                 "avg_frame_rate": "30/1"},
                {"codec_type": "audio", "codec_name": "aac"},
            ],
        }

    def tearDown(self):
        self.temporary.cleanup()

    def test_valid_mp4_returns_stable_contract(self):
        calls = [completed(json.dumps(self.metadata).encode()), completed()]
        with patch("sceneit.inspect_media._run", side_effect=calls):
            result = inspect_mp4(self.path)
        self.assertEqual(result["duration"], 12.5)
        self.assertEqual(result["videoCodec"], "h264")
        self.assertEqual(result["audioCodec"], "aac")
        self.assertEqual(result["sha256"],
                         hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_silent_visual_mp4_is_supported(self):
        self.metadata["streams"] = self.metadata["streams"][:1]
        with patch("sceneit.inspect_media._run", side_effect=[
                completed(json.dumps(self.metadata).encode()), completed()]):
            result = inspect_mp4(self.path)
        self.assertFalse(result["hasAudio"])
        self.assertIsNone(result["audioCodec"])

    def test_filename_does_not_substitute_for_container(self):
        fake = Path(self.temporary.name) / "looks-valid.mp4"
        fake.write_bytes(b"not an mp4")
        with self.assertRaises(MediaInspectionError):
            inspect_mp4(fake)

    def test_rejects_multiple_video_and_unsupported_codec(self):
        cases = []
        duplicate = json.loads(json.dumps(self.metadata))
        duplicate["streams"].append(dict(duplicate["streams"][0]))
        cases.append(duplicate)
        hevc = json.loads(json.dumps(self.metadata))
        hevc["streams"][0]["codec_name"] = "hevc"
        cases.append(hevc)
        for metadata in cases:
            with self.subTest(metadata=metadata), \
                    patch("sceneit.inspect_media._run",
                          return_value=completed(json.dumps(metadata).encode())), \
                    self.assertRaises(MediaInspectionError):
                inspect_mp4(self.path)

    def test_rejects_provider_duration_limits(self):
        for value in ("3.9", "14401"):
            metadata = json.loads(json.dumps(self.metadata))
            metadata["format"]["duration"] = value
            with self.subTest(value=value), patch(
                    "sceneit.inspect_media._run",
                    return_value=completed(json.dumps(metadata).encode())), \
                    self.assertRaises(MediaInspectionError):
                inspect_mp4(self.path)


class GeneratedMediaTests(unittest.TestCase):
    """Exercise the real local parser/decoder with rights-free generated media."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise unittest.SkipTest("ffmpeg and ffprobe are required")
        cls.temporary = tempfile.TemporaryDirectory()
        cls.silent = Path(cls.temporary.name) / "silent.mp4"
        cls.audio = Path(cls.temporary.name) / "audio.mp4"
        common = [
            "ffmpeg", "-v", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=360x360:r=24:d=4",
        ]
        subprocess.run(
            common + ["-threads", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                      "-movflags", "+faststart", str(cls.silent)],
            check=True, timeout=30,
        )
        subprocess.run(
            common + ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                      "-t", "4", "-threads", "1", "-c:v", "libx264",
                      "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
                      "-movflags", "+faststart", str(cls.audio)],
            check=True, timeout=30,
        )

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_real_generated_silent_h264_mp4(self):
        result = inspect_mp4(self.silent)
        self.assertFalse(result["hasAudio"])
        self.assertEqual((result["width"], result["height"]), (360, 360))

    def test_real_generated_h264_aac_mp4(self):
        result = inspect_mp4(self.audio)
        self.assertTrue(result["hasAudio"])
        self.assertEqual(result["audioCodec"], "aac")


if __name__ == "__main__":
    unittest.main()