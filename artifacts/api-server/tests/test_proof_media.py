"""Provider-free coverage of cached evidence and the hard media boundary."""
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import ANY, Mock, patch

from sceneit.processes import bounded_process
from sceneit.server import create_app


@contextmanager
def rows(value):
    conn = Mock()
    conn.execute.return_value.fetchone.return_value = value
    yield conn


class ProofMediaTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({
            "TESTING": True, "TRUSTED_HOSTS": ["localhost"],
            "PILOT_ALLOWED_SUBJECTS": "pilot", "SESSION_SECRET": "fixture-secret" * 3,
        })
        self.client = self.app.test_client()
        self.session = patch("sceneit.auth._session_from_cookie",
                             return_value={"user_id": "pilot", "csrf_token": "fixture"})
        self.session.start()
        self.addCleanup(self.session.stop)

    def test_missing_still_is_actionable_and_never_extracts_in_http(self):
        with patch("sceneit.proof_media.connection",
                   lambda: rows({"matches": [{"rank": 1}], "object_path": None})), \
                patch("sceneit.proof_media.subprocess.run") as extract:
            response = self.client.get(
                "/api/proof/searches/00000000-0000-0000-0000-000000000001/frames/1")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["code"], "frame_pending")
        self.assertIn("no-store", response.headers["Cache-Control"])
        extract.assert_not_called()

    def test_cached_still_uses_private_storage_not_workspace_source(self):
        with patch("sceneit.proof_media.connection",
                   lambda: rows({"matches": [{}], "object_path": "/private/cache.jpg"})), \
                patch("sceneit.proof_media.open_private", return_value=b"fixture") as storage, \
                patch("sceneit.proof_media.subprocess.run") as extract:
            response = self.client.get(
                "/api/proof/searches/00000000-0000-0000-0000-000000000001/frames/1")
        self.assertEqual(response.status_code, 200)
        storage.assert_called_once_with(
            "/private/cache.jpg", owner_id=None,
            operation_id=ANY, require_membership=False)
        extract.assert_not_called()

    def test_worker_saturation_does_not_spawn_media(self):
        from sceneit.resources import ResourceExhausted
        from sceneit.proof_media import prepare_frames
        with patch("sceneit.proof_media._pending", return_value={"id": "saved"}), \
                patch("sceneit.proof_media.shared_permit", side_effect=ResourceExhausted("media")), \
                patch("sceneit.proof_media.bounded_process") as spawn:
            prepare_frames()
        spawn.assert_not_called()

    def test_process_timeout_terminates_group_before_returning(self):
        started = time.monotonic()
        try:
            result = bounded_process([sys.executable, "-c", "import time; time.sleep(30)"], timeout=.3)
            self.assertNotEqual(result, 0)
        except subprocess.TimeoutExpired:
            pass
        self.assertLess(time.monotonic() - started, 2)

    def test_guard_stops_tool_after_parent_death(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "pid"
            parent = subprocess.Popen([
                sys.executable, "-c",
                "import sys,time; from sceneit.processes import start_guarded; "
                "start_guarded([sys.executable,'-c',"
                "'import os,sys,time,pathlib; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)',"
                "sys.argv[1]], timeout=20); time.sleep(30)",
                str(marker),
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(marker.exists())
                child_pid = int(marker.read_text())
                parent.kill()
                parent.wait()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    stat = Path(f"/proc/{child_pid}/stat")
                    if not stat.exists() or stat.read_text().split()[2] == "Z":
                        break
                    time.sleep(.02)
                else:
                    self.fail("Tool survived parent death")
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait()