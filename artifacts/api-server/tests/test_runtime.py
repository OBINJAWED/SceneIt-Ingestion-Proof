"""Supervisor checks never start services or call external providers."""
import unittest
from unittest.mock import Mock, patch

from sceneit.runtime import commands, supervise


class RuntimeTests(unittest.TestCase):
    def test_worker_and_web_are_distinct_processes(self):
        web, worker = commands()
        self.assertIn("sceneit.server:app", web)
        self.assertEqual(worker[-1], "sceneit.import_worker")
        self.assertNotIn("sceneit.worker", worker)

    def test_child_failure_stops_sibling(self):
        failed, alive = Mock(), Mock()
        failed.poll.return_value = 1
        alive.poll.return_value = None
        with patch("sceneit.runtime.signal.signal"), \
             patch("sceneit.runtime.subprocess.Popen", side_effect=[failed, alive]):
            self.assertEqual(supervise([["web"], ["worker"]]), 1)
        alive.terminate.assert_called_once()
        failed.wait.assert_called_once()