"""Deterministic platform boundary tests; no network is used."""
import http.client
import unittest
from unittest.mock import patch

from sceneit.platforms import (FileRequired, PlatformError, canonicalize_link,
                               resolve_link, _safe_youtube_dl)
from sceneit.safe_network import (NetworkSafetyError, bounded_extractor_network,
                                  public_addresses)


class PlatformTests(unittest.TestCase):
    def test_canonicalizes_supported_single_video_links(self):
        cases = {
            "https://youtu.be/vLqagjJAvU8?t=4":
                ("youtube", "https://www.youtube.com/watch?v=vLqagjJAvU8"),
            "https://www.youtube.com/shorts/vLqagjJAvU8":
                ("youtube", "https://www.youtube.com/watch?v=vLqagjJAvU8"),
            "https://twitter.com/person/status/123456789":
                ("x", "https://x.com/person/status/123456789"),
            "https://www.tiktok.com/@person/video/123456789":
                ("tiktok", "https://www.tiktok.com/@person/video/123456789"),
            "https://vt.tiktok.com/ZShare123/":
                ("tiktok", "https://vt.tiktok.com/ZShare123/"),
            "https://www.tiktok.com/t/ZShare123/":
                ("tiktok", "https://www.tiktok.com/t/ZShare123/"),
            "https://player.vimeo.com/video/123456789":
                ("vimeo", "https://vimeo.com/123456789"),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                result = canonicalize_link(raw)
                self.assertEqual((result["sourceKind"], result["sourceUrl"]), expected)

    def test_rejects_unsafe_unsupported_and_collection_links(self):
        for raw in (
            "http://youtu.be/vLqagjJAvU8",
            "https://youtube.com/playlist?list=abc",
            "https://youtube.com/watch?v=vLqagjJAvU8&list=abc",
            "https://youtube.com/shorts/vLqagjJAvU8?list=abc",
            "https://youtu.be/vLqagjJAvU8?list=abc",
            "https://vimeo.com/showcase/12345",
            "https://x.com/person",
            "https://example.com/video/123",
            "https://user:pass@vimeo.com/12345",
        ):
            with self.subTest(raw=raw), self.assertRaises(PlatformError):
                canonicalize_link(raw)

    def test_youtube_never_constructs_downloader(self):
        source = canonicalize_link("https://youtu.be/vLqagjJAvU8")
        with patch.dict("sys.modules", {"yt_dlp": unittest.mock.Mock()}), \
                self.assertRaises(FileRequired):
            resolve_link(source, "/tmp/never-created.mp4")

    def test_private_dns_answers_are_rejected(self):
        resolver = lambda *args, **kwargs: [
            (2, 1, 6, "", ("169.254.169.254", 443))
        ]
        with self.assertRaises(NetworkSafetyError):
            public_addresses("video.twimg.com", resolver=resolver)

    def test_extractor_request_guard_accepts_only_allowlisted_https(self):
        # Use the actual stdlib HTTPS connection class used by yt-dlp's forced
        # Urllib handler, while replacing its send method before the guard is
        # installed so this deterministic test performs no network access.
        with patch.object(http.client.HTTPConnection, "request",
                          autospec=True, return_value=None) as send:
            with bounded_extractor_network(("vimeo.com",)):
                http.client.HTTPSConnection(
                    "player.vimeo.com", 443).request("GET", "/video/123")
                with self.assertRaises(NetworkSafetyError):
                    http.client.HTTPConnection(
                        "vimeo.com", 80).request("GET", "/video/123")
                with self.assertRaises(NetworkSafetyError):
                    http.client.HTTPSConnection(
                        "evil.example", 443).request("GET", "/redirect")
                with self.assertRaises(NetworkSafetyError):
                    http.client.HTTPSConnection(
                        "vimeo.com", 443).request(
                            "GET", "https://user:pass@vimeo.com/video/123")
        send.assert_called_once()

    def test_installed_extractor_is_forced_to_urllib_only(self):
        with _safe_youtube_dl({
                "quiet": True, "ignoreconfig": True, "proxy": "",
                "cookiefile": None, "usenetrc": False, "plugin_dirs": [],
        }) as ydl:
            self.assertEqual(
                set(ydl._request_director.handlers),
                {"Urllib"})

    def test_ambiguous_multi_video_requires_file(self):
        info = {
            "_type": "playlist",
            "entries": [{"id": "one"}, {"id": "two"}],
        }
        source = canonicalize_link("https://x.com/person/status/123456789")
        with patch("sceneit.platforms._isolated_extract", return_value=info), \
                self.assertRaises(PlatformError) as raised:
            resolve_link(source, "/tmp/never-created.mp4")
        self.assertEqual(raised.exception.code, "ambiguous_media")

    def test_silent_progressive_mp4_is_a_real_direct_attempt(self):
        info = {
            "id": "123456789", "title": "Silent clip",
            "formats": [{
                "url": "https://video.twimg.com/clip.mp4", "ext": "mp4",
                "protocol": "https", "vcodec": "h264", "acodec": "none",
                "height": 720, "width": 1280,
            }],
        }
        source = canonicalize_link("https://x.com/person/status/123456789")
        with patch("sceneit.platforms._isolated_extract", return_value=info), \
                patch("sceneit.platforms.download_https") as download:
            result = resolve_link(source, "/tmp/direct-silent.mp4")
        download.assert_called_once()
        self.assertEqual(result["title"], "Silent clip")

    def test_live_restricted_and_non_video_are_rejected(self):
        source = canonicalize_link("https://vimeo.com/123456789")
        cases = (
            ({"is_live": True, "formats": []}, "live_unsupported"),
            ({"has_drm": True, "formats": []}, "restricted_media"),
            ({"formats": []}, "non_video"),
        )
        for info, code in cases:
            with self.subTest(code=code), \
                    patch("sceneit.platforms._isolated_extract", return_value=info), \
                    self.assertRaises(PlatformError) as raised:
                resolve_link(source, "/tmp/never-created.mp4")
            self.assertEqual(raised.exception.code, code)


if __name__ == "__main__":
    unittest.main()