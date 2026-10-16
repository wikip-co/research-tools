import unittest

from wiki_automation.local_publish import (
    inserted_section_lines,
    rendered_structure_issues,
    split_multi_sentence_bullet,
)

SOURCE = (
    "Despite these findings, considerable heterogeneity among the included studies "
    "warrants cautious interpretation of the results. Nonetheless, the observed "
    "lipid-modulating effects suggest that vitamin E may reduce cardiovascular risk "
    "in diabetic populations."
)
PACKET = {"markdown": SOURCE, "abstract": SOURCE, "study_type": "Meta-Analysis"}


class SplitMultiSentenceBulletTests(unittest.TestCase):
    def test_splits_into_exact_single_sentence_quotes(self):
        item = {"text": SOURCE, "source_quote": SOURCE, "claim_kind": "source_finding"}
        pieces = split_multi_sentence_bullet(item, PACKET)
        self.assertEqual(len(pieces), 2)
        for piece in pieces:
            self.assertIn(piece["source_quote"], SOURCE)
            self.assertEqual(piece["text"], piece["source_quote"])
            self.assertEqual(piece["claim_kind"], "source_finding")

    def test_single_sentence_unchanged(self):
        sentence = SOURCE.split(". ")[0] + "."
        item = {"text": sentence, "source_quote": sentence}
        self.assertEqual(split_multi_sentence_bullet(item, PACKET), [item])

    def test_unprovable_piece_leaves_bullet_unchanged(self):
        item = {
            "text": "Vitamin E cured every participant in the trial. It also reversed diabetes entirely.",
            "source_quote": "Vitamin E cured every participant in the trial. It also reversed diabetes entirely.",
        }
        self.assertEqual(split_multi_sentence_bullet(item, PACKET), [item])


class NormalizeSplitEntityTests(unittest.TestCase):
    def test_split_sentence_without_entity_is_dropped(self):
        from wiki_automation.local_publish import normalize_simple_draft_plan

        plan = {
            "target_proposals": [
                {
                    "operation": "append_existing",
                    "target_entity": "vitamin E",
                    "heading": "## Research",
                    "bullets": [
                        {"text": SOURCE, "source_quote": SOURCE, "claim_kind": "source_finding"}
                    ],
                }
            ]
        }
        actions = normalize_simple_draft_plan(plan, packet=PACKET)
        bullets = plan["target_proposals"][0]["bullets"]
        self.assertEqual(len(bullets), 1)
        self.assertIn("vitamin E", bullets[0]["text"])
        self.assertIn(bullets[0]["source_quote"], SOURCE)
        kinds = {action["action"] for action in actions}
        self.assertIn("split_multi_sentence_bullet", kinds)
        self.assertIn("dropped_split_sentence_without_entity", kinds)


class InsertedSectionScopeTests(unittest.TestCase):
    def test_only_new_lines_are_checked(self):
        original = [
            "",
            "### Skin",
            "- Existing human text has two sentences here. It predates the rule entirely.[^1]",
            "",
        ]
        new_block = ["Summary sentence for the new block.[^2]", "", "### Lipid Metabolism", "", "- One new sentence about lipids.[^2]"]
        updated = original + new_block
        lines = inserted_section_lines(original, updated)
        self.assertEqual(lines, new_block)
        plan = {"section_summary": {"text": "Summary sentence for the new block."}, "bullets": []}
        self.assertEqual(rendered_structure_issues(lines, PACKET, plan), [])

    def test_new_multi_sentence_bullet_still_rejected(self):
        original = ["", "### Skin", "- Old.[^1]"]
        updated = original + ["", "### Lipids", "", "- First new sentence about lipids here. Second new sentence about lipids here.[^2]"]
        lines = inserted_section_lines(original, updated)
        self.assertIn("bullet_not_single_idea", rendered_structure_issues(lines, PACKET, {"bullets": []}))

    def test_misplaced_summary_in_new_block_still_rejected(self):
        original = ["", "### Skin", "- Old.[^1]"]
        updated = original + ["", "### Lipids", "", "- One new sentence about lipids.[^2]", "Late summary text here.[^2]"]
        lines = inserted_section_lines(original, updated)
        plan = {"section_summary": {"text": "Late summary text here."}, "bullets": []}
        self.assertIn("section_summary_missing_or_misplaced", rendered_structure_issues(lines, PACKET, plan))


if __name__ == "__main__":
    unittest.main()
