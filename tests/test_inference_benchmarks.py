import contextlib
import importlib.util
import io
import json
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location(
    "benchmark_local_inference", SCRIPTS / "benchmark-local-inference.py"
)
extraction = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extraction)


def completion(message):
    return io.BytesIO(json.dumps({
        "choices": [{"message": message, "finish_reason": "stop"}]
    }).encode())


class InferenceBenchmarkTests(unittest.TestCase):
    def animal_result(self, **overrides):
        case = next(case for case in extraction.CASES if case[0] == "animal_scope")
        answer = dict(case[2], source_quote=case[3])
        answer.update(overrides)
        with patch("urllib.request.urlopen", return_value=completion({
            "content": json.dumps(answer)
        })):
            return extraction.run_case(
                "http://unused.invalid/v1", case, "test", 0, "direct"
            )

    def test_extraction_accepts_well_typed_zero_and_false(self):
        record = self.animal_result()
        self.assertEqual(record["score"], record["total"])
        self.assertEqual(record["total"], 7)

    def test_extraction_rejects_boolean_number_substitutions(self):
        for count, flag in [(False, 0), (0.0, 0.0)]:
            with self.subTest(count=count, flag=flag):
                record = self.animal_result(
                    participants_human=count, human_efficacy_demonstrated=flag
                )
                self.assertFalse(record["checks"]["participants_human"])
                self.assertFalse(record["checks"]["human_efficacy_demonstrated"])
                self.assertEqual(record["score"], record["total"] - 2)

    def science_result(self, message):
        with tempfile.TemporaryDirectory() as tmp:
            questions = Path(tmp) / "questions.json"
            output = Path(tmp) / "results.jsonl"
            questions.write_text(json.dumps([{
                "question_id": 1,
                "category": "test",
                "question": "Choose the first option.",
                "options": ["first", "second"],
                "answer": "A",
                "cot_content": "ANSWER_KEY_REASONING_MUST_NOT_BE_SENT",
            }]))
            args = [str(SCRIPTS / "benchmark-science-inference.py"),
                    "--label", "test", "--questions", str(questions),
                    "--output", str(output)]
            with patch.object(sys, "argv", args), patch(
                "urllib.request.urlopen", return_value=completion(message)
            ) as request, contextlib.redirect_stdout(io.StringIO()):
                runpy.run_path(args[0], run_name="__main__")
            sent = json.loads(request.call_args.args[0].data)
            self.assertNotIn("ANSWER_KEY_REASONING_MUST_NOT_BE_SENT", json.dumps(sent))
            self.assertNotIn("answer", sent)
            return json.loads(output.read_text())

    def test_science_accepts_final_json_in_reasoning_when_content_empty(self):
        for content in [None, "", "  "]:
            with self.subTest(content=content):
                record = self.science_result({
                    "content": content, "reasoning_content": '{"answer":"A"}'
                })
                self.assertTrue(record["correct"])
                self.assertEqual(record["answer"], "A")

    def test_science_prefers_content_and_rejects_unstructured_reasoning(self):
        record = self.science_result({
            "content": '{"answer":"B"}', "reasoning_content": '{"answer":"A"}'
        })
        self.assertFalse(record["correct"])
        self.assertEqual(record["answer"], "B")
        record = self.science_result({
            "content": "", "reasoning_content": "My analysis suggests A."
        })
        self.assertFalse(record["correct"])
        self.assertIn("error", record)
