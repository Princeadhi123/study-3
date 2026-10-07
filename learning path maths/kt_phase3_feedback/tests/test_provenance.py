"""Tests for Phase 3 provenance records."""
import tempfile
import hashlib
import json
import unittest
from pathlib import Path

from provenance import file_record, sha256_file


class ProvenanceTests(unittest.TestCase):
    def test_transition_preserves_frozen_sources_and_captures(self):
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads(Path(__file__).with_name(
            "frozen_transition_manifest.json").read_text(encoding="utf-8"))
        required = [root / rel for rel in manifest["files"]]
        required.extend(root / rel for rel in manifest["trees"])
        if not all(path.exists() for path in required):
            self.skipTest("private frozen artifacts not installed")

        def sha(path):
            h = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    h.update(chunk)
            return h.hexdigest()

        for rel, expected in manifest["files"].items():
            self.assertEqual(sha(root / rel), expected, rel)
        for rel, expected in manifest["trees"].items():
            directory = root / rel
            entries = [(p.relative_to(directory).as_posix(), sha(p))
                       for p in sorted(directory.rglob("*"))
                       if p.is_file() and "__pycache__" not in p.parts]
            self.assertEqual(len(entries), expected["file_count"], rel)
            actual = hashlib.sha256(json.dumps(
                entries, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
            self.assertEqual(actual, expected["sha256"], rel)

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
