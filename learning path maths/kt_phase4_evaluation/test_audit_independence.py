"""Fail-closed audit guards without reading private research data."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from audit_independence import audit


class AuditGuardsTest(unittest.TestCase):
    def test_existing_audit_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "audit.json"
            output.write_text("original", encoding="utf-8")
            with patch("audit_independence.load_json") as load:
                with self.assertRaisesRegex(FileExistsError, "overwrite"):
                    audit(Path(tmp), output)
                load.assert_not_called()
            self.assertEqual(output.read_text(encoding="utf-8"), "original")

    def test_changed_frozen_input_stops_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "audit.json"
            manifest = {"inputs": {"sequences": {"sha256": "expected"}}}
            with patch("audit_independence.load_json", return_value=manifest), \
                    patch("audit_independence.digest", return_value="changed"):
                with self.assertRaisesRegex(ValueError, "Frozen input changed: sequences"):
                    audit(Path(tmp), output)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
