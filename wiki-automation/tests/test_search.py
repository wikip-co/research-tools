import unittest

from wiki_automation.cli import ArticleRecord, research_match_candidates, search_articles


class SearchArticlesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.articles = [
            ArticleRecord(
                path="Child Development/Birth/Postpartum/hypertension.md",
                title="Hypertension",
                stem="hypertension",
                tags=["Cardiovascular Health"],
                permalink="postpartum-hypertension/",
                body="Postpartum hypertension is high blood pressure after pregnancy.",
            ),
            ArticleRecord(
                path="Natural Healing/Complex Carbohydrates/resveratrol.md",
                title="Resveratrol",
                stem="resveratrol",
                tags=["Hypertension"],
                permalink="resveratrol/",
                body="Resveratrol may support blood pressure.",
            ),
            ArticleRecord(
                path="Nutrition/pea-protein.md",
                title="Pea Protein",
                stem="pea-protein",
                tags=["Protein"],
                permalink=None,
                body="A food article without cardiovascular content.",
            ),
        ]

    def test_all_mode_requires_full_query_coverage_across_article(self) -> None:
        matches = search_articles(self.articles, "postpartum hypertension", match_mode="all")

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["title"], "Hypertension")
        self.assertIn("path", matches[0]["matched_fields"])
        self.assertIn("permalink", matches[0]["matched_fields"])
        self.assertIn("body", matches[0]["matched_fields"])

    def test_phrase_mode_finds_exact_normalized_phrase(self) -> None:
        matches = search_articles(self.articles, "postpartum hypertension", match_mode="phrase")

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["matched_terms"], ["postpartum hypertension"])

    def test_field_restriction_limits_matches(self) -> None:
        title_matches = search_articles(
            self.articles,
            "hypertension",
            match_mode="all",
            fields=["title"],
        )
        tag_matches = search_articles(
            self.articles,
            "hypertension",
            match_mode="all",
            fields=["tags"],
        )

        self.assertEqual([match["title"] for match in title_matches], ["Hypertension"])
        self.assertEqual([match["title"] for match in tag_matches], ["Resveratrol"])

    def test_research_match_prefers_source_entity_over_paper_title_shape(self) -> None:
        articles = [
            ArticleRecord(
                path="Natural Healing/Chemicals/quercetin.md",
                title="Quercetin",
                stem="quercetin",
                tags=["Antioxidant", "Inflammation"],
                permalink=None,
                body="Quercetin is a plant flavonol.",
            ),
            ArticleRecord(
                path="Technology/continuous-delivery.md",
                title="Continuous Delivery",
                stem="continuous-delivery",
                tags=["Software"],
                permalink=None,
                body="Signals and modulators in delivery systems.",
            ),
        ]
        matches = research_match_candidates(
            articles,
            title="From antioxidant to signal modulator: the expanding role of quercetin",
            abstract="Quercetin has antioxidant and anti-inflammatory effects.",
            alert_name="Quercetin",
        )
        self.assertEqual(matches[0]["path"], "Natural Healing/Chemicals/quercetin.md")


if __name__ == "__main__":
    unittest.main()
