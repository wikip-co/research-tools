import tempfile
import unittest
from pathlib import Path

from gmail_reader.app import (
    WORKFLOW_STATES,
    attach_archive,
    canonicalize_url,
    ensure_db,
    find_paper,
    mark_paper_published,
    paper_identity,
    parse_articles_from_html,
    set_paper_state,
    upsert_external_paper,
)


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


class PaperTrackingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "papers.db"
        ensure_db(self.db_path).close()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_canonicalize_url_strips_query_and_normalizes_host(self) -> None:
        self.assertEqual(
            canonicalize_url("https://www.Example.com/path/to/paper/?utm_source=test"),
            "https://example.com/path/to/paper",
        )

    def test_paper_identity_prefers_doi(self) -> None:
        paper_key, canonical_url, doi, pmid = paper_identity(
            "Example Paper",
            "https://example.com/paper",
            "10.1000/xyz123",
            "",
        )
        self.assertTrue(paper_key)
        self.assertEqual(canonical_url, "https://example.com/paper")
        self.assertEqual(doi, "10.1000/xyz123")
        self.assertEqual(pmid, "")

    def test_upsert_and_update_paper_state(self) -> None:
        created = upsert_external_paper(
            self.db_path,
            title="Example Paper",
            url="https://example.com/paper?utm_campaign=test",
            doi="",
            pmid="",
            workflow_state="scraped",
            matched_content_path="",
        )
        paper_key = created["paper"]["paper_key"]

        marked = mark_paper_published(
            self.db_path,
            identifier="https://example.com/paper",
            matched_content_path="Nutrition/example-paper.md",
            commit="abc123",
            pr="https://github.com/example/repo/pull/1",
        )
        self.assertEqual(marked["paper"]["paper_key"], paper_key)
        self.assertEqual(
            marked["paper"]["matched_content_path"],
            "Nutrition/example-paper.md",
        )

        archived = attach_archive(
            self.db_path,
            identifier=paper_key,
            archive_path="/archive/example-paper/source.pdf",
        )
        self.assertEqual(
            archived["paper"]["archived_source_path"],
            "/archive/example-paper/source.pdf",
        )

        found = find_paper(self.db_path, "https://example.com/paper")
        self.assertTrue(found["found"])
        self.assertEqual(found["paper"]["paper_key"], paper_key)
        self.assertEqual(found["paper"]["workflow_state"], "pr_open")

        merged = set_paper_state(
            self.db_path,
            identifier=paper_key,
            state="merged",
            matched_content_path="Nutrition/example-paper.md",
            commit="abc123",
            pr="https://github.com/example/repo/pull/1",
            archive_path="/archive/example-paper/source.pdf",
        )
        self.assertEqual(merged["paper"]["workflow_state"], "merged")
        self.assertEqual(merged["paper"]["archived_source_path"], "/archive/example-paper/source.pdf")

    def test_parse_scholar_alert_fixture(self) -> None:
        html = (FIXTURES_DIR / "scholar_alert.html").read_text(encoding="utf-8")
        alert_name, candidates = parse_articles_from_html(html, "Resveratrol - new results")
        self.assertEqual(alert_name, "Resveratrol")
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0].status, "selected")
        self.assertEqual(candidates[0].article_url, "https://example.org/rct")
        self.assertIn(candidates[1].status, {"review", "rejected"})

    def test_workflow_state_enum_exposes_expected_values(self) -> None:
        self.assertEqual(WORKFLOW_STATES[0], "discovered")
        self.assertIn("merged", WORKFLOW_STATES)


if __name__ == "__main__":
    unittest.main()
