import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from wiki_automation import cli


SCRAPE_RESULT = {
    "url": "https://example.org/source",
    "requested_url": "https://example.org/source",
    "reference_url": "https://doi.org/10.1000/example",
    "title": "Example Source Study",
    "authors": "Jane Doe, John Roe",
    "abstract": "Randomized controlled trial showing improved outcomes in adults.",
    "keywords": "nutrition, adults",
    "study_type": "Human Study: Randomized Controlled Trial",
    "pub_date": "2026-03-01",
    "footnote_markdown": "[^1]: **Title:** [Example Source Study](https://doi.org/10.1000/example)<br>",
    "doi": "10.1000/example",
    "pmid": "12345678",
}


class WorkflowCommandTests(unittest.TestCase):
    def test_prepare_packet_creates_stub_and_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir) / "content"
            repo_root.mkdir()
            output_dir = Path(tmpdir) / "out"
            cache_dir = Path(tmpdir) / "cache"

            args = Namespace(
                url="https://example.org/source",
                title=None,
                slug="example-source",
                category="Nutrition",
                article_path=None,
                create_new=True,
                overwrite=False,
                tags=["Research"],
                image_file=None,
                image_screenshot=False,
                image_screenshot_url=None,
                image_screenshot_output=None,
                image_screenshot_full_page=False,
                image_screenshot_annotate=False,
                image_screenshot_wait_ms=1500,
                image_public_id=None,
                image_folder=None,
                limit=5,
                output_dir=str(output_dir),
                match_existing=False,
                alert_name="",
            )

            with patch.object(cli, "REPO_ROOT", repo_root), patch.object(cli, "CONTENT_INDEX_DIR", cache_dir), patch.object(
                cli,
                "scrape_source_packet",
                return_value=SCRAPE_RESULT,
            ), patch.object(cli, "sync_paper_record", return_value={"paper": {"paper_key": "abc"}}), patch.object(
                cli,
                "set_paper_workflow_state",
                return_value={"paper": {"workflow_state": "drafted"}},
            ):
                result = cli.prepare_packet(args)

            created = repo_root / "Nutrition" / "example-source-study.md"
            self.assertEqual(result["created_article_path"], "Nutrition/example-source-study.md")
            self.assertTrue(created.is_file())
            self.assertTrue(Path(result["packet_path"]).is_file())

    def test_append_research_applies_content_and_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir) / "content"
            repo_root.mkdir()
            cache_dir = Path(tmpdir) / "cache"
            article_path = repo_root / "Nutrition" / "alpha.md"
            article_path.parent.mkdir(parents=True)
            article_path.write_text(
                "---\n"
                "title: Alpha\n"
                "tags:\n"
                "  - Research\n"
                "---\n\n"
                "## Key Findings\n\n"
                "Existing content.\n\n"
                "[^1]: **Title:** [Existing](https://example.org/existing)<br>\n",
                encoding="utf-8",
            )

            args = Namespace(
                url="https://example.org/source",
                target="Nutrition/alpha.md",
                section="Key Findings",
                subsection="New Evidence",
                tags=[],
                add_tags=False,
                apply=True,
                commit=False,
            )

            with patch.object(cli, "REPO_ROOT", repo_root), patch.object(cli, "CONTENT_INDEX_DIR", cache_dir), patch.object(
                cli,
                "run_json_tool",
                return_value=SCRAPE_RESULT,
            ), patch.object(cli, "sync_paper_record", return_value={"paper": {"paper_key": "abc"}}), patch.object(
                cli,
                "set_paper_workflow_state",
                return_value={"paper": {"workflow_state": "drafted"}},
            ):
                result = cli.append_research(args)

            updated = article_path.read_text(encoding="utf-8")
            self.assertTrue(result["applied"])
            self.assertIn("### New Evidence", updated)
            self.assertIn("[^2]:", updated)

    def test_publish_pull_request_uses_changed_article_paths(self) -> None:
        args = Namespace(
            base="main",
            branch="agent/test-branch",
            remote="origin",
            title=None,
            body=None,
            fill=False,
            draft=True,
            commit_message=None,
            include_all=False,
            paths=[],
        )

        with patch.object(cli.shutil, "which", return_value="/usr/bin/tool"), patch.object(
            cli,
            "changed_repo_paths",
            return_value=["Nutrition/alpha.md", "notes.txt"],
        ), patch.object(cli, "ensure_branch_checked_out") as ensure_branch, patch.object(
            cli,
            "stage_and_commit_paths",
            return_value="abc123",
        ) as stage_commit, patch.object(cli, "push_branch") as push_branch, patch.object(
            cli,
            "create_pull_request",
            return_value={"url": "https://github.com/example/repo/pull/1", "base": "main", "head": "agent/test-branch", "draft": True},
        ) as create_pr, patch.object(
            cli,
            "update_paper_states_for_paths",
            side_effect=[[{"paper": {"workflow_state": "committed"}}], [{"paper": {"workflow_state": "pr_open"}}]],
        ) as update_states:
            result = cli.publish_pull_request(args)

        ensure_branch.assert_called_once_with("agent/test-branch")
        stage_commit.assert_called_once()
        push_branch.assert_called_once_with("agent/test-branch", "origin")
        create_pr.assert_called_once()
        self.assertEqual(result["paths"], ["Nutrition/alpha.md"])
        self.assertEqual(result["commit_sha"], "abc123")
        self.assertEqual(update_states.call_count, 2)


if __name__ == "__main__":
    unittest.main()
