import tempfile
import unittest
from pathlib import Path

from wiki_automation.cli import build_markdown_article, parse_markdown_article


class FrontmatterRoundTripTests(unittest.TestCase):
    def test_yaml_frontmatter_round_trip_preserves_tags_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "article.md"
            path.write_text(
                build_markdown_article(
                    {
                        "title": "Example Article",
                        "tags": ["Research", "Nutrition"],
                        "image": "example-image",
                    },
                    "## Notes\n\nBody content.\n",
                ),
                encoding="utf-8",
            )

            metadata, body = parse_markdown_article(path)
            self.assertEqual(metadata["title"], "Example Article")
            self.assertEqual(metadata["tags"], ["Research", "Nutrition"])
            self.assertEqual(metadata["image"], "example-image")
            self.assertIn("Body content.", body)


if __name__ == "__main__":
    unittest.main()
