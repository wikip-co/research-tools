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

    def test_rat_citrus_blend_does_not_match_bergamot_from_generic_terms(self) -> None:
        articles = [
            ArticleRecord(
                path="Natural Healing/Herbs/bergamot.md",
                title="Bergamot",
                stem="bergamot",
                tags=["Citrus", "Metabolic Health"],
                permalink=None,
                body=(
                    "Bergamot is a citrus fruit. Animal studies discuss metabolic "
                    "effects and supplementation in rats."
                ),
            )
        ]
        matches = research_match_candidates(
            articles,
            title=(
                "Effects of a clementine and pink grapefruit blend on metabolic "
                "alterations in rats"
            ),
            abstract=(
                "The clementine and pink grapefruit blend was administered to rats "
                "with diet-induced metabolic alterations."
            ),
            keywords="clementine; pink grapefruit; citrus blend; metabolic",
        )
        self.assertEqual(matches, [])

    def test_compendium_matching_discovers_full_text_entities_without_bergamot(self) -> None:
        entities = ["Clementine", "Grapefruit", "Orange", "Citrus", "Lycopene"]
        articles = [
            ArticleRecord(
                path=f"Natural Healing/{entity.lower()}.md",
                title=entity,
                stem=entity.lower(),
                tags=[],
                permalink=None,
                body=f"Background information about {entity}.",
            )
            for entity in entities
        ]
        articles.append(
            ArticleRecord(
                path="Natural Healing/bergamot.md",
                title="Bergamot",
                stem="bergamot",
                tags=["Citrus"],
                permalink=None,
                body="Bergamot is a citrus fruit.",
            )
        )
        full_text = (
            "Clementine fruit provides flavonoids. Pink grapefruit contains carotenoids. "
            "Orange fruit provides vitamin C. Citrus fruits contain diverse flavonoids. "
            "Lycopene occurs in red-colored foods.\n\n## References\n\n"
            "Bergamot search term citation."
        )
        strict = research_match_candidates(
            articles,
            title="Dietary intervention and metabolic alterations in rats",
            abstract="The intervention was administered during experimental feeding.",
            full_text=full_text,
            include_background=False,
        )
        self.assertEqual(strict, [])

        compendium = research_match_candidates(
            articles,
            title="Dietary intervention and metabolic alterations in rats",
            abstract="The intervention was administered during experimental feeding.",
            full_text=full_text,
            include_background=True,
        )
        self.assertEqual(
            {match["title"] for match in compendium},
            {"Clementine", "Grapefruit", "Orange", "Citrus", "Lycopene"},
        )
        self.assertNotIn("Bergamot", {match["title"] for match in compendium})


if __name__ == "__main__":
    unittest.main()
