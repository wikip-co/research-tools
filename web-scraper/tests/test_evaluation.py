import json
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import evaluate

FIXTURES = Path(__file__).parent / "fixtures"


class EvaluationTests(unittest.TestCase):
    def test_offline_comparison_uses_real_extraction_and_no_network(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(evaluate.scraper, "fetch_with_scrapling") as network,
            patch.object(
                evaluate.scraper,
                "fetch_json",
                side_effect=AssertionError("network disabled"),
            ),
        ):
            output = Path(temp) / "report.json"
            evaluate.main([str(FIXTURES / "evaluation.json"), "--output", str(output)])
            report = json.loads(output.read_text())
            self.assertEqual(len(report["results"]), 12)
            self.assertEqual(report["summary"]["fallback_recoveries"], 2)
            self.assertEqual(report["mode"], "offline")
            self.assertNotIn("body_markdown", output.read_text())
            self.assertNotIn("Synthetic study methods", output.read_text())
            self.assertTrue(output.with_suffix(".md").is_file())
        network.assert_not_called()

    def test_live_requires_explicit_flag(self):
        manifest = Path(__file__).parents[1] / "evaluation/publishers.json"
        with self.assertRaises(SystemExit):
            evaluate.main([str(manifest), "--output", "/tmp/not-written.json"])

    def test_live_requires_access_review(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = Path(temp) / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "mode": "live",
                        "cases": [{"id": "unreviewed", "url": "https://example.org"}],
                    }
                )
            )
            output = Path(temp) / "report.json"
            with (
                patch.object(evaluate, "robots_policy") as robots,
                patch.object(evaluate, "isolated_live_attempt") as live,
            ):
                evaluate.main([str(manifest), "--live", "--output", str(output)])
            robots.assert_not_called()
            live.assert_not_called()
            self.assertEqual(
                json.loads(output.read_text())["results"][0]["skipped"],
                "access_not_reviewed",
            )

    def test_robots_block_prevents_every_backend(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = Path(temp) / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "mode": "live",
                        "cases": [
                            {
                                "id": "blocked",
                                "url": "https://example.org",
                                "access_reviewed": True,
                            }
                        ],
                    }
                )
            )
            with (
                patch.object(
                    evaluate,
                    "robots_policy",
                    return_value=(False, 2, "robots_disallowed"),
                ),
                patch.object(evaluate, "isolated_live_attempt") as live,
            ):
                evaluate.main(
                    [
                        str(manifest),
                        "--live",
                        "--output",
                        str(Path(temp) / "report.json"),
                    ]
                )
            live.assert_not_called()

    def test_errors_are_sanitized(self):
        case = {
            "id": "error",
            "url": "https://example.org",
            "fixtures": {"scrapling": "error:token=SECRET"},
        }
        result = evaluate.evaluate_one(case, "scrapling", FIXTURES)
        self.assertEqual(result["failure_class"], "fetch_error")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_expected_identity_is_independent_of_content_length(self):
        case = {
            "id": "wrong-article",
            "url": "https://example.org",
            "expected_doi": "10.1000/wrong",
            "fixtures": {"scrapling": "complete_article.html"},
        }
        result = evaluate.evaluate_one(case, "scrapling", FIXTURES)
        self.assertFalse(result["valid_article"])
        self.assertIn("expected_doi_mismatch", result["issues"])

    def test_skipped_backends_are_excluded_from_denominator(self):
        summary = evaluate.summarize(
            [
                {
                    "case": "skipped",
                    "backend": "scrapling",
                    "skipped": "robots",
                    "valid_article": None,
                }
            ]
        )
        self.assertEqual(summary["scrapling_failures"], 0)
        self.assertIsNone(summary["fallback_recovery_rate"])

    def test_pdf_is_reported_as_shared_pipeline(self):
        result = evaluate.evaluate_one(
            {"id": "pdf", "url": "https://example.org/paper.pdf"}, "scrapling", FIXTURES
        )
        self.assertEqual(result["skipped"], "pdf_uses_shared_pipeline")

    def test_html_error_is_not_a_robots_policy(self):
        response = MagicMock()
        response.read.return_value = b"<html>Unavailable</html>"
        response.headers = Message()
        response.headers["Content-Type"] = "text/html"
        with (
            patch.object(evaluate, "validate_public_url", side_effect=lambda url: url),
            patch.object(evaluate, "public_urlopen") as fetch,
        ):
            fetch.return_value.__enter__.return_value = response
            self.assertEqual(
                evaluate.robots_policy("https://example.org/article")[2],
                "robots_unavailable",
            )
