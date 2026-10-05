"""Regression cases for the conservative bank screen (no real data or model)."""
import unittest
from fractions import Fraction

from audit_stronger_banks import check_question, expression, representatives


def question(text, options, answer_index):
    return {"text": text, "options": options, "answer_index": answer_index}


class ContentChecks(unittest.TestCase):
    def test_explicit_fraction_and_decimal_arithmetic(self):
        self.assertEqual(expression("4\u20446 \u00b7 5\u20444"), Fraction(5, 6))
        self.assertEqual(expression("0,1 + 0,2"), Fraction(3, 10))
        q = question("4\u20446 \u00b7 5\u20444",
                     ["18\u204424", "18\u204430", "20\u204424"], 2)
        self.assertEqual(check_question(q)[0], "fraction_arithmetic")

    def test_bad_key_and_equivalent_numeric_options_rejected(self):
        for q in (
            question("50 % luvusta 100", ["50", "100", "25"], 1),
            question("1/2 * 2", ["1", "2/2", "2"], 0),
        ):
            with self.assertRaises(ValueError):
                check_question(q)

    def test_nonsemantic_and_two_choice_options_rejected(self):
        for options in (["tosi", "valhe", "ehk\u00e4", "\u2665"], ["1", "2"]):
            with self.assertRaises(ValueError):
                check_question(question("1/2 = 1/2", options, 0))

    def test_missing_context_and_ambiguous_mixed_numbers_not_accepted(self):
        for text in ("55 ja 65", "korkeus on 10 cm ja kanta 5 cm.",
                     "Kuinka suuri on kulma alpha?", "Ota 2\u20443 luvusta 4"):
            with self.assertRaises(LookupError):
                check_question(question(text, ["1", "2", "3"], 0))

    def test_signed_numbers_and_explicit_query(self):
        cases = [
            question("luvun -8 vastaluku", ["8", "-4", "-8", "4"], 0),
            question("Mik\u00e4 on luvun -5 itseisarvo, eli |-5|?",
                     ["-5", "10", "-10", "5"], 3),
            question("x = 10 - 2", ["2", "3", "5", "8"], 3),
            question("1 < x < 3", ["1", "3", "4", "2"], 3),
            question("Kuinka paljon on 1 % luvusta 1000?", ["1", "100", "1000", "10"], 3),
        ]
        for q in cases:
            with self.subTest(text=q["text"]):
                self.assertTrue(check_question(q)[1])

    def test_inconsistent_instructions_and_arbitrary_code_rejected(self):
        with self.assertRaises(ValueError):
            check_question(question("Mik\u00e4 on luvun -5 itseisarvo, eli |-4|?",
                                    ["-5", "4", "5"], 2))
        for text in ("__import__('os')", "2**3", "2 ^ 3"):
            with self.assertRaises((ValueError, SyntaxError)):
                expression(text)


class SelectionChecks(unittest.TestCase):
    def test_exact_variant_support_not_pooled_and_outcomes_not_ranked(self):
        def row(qid, text, students, correct):
            return {"question": {"question_id": qid, "text": text},
                    "support": {"students": students, "with_history_students": students,
                                "n": students, "correct": correct}}
        rows = [row("a", " Same prompt ", 3, 0),
                row("b", "same prompt", 4, 4),
                row("c", "another prompt", 2, 2)]
        picked = representatives(rows, 2)
        self.assertEqual([r["question"]["question_id"] for r in picked], ["b", "c"])
        self.assertEqual(representatives(rows, 5), [])
        rows[1]["support"]["correct"] = 0
        self.assertEqual(representatives(rows, 2), picked)


if __name__ == "__main__":
    unittest.main()
