"""Regression tests for the requires_private_artifacts skip helper."""
import tempfile
import unittest
from pathlib import Path

from tests.helpers import requires_private_artifacts


def _run(case):
    result = unittest.TestResult()
    case.run(result)
    return result


class RequiresPrivateArtifactsTests(unittest.TestCase):
    def test_missing_file_skips_decorated_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            absent = Path(tmp) / "absent.json"

            class Missing(unittest.TestCase):
                @requires_private_artifacts(absent)
                def runTest(self):
                    self.fail("must not run")

            result = _run(Missing())
            self.assertEqual(result.testsRun, 1)
            self.assertEqual(len(result.skipped), 1)
            self.assertFalse(result.failures)
            self.assertFalse(result.errors)

    def test_existing_file_executes_and_failure_not_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            present = Path(tmp) / "present.json"
            present.write_text("{}", encoding="utf-8")

            class Failing(unittest.TestCase):
                @requires_private_artifacts(present)
                def runTest(self):
                    self.fail("inner failure must surface")

            result = _run(Failing())
            self.assertEqual(result.testsRun, 1)
            self.assertEqual(len(result.failures), 1)
            self.assertFalse(result.skipped)

    def test_existing_file_executes_and_error_not_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            present = Path(tmp) / "present.json"
            present.write_text("{}", encoding="utf-8")

            class Erroring(unittest.TestCase):
                @requires_private_artifacts(present)
                def runTest(self):
                    raise ValueError("inner error must surface")

            result = _run(Erroring())
            self.assertEqual(result.testsRun, 1)
            self.assertEqual(len(result.errors), 1)
            self.assertFalse(result.skipped)

    def test_all_supplied_paths_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            present = Path(tmp) / "present.json"
            present.write_text("{}", encoding="utf-8")
            absent = Path(tmp) / "absent.json"

            class Partial(unittest.TestCase):
                @requires_private_artifacts(present, absent)
                def runTest(self):
                    self.fail("must not run")

            result = _run(Partial())
            self.assertEqual(len(result.skipped), 1)
            self.assertIn("absent.json",
                          result.skipped[0][1])


if __name__ == "__main__":
    unittest.main()
