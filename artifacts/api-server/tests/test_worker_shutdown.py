"""Offline graceful-shutdown coverage for the existing worker runtime."""
import signal
import threading
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

from sceneit import import_worker


class WorkerShutdownTests(unittest.TestCase):
    def test_idle_stop_unwinds_worker_permit_and_advisory_guard(self):
        stop = threading.Event()
        guard = Mock()
        with patch.object(import_worker, "acquire", return_value="holder"), \
                patch.object(import_worker, "release") as release, \
                patch.object(import_worker, "worker_lock", return_value=nullcontext(guard)), \
                patch.object(import_worker, "heartbeat"), \
                patch.object(import_worker, "cleanup_expired"), \
                patch.object(import_worker, "claim_job", return_value=None) as claim, \
                patch.object(import_worker, "prepare_proof_frames", side_effect=stop.set), \
                patch.object(import_worker, "process_job") as process:
            import_worker.run_forever(stop)
        claim.assert_called_once()
        process.assert_not_called()
        release.assert_called_once_with("import_worker", "holder")
        self.assertIsNone(import_worker._active_worker_guard)

    def test_signal_requests_stop_and_restores_previous_handlers(self):
        handlers = {}
        previous = Mock()

        def register(signum, handler):
            handlers[signum] = handler
            return previous

        def run(stop):
            self.assertFalse(stop.is_set())
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            self.assertTrue(stop.is_set())

        with patch.object(import_worker.signal, "signal", side_effect=register), \
                patch.object(import_worker, "run_forever", side_effect=run):
            import_worker.main()
        self.assertIs(handlers[signal.SIGTERM], previous)
        self.assertIs(handlers[signal.SIGINT], previous)


if __name__ == "__main__":
    unittest.main()