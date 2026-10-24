"""Tests for the explicit artifact-recovery activation path."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import phase3_paths  # noqa: F401
import recovery_runtime
import research_runtime


CONFIG_PATH = phase3_paths.PHASE3_ROOT / ".devin" / "phase3_recovery.json"
ORIGINAL_POOL_SHA = (
    "da12e7195149e1b7d92b2d2363b9b9a7f16de98af003e81387a333ebc77c38c0")
ORIGINAL_POOL_CANONICAL = (
    "ef0d10ced42dcac13e2bf326bb305b0d287900f8ceae3c05fcac7ff5de5c46a4")


def _empty_caches():
    return (
        mock.patch.object(
            research_runtime, "_capture",
            mock.Mock(cache_info=lambda: mock.Mock(currsize=0))),
        mock.patch.object(
            research_runtime, "_bound_bank",
            mock.Mock(cache_info=lambda: mock.Mock(currsize=0))))


class RecoveryActivationTests(unittest.TestCase):
    def setUp(self):
        if not CONFIG_PATH.exists():
            self.skipTest("explicit recovery configuration not installed")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = json.loads(
            CONFIG_PATH.read_text(encoding="utf-8"))

    def _write(self, config):
        path = Path(self.tmp.name) / "recovery_config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def _globals(self):
        return (research_runtime.DEFAULT_CAPTURE,
                research_runtime.POOL_PATH, research_runtime.POOL_HASH,
                research_runtime.RECOVERY_PROVENANCE)

    def _rejected(self, config, currsize=0):
        before = self._globals()
        path = self._write(config)
        capture, bank = _empty_caches()
        if currsize:
            capture = mock.patch.object(
                research_runtime, "_capture",
                mock.Mock(cache_info=lambda: mock.Mock(currsize=1)))
        with capture, bank:
            with self.assertRaises(ValueError):
                recovery_runtime.activate_recovery(path)
        self.assertEqual(self._globals(), before)

    def test_schema_required(self):
        for bad in ({"schema": "bogus"}, {"schema": None}):
            with self.subTest(bad=bad):
                config = dict(self.config, **bad)
                self._rejected(config)

    def test_missing_and_extra_keys_rejected(self):
        for key in self.config:
            config = dict(self.config)
            del config[key]
            with self.subTest(missing=key):
                self._rejected(config)
        self._rejected(dict(self.config, extra_field=True))

    def test_status_must_disclose_unrecovered_history(self):
        for bad in ("fully_recovered", "", None):
            with self.subTest(status=bad):
                self._rejected(dict(self.config, status=bad))

    def test_path_traversal_and_absolute_paths_rejected(self):
        for key in ("practice_pool", "graph_capture", "replay_source",
                    "replay_report", "runtime_root"):
            for bad in ("../escape.json",
                        str(phase3_paths.PHASE3_ROOT.parent /
                            "outside.json")):
                with self.subTest(key=key, value=bad):
                    self._rejected(dict(self.config, **{key: bad}))

    def test_original_identity_mismatch_rejected(self):
        for key, good in (
                ("original_practice_pool_sha256", ORIGINAL_POOL_SHA),
                ("practice_pool_canonical_sha256",
                 ORIGINAL_POOL_CANONICAL)):
            for bad in ("0" * 64, good[::-1]):
                with self.subTest(key=key):
                    self._rejected(dict(self.config, **{key: bad}))

    def test_raw_file_hash_mismatch_rejected(self):
        for key in ("practice_pool_sha256", "graph_sha256"):
            with self.subTest(key=key):
                self._rejected(dict(self.config, **{key: "0" * 64}))

    def test_requires_empty_caches(self):
        self._rejected(dict(self.config), currsize=1)

    def test_activation_loads_recovered_data_in_isolated_process(self):
        script = (
            "import research_content_descriptions_v3 as constants;"
            "import research_runtime;"
            "assert research_runtime.artifact_recovery_provenance() "
            "is None;"
            "from recovery_runtime import activate_recovery;"
            "activate_recovery('.devin/phase3_recovery.json');"
            "pool = research_runtime.load_current_practice_pool();"
            "assert len(pool['questions']) == 24;"
            "bank, _t, prov = research_runtime.load_research_bank"
            "('warm');"
            "assert len(bank['questions']) == 40;"
            "assert prov.get('artifact_recovery');"
            "assert len(research_runtime._capture()['exercises'])"
            " == 104;"
            "assert constants.POOL_HASH == '" + ORIGINAL_POOL_SHA + "';"
            "assert 'recovery_runtime_20261024' not in "
            "str(constants.POOL_PATH);")
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=phase3_paths.PHASE3_ROOT, capture_output=True,
            text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
