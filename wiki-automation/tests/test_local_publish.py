from __future__ import annotations

import unittest
from pathlib import Path

from wiki_automation.local_publish import (
    apply_draft_plan,
    combine_critic_reviews,
    deterministic_placement_review_issues,
    duplicate_identifiers,
    merge_duplicate_checks,
    run_local_publish,
    validate_critic_review,
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

    def test_packet_rejects_placeholder_citation_metadata(self) -> None:
        packet = dict(self.packet)
        packet.update(
            {
                "doi": "DOI:",
                "reference_url": "https://doi.org/DOI:",
                "authors": "Authors and Affiliations",
                "journal": "Ovid",
                "pub_date": "Unknown",
                "abstract": "A truncated abstract...",
            }
        )
        result = validate_source_packet(packet)
        self.assertFalse(result.ok)
        self.assertIn("invalid_doi", result.issues)
        self.assertIn("missing_or_placeholder_authors", result.issues)
        self.assertIn("missing_or_placeholder_journal", result.issues)
        self.assertIn("missing_or_placeholder_pub_date", result.issues)
        self.assertIn("truncated_abstract", result.issues)

    def test_duplicate_identifiers_include_doi_and_url_fallbacks(self) -> None:
        packet = dict(self.packet)
        packet["requested_url"] = "https://publisher.example/requested"
        self.assertEqual(
            duplicate_identifiers(packet, "https://alert.example/source"),
            [
                "https://doi.org/10.1000/example",
                "10.1000/example",
                "https://example.org/article",
                "https://publisher.example/requested",
                "https://alert.example/source",
            ],
        )

    def test_duplicate_checks_merge_hits_and_active_paper_state(self) -> None:
        merged = merge_duplicate_checks(
            [
                {
                    "identifier": "10.1000/example",
                    "content_hits": [{"path": "a.md", "ref_num": "1", "ref_url": "https://doi.org/10.1000/example"}],
                    "paper_result": {"paper": {"workflow_state": "discovered"}},
                },
                {
                    "identifier": "https://example.org/article",
                    "content_hits": [{"path": "a.md", "ref_num": "1", "ref_url": "https://doi.org/10.1000/example"}],
                    "paper_result": {"paper": {"workflow_state": "pr_open"}},
                },
            ]
        )
        self.assertEqual(merged["content_hit_count"], 1)
        self.assertEqual(merged["paper_result"]["paper"]["workflow_state"], "pr_open")

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

    def test_critic_rejects_unquoted_and_self_contradictory_objections(self) -> None:
        plan = {
            "bullets": [
                {
                    "text": "Poor aqueous solubility limits translation.",
                    "source_quote": "Poor aqueous solubility and extensive metabolism limit the clinical translation of quercetin.",
                }
            ]
        }
        review = {
            "issues": [
                {
                    "code": "unsupported_claim",
                    "severity": "blocking",
                    "bullet_index": 0,
                    "explanation": "The source supports this claim, so it is unsupported.",
                    "source_quote": "Poor aqueous solubility and extensive metabolism limit the clinical translation of quercetin.",
                    "target_quote": "",
                },
                {
                    "code": "medical_overclaim",
                    "severity": "review",
                    "bullet_index": 0,
                    "explanation": "This wording implies human efficacy.",
                    "source_quote": "This quote is not in the packet.",
                    "target_quote": "",
                },
            ]
        }
        validated = validate_critic_review(
            review,
            review_kind="evidence",
            packet=self.packet,
            plan=plan,
            selected_target_markdown="## Safety\n",
        )
        self.assertEqual(validated["issues"], [])
        errors = [
            error
            for item in validated["rejected_issues"]
            for error in item["validation_errors"]
        ]
        self.assertIn("self_contradictory_issue", errors)
        self.assertIn("source_quote_not_exact", errors)

    def test_split_critic_uses_validated_severity_for_decision(self) -> None:
        placement = {
            "kind": "placement",
            "response_valid": True,
            "issues": [{"code": "wrong_heading", "severity": "warning"}],
            "rejected_issues": [],
        }
        evidence = {
            "kind": "evidence",
            "response_valid": True,
            "issues": [],
            "rejected_issues": [],
        }
        warning_only = combine_critic_reviews(placement, evidence)
        self.assertTrue(warning_only["approved"])
        evidence["issues"] = [{"code": "medical_overclaim", "severity": "review"}]
        needs_revision = combine_critic_reviews(placement, evidence)
        self.assertFalse(needs_revision["approved"])
        self.assertEqual(needs_revision["recommendation"], "revise")

    def test_critic_discards_bad_optional_target_quote_and_promotes_safety_severity(self) -> None:
        plan = {"bullets": [{"text": "A finding."}]}
        source_quote = (
            "Poor aqueous solubility and extensive metabolism limit the clinical "
            "translation of quercetin."
        )
        validated = validate_critic_review(
            {
                "issues": [
                    {
                        "code": "study_type_inflation",
                        "severity": "warning",
                        "bullet_index": 0,
                        "explanation": "The evidence scope is broader than the experiment.",
                        "source_quote": source_quote,
                        "target_quote": "A proposed bullet that is not yet on the target page.",
                    }
                ]
            },
            review_kind="evidence",
            packet=self.packet,
            plan=plan,
            selected_target_markdown="## Existing target\n",
        )
        self.assertEqual(len(validated["issues"]), 1)
        issue = validated["issues"][0]
        self.assertEqual(issue["severity"], "review")
        self.assertEqual(issue["target_quote"], "")
        self.assertIn("severity_promoted_from_warning", issue["validation_warnings"])
        self.assertIn(
            "discarded_optional_nonexact_target_quote",
            issue["validation_warnings"],
        )

    def test_critic_cannot_remove_required_preclinical_heading_scope(self) -> None:
        packet = dict(self.packet)
        packet["study_type"] = "Animal Study: In Vivo"
        plan = {
            "heading": "### Multiple Myeloma (Animal Evidence)",
            "bullets": [{"text": "A finding."}],
        }
        target = "## Disease / Symptom Treatment\n\n### Diabetes\n"
        validated = validate_critic_review(
            {
                "issues": [
                    {
                        "code": "wrong_heading",
                        "severity": "review",
                        "bullet_index": None,
                        "explanation": "The Animal Evidence label is redundant and overly specific; remove it.",
                        "source_quote": "",
                        "target_quote": "## Disease / Symptom Treatment\n\n### Diabetes",
                    }
                ]
            },
            review_kind="placement",
            packet=packet,
            plan=plan,
            selected_target_markdown=target,
        )
        self.assertEqual(validated["issues"], [])
        self.assertIn(
            "contradicts_deterministic_preclinical_scope",
            validated["rejected_issues"][0]["validation_errors"],
        )

    def test_new_isolated_compound_bullets_require_placement_review(self) -> None:
        packet = dict(self.packet)
        packet["abstract"] = (
            "Mass spectrometry identified dihydromyricetin (DMY) as a pivotal "
            "component. DMY induced apoptosis in cells."
        )
        plan = {
            "bullets": [
                {"text": "Dihydromyricetin (DMY) was identified as a component."},
                {"text": "DMY induced apoptosis in cells."},
            ]
        }
        issues = deterministic_placement_review_issues(
            packet=packet,
            plan=plan,
            selected_target_markdown="---\ntitle: Green Tea\n---\n\n# Green Tea\n",
        )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["code"], "unsafe_context_inference")
        self.assertEqual(issues[0]["severity"], "review")
        self.assertEqual(issues[0]["origin"], "deterministic_placement_policy")

        already_covered = deterministic_placement_review_issues(
            packet=packet,
            plan=plan,
            selected_target_markdown="---\ntitle: Dihydromyricetin (DMY)\n---\n",
        )
        self.assertEqual(already_covered, [])

    def test_passive_worker_prohibits_critic_override(self) -> None:
        with self.assertRaisesRegex(ValueError, "prohibited"):
            run_local_publish(
                source="https://example.org/article",
                alert_name="",
                content_repo=Path("/tmp/content"),
                tools_root=Path("/tmp/tools"),
                output_dir=Path("/tmp/output"),
                base_url="http://127.0.0.1:8080/v1",
                model="test",
                publish=True,
                allow_critic_rejection=True,
                override_reason="Human reviewed",
                passive_worker=True,
            )

    def test_off_mode_and_unaudited_overrides_cannot_publish(self) -> None:
        common = {
            "source": "https://example.org/article",
            "alert_name": "",
            "content_repo": Path("/tmp/content"),
            "tools_root": Path("/tmp/tools"),
            "output_dir": Path("/tmp/output"),
            "base_url": "http://127.0.0.1:8080/v1",
            "model": "test",
        }
        with self.assertRaisesRegex(ValueError, "manual dry-run only"):
            run_local_publish(**common, publish=True, critic_mode="off")
        with self.assertRaisesRegex(ValueError, "non-empty --override-reason"):
            run_local_publish(
                **common,
                publish=True,
                allow_critic_rejection=True,
                override_reason="",
            )
        with self.assertRaisesRegex(ValueError, "only valid with --publish"):
            run_local_publish(
                **common,
                publish=False,
                allow_critic_rejection=True,
                override_reason="Reviewed",
            )


if __name__ == "__main__":
    unittest.main()
