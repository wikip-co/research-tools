from __future__ import annotations

import unittest

from wiki_automation.local_publish import (
    apply_draft_plan,
    validate_draft_plan,
    validate_rendered_markdown,
    validate_source_packet,
)


class LocalPublishTests(unittest.TestCase):
    def setUp(self) -> None:
        self.packet = {
            "title": "Quercetin review",
            "doi": "10.1000/example",
            "abstract": (
                "Poor aqueous solubility and extensive metabolism limit the clinical "
                "translation of quercetin. Clinical study results remain heterogeneous."
            ),
            "body_markdown": "Full source evidence. " * 100,
            "retrieval_issues": [],
            "reference_url": "https://doi.org/10.1000/example",
            "url": "https://example.org/article",
            "journal": "Example Journal",
            "pub_date": "2026",
            "study_type": "Review",
            "authors": "A. Author",
        }

    def test_packet_rejects_large_captcha(self) -> None:
        packet = {
            "title": "Are you a robot?",
            "body_markdown": "captcha bundle " * 10000,
            "retrieval_issues": ["captcha_page"],
        }
        result = validate_source_packet(packet)
        self.assertFalse(result.ok)
        self.assertIn("captcha_page", result.issues)

    def test_plan_requires_exact_source_quote_and_candidate(self) -> None:
        plan = {
            "decision": "append_existing",
            "target_path": "Natural Healing/quercetin.md",
            "parent_heading": "",
            "heading": "## Safety",
            "bullets": [
                {
                    "text": "Poor aqueous solubility and extensive metabolism limit the clinical translation of quercetin.",
                    "source_quote": "Poor aqueous solubility and extensive metabolism limit the clinical translation of quercetin.",
                    "evidence_scope": "review_summary",
                }
            ],
        }
        result = validate_draft_plan(
            plan,
            packet=self.packet,
            candidate_paths={"Natural Healing/quercetin.md"},
        )
        self.assertTrue(result.ok, result.issues)

        plan["bullets"][0]["source_quote"] = "This statement was never in the source packet at all."
        bad = validate_draft_plan(
            plan,
            packet=self.packet,
            candidate_paths={"Natural Healing/quercetin.md"},
        )
        self.assertIn("bullet_0_quote_not_in_source", bad.issues)

    def test_apply_plan_adds_cited_bullet_and_next_reference(self) -> None:
        markdown = """---
title: Quercetin
tags:
- Antioxidant
---

## Safety

- Existing finding.[^1]

## References

[^1]: Existing reference
"""
        plan = {
            "heading": "## Safety",
            "parent_heading": "",
            "bullets": [
                {
                    "text": "Poor aqueous solubility and extensive metabolism limit the clinical translation of quercetin."
                }
            ],
        }
        updated = apply_draft_plan(markdown, plan, self.packet)
        self.assertIn("quercetin.[^2]", updated)
        self.assertIn("[^2]: **Title:**", updated)
        rendered = validate_rendered_markdown(
            markdown, updated, plan=plan, packet=self.packet
        )
        self.assertTrue(rendered.ok, rendered.issues)

    def test_preclinical_plan_requires_scoped_heading(self) -> None:
        packet = dict(self.packet)
        packet["title"] = "Resveratrol in animal models of pulmonary fibrosis"
        plan = {
            "decision": "append_existing",
            "target_path": "Natural Healing/resveratrol.md",
            "parent_heading": "## Disease / Symptom Treatment",
            "heading": "### Pulmonary Fibrosis",
            "bullets": [
                {
                    "text": "Poor aqueous solubility and extensive metabolism limit the clinical translation of quercetin.",
                    "source_quote": "Poor aqueous solubility and extensive metabolism limit the clinical translation of quercetin.",
                    "evidence_scope": "animal",
                }
            ],
        }
        invalid = validate_draft_plan(
            plan,
            packet=packet,
            candidate_paths={"Natural Healing/resveratrol.md"},
        )
        self.assertIn("preclinical_heading_scope_missing", invalid.issues)
        plan["heading"] = "### Pulmonary Fibrosis (Animal Evidence)"
        valid = validate_draft_plan(
            plan,
            packet=packet,
            candidate_paths={"Natural Healing/resveratrol.md"},
        )
        self.assertTrue(valid.ok, valid.issues)

    def test_new_subsection_is_inserted_before_footnote_definitions(self) -> None:
        markdown = """---
title: Quercetin
tags:
- Antioxidant
---

## Disease / Symptom Treatment

Existing text.[^1]

[^1]: Existing reference
"""
        plan = {
            "heading": "### Example Condition",
            "parent_heading": "## Disease / Symptom Treatment",
            "bullets": [{"text": "A source-supported finding."}],
        }
        updated = apply_draft_plan(markdown, plan, self.packet)
        self.assertLess(updated.index("### Example Condition"), updated.index("[^1]:"))
        self.assertGreater(updated.index("[^2]:"), updated.index("[^1]:"))


if __name__ == "__main__":
    unittest.main()
