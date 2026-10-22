"""Deterministic offline tests for tests/browser_runtime.py."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.browser_runtime import (
    cleanup_browser_directory, read_devtools_port)


class ReadDevtoolsPortTests(unittest.TestCase):
    def test_reads_port_from_real_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            (profile / "DevToolsActivePort").write_text(
                "8123\n/devtools/browser/abc\n", encoding="utf-8")
            self.assertEqual(read_devtools_port(profile), "8123")

    def test_retries_after_permission_error_then_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            target = profile / "DevToolsActivePort"
            target.write_text("9999\n", encoding="utf-8")
            original = Path.read_text
            calls = []

            def spy(path, *args, **kwargs):
                if path == target and not calls:
                    calls.append(1)
                    raise PermissionError("locked")
                return original(path, *args, **kwargs)

            with mock.patch.object(Path, "read_text", autospec=True,
                                   side_effect=spy), \
                    mock.patch("tests.browser_runtime.time.sleep"):
                self.assertEqual(read_devtools_port(profile), "9999")
            self.assertEqual(calls, [1])

    def test_missing_then_empty_then_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            target = profile / "DevToolsActivePort"
            target.write_text("7777\n", encoding="utf-8")
            original = Path.read_text
            calls = []

            def spy(path, *args, **kwargs):
                if path == target:
                    calls.append(1)
                    if len(calls) == 1:
                        raise FileNotFoundError("absent")
                    if len(calls) == 2:
                        return ""
                return original(path, *args, **kwargs)

            with mock.patch.object(Path, "read_text", autospec=True,
                                   side_effect=spy), \
                    mock.patch("tests.browser_runtime.time.sleep"):
                self.assertEqual(read_devtools_port(profile), "7777")
            self.assertEqual(len(calls), 3)

    def test_permanent_permission_error_times_out_chained(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            with mock.patch.object(
                    Path, "read_text", autospec=True,
                    side_effect=PermissionError("locked")), \
                    mock.patch("tests.browser_runtime.time.sleep"), \
                    mock.patch("tests.browser_runtime.time.monotonic",
                               side_effect=[0.0, 5.0, 15.0]):
                with self.assertRaises(TimeoutError) as ctx:
                    read_devtools_port(profile, timeout=10.0)
            self.assertIsInstance(ctx.exception.__cause__, PermissionError)
            self.assertIn("DevToolsActivePort", str(ctx.exception))


class CleanupBrowserDirectoryTests(unittest.TestCase):
    def test_real_directory_cleanup_succeeds(self):
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name)
        cleanup_browser_directory(tmp)
        self.assertFalse(path.exists())

    def test_transient_permission_error_retries(self):
        tmp = mock.Mock()
        tmp.cleanup.side_effect = [PermissionError("busy"), None]
        with mock.patch("tests.browser_runtime.time.sleep"), \
                mock.patch("tests.browser_runtime.time.monotonic",
                           side_effect=[0.0, 0.1]):
            cleanup_browser_directory(tmp, timeout=5.0)
        self.assertEqual(tmp.cleanup.call_count, 2)

    def test_permanent_permission_error_propagates(self):
        tmp = mock.Mock()
        tmp.cleanup.side_effect = PermissionError("busy")
        with mock.patch("tests.browser_runtime.time.sleep"), \
                mock.patch("tests.browser_runtime.time.monotonic",
                           side_effect=[0.0, 6.0]):
            with self.assertRaises(PermissionError):
                cleanup_browser_directory(tmp, timeout=5.0)
        self.assertEqual(tmp.cleanup.call_count, 1)

    def test_other_errors_not_retried(self):
        tmp = mock.Mock()
        tmp.cleanup.side_effect = OSError("not a lock")
        with self.assertRaises(OSError):
            cleanup_browser_directory(tmp)
        self.assertEqual(tmp.cleanup.call_count, 1)


if __name__ == "__main__":
    unittest.main()
