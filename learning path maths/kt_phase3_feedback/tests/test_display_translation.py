"""Display-layer checks for escape normalization and draft translations.

Loads the translation helpers from ``web_demo/app.js`` (everything before
``const $ =``) into a bare Node ``vm`` context — no DOM, server, model or
capture work — and exercises them against the frozen warm/cold bank and
24-question pool prompts. Skipped when no Node binary is installed.
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "web_demo" / "app.js"
PROMPT_SOURCES = (
    ROOT / "artifacts" / "shadow_smoke_20261006" / "warm_bank_private.json",
    ROOT / "artifacts" / "shadow_smoke_20261006" / "cold_bank_private.json",
    ROOT / "artifacts" / "synthetic_practice_supplement_20261006"
    / "practice_pool_v2_private.json",
)

HARNESS = """
const vm = require("vm");
const fs = require("fs");
const source = fs.readFileSync(process.argv[2], "utf8");
const boundary = "const $ =";
const cut = source.indexOf(boundary);
if (cut < 0) throw new Error("app.js helper boundary not found");
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(source.slice(0, cut), sandbox);
const exprs = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
process.stdout.write(JSON.stringify(exprs.map(
  (expr) => vm.runInContext("(" + expr + ")", sandbox))));
"""

DRAGON_ENGLISH = (
    "Norwegian dragons practise making loops in the air. Younger dragons "
    "can naturally make more consecutive loops than older dragons. The "
    "highest number of consecutive loops is 28. At their best, the oldest "
    "dragons make only one quarter of that record. What is the greatest "
    "number of consecutive loops the old dragons can make?")
FINNISH_MARKERS = ("Sievennä", "luvusta", "jaollinen", "Alkuluku", "Norjalaiset")


def frozen_prompts():
    texts = []
    for path in PROMPT_SOURCES:
        bank = json.loads(path.read_text(encoding="utf-8"))
        texts.extend(question["text"] for question in bank["questions"])
    return texts


class DisplayTranslationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("installed Node required")
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.harness = Path(tmp.name) / "harness.js"
        cls.harness.write_text(HARNESS, encoding="utf-8")
        cls.exprs = Path(tmp.name) / "exprs.json"

    def js(self, *expressions):
        self.exprs.write_text(json.dumps(expressions), encoding="utf-8")
        result = subprocess.run(
            [self.node, str(self.harness), str(APP_JS), str(self.exprs)],
            capture_output=True, text=True, encoding="utf-8",
            check=True, timeout=30)
        return json.loads(result.stdout)

    def question_en(self, text):
        return self.js(f"englishQuestionText({json.dumps({'text': text})})")[0]

    def test_literal_escape_algebra_prompts_translate_exactly(self):
        # Frozen warm prompts carry literal backslash-n pairs.
        escaped = ("Sievennä lauseke:\\n\\n5v + 2v",
                   "Sievennä lauseke:\\n\\n3x + 4x")
        expected = ("Simplify: 5v + 2v", "Simplify: 3x + 4x")
        for raw, wanted in zip(escaped, expected):
            self.assertEqual(self.question_en(raw), wanted)

    def test_real_whitespace_normalizes(self):
        actual = "Sievennä lauseke:\r\n\n  5v + 2v"
        self.assertEqual(self.question_en(actual), "Simplify: 5v + 2v")
        # Actual CR/LF characters.
        out = self.js('normalizeDisplayText("  x\\r\\ny \\n z  ")')[0]
        self.assertEqual(out, "x y z")
        # Literal backslash-r-backslash-n pairs, as stored in the frozen bank.
        literal = self.js('normalizeDisplayText("a\\\\r\\\\nb\\\\nc")')[0]
        self.assertEqual(literal, "a b c")

    @unittest.skipUnless(PROMPT_SOURCES[0].is_file(),
                         "private warm bank fixture required")
    def test_dragon_prompt_translates_exactly(self):
        warm = json.loads(PROMPT_SOURCES[0].read_text(encoding="utf-8"))
        prompt = next(q["text"] for q in warm["questions"]
                      if q["text"].startswith("Norjalaiset"))
        result = self.question_en(prompt)
        self.assertEqual(result, DRAGON_ENGLISH)
        self.assertIn("28", result)
        self.assertIn("one quarter", result)
        self.assertNotIn("7", result)

    def test_finnish_display_normalizes_without_translating(self):
        raw = "Sievennä lauseke:\\n\\n5v + 2v"
        result = self.js(f"normalizeDisplayText({json.dumps(raw)})")[0]
        self.assertEqual(result, "Sievennä lauseke: 5v + 2v")
        dragon = self.js(
            f"normalizeDisplayText({json.dumps('Norjalaiset lohikäärmeet')})")[0]
        self.assertEqual(dragon, "Norjalaiset lohikäärmeet")

    def test_options_normalize_then_translate(self):
        out = self.js(*[
            f"englishOptionText({json.dumps(value)})" for value in
            ("7v", "2,50 €", "neljä\\nsilmukkaa", "1,5")])
        self.assertEqual(
            out, ["7v", "2.50 EUR", "neljä silmukkaa", "1.5"])

    @unittest.skipUnless(all(p.is_file() for p in PROMPT_SOURCES),
                         "private bank/pool fixtures required")
    def test_frozen_research_prompts_render_without_escapes_or_finnish(self):
        texts = frozen_prompts()
        self.assertEqual(len(texts), 104)
        rendered = self.js(*[
            f"englishQuestionText({json.dumps({'text': text})})"
            for text in texts])
        for text, display in zip(texts, rendered):
            self.assertNotIn("\\n", display, text)
            self.assertNotIn("\n", display, text)
            for marker in FINNISH_MARKERS:
                self.assertNotIn(marker, display, text)


if __name__ == "__main__":
    unittest.main()
