"""Tests for Phase 3 provenance records."""
import tempfile
import unittest
from pathlib import Path

from provenance import file_record, sha256_file


class ProvenanceTests(unittest.TestCase):
    def test_file_record_hashes_small_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.txt"
            path.write_text("abc", encoding="utf-8")
            record = file_record(path, "test artifact")
            self.assertEqual(record["size_bytes"], 3)
            self.assertEqual(record["sha256"],
                             "ba7816bf8f01cfea414140de5dae2223"
                             "b00361a396177a9cb410ff61f20015ad")
            self.assertEqual(sha256_file(path, 2),
                             "fb8e20fc2e4c3f248c60c39bd652f3c"
                             "1347298bb977b8b4d5903b85055620603")


if __name__ == "__main__":
    unittest.main()
