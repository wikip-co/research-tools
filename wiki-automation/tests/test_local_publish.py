from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from wiki_automation import cli, local_publish
from wiki_automation.local_publish import (
    apply_draft_plan,
    combine_critic_reviews,
    deterministic_placement_review_issues,
    duplicate_identifiers,
    format_critic_pr_audit,
    merge_duplicate_checks,
    run_local_publish,
    source_is_preclinical,
    validate_critic_review,
    validate_draft_plan,
    validate_rendered_markdown,
    validate_source_packet,
    word_token_similarity,
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

    def test_rat_mouse_terms_are_preclinical_and_require_animal_scope(self) -> None:
        for cue in ("rat", "rats", "mouse", "mice"):
            with self.subTest(cue=cue):
                packet = dict(self.packet)
                packet["abstract"] = (
                    f"This experiment administered the botanical blend to {cue} "
                    "and measured a metabolic outcome over eight weeks."
                )
                self.assertTrue(source_is_preclinical(packet))
                plan = {
                    "decision": "append_existing",
                    "target_path": "Natural Healing/example.md",
                    "parent_heading": "## Disease / Symptom Treatment",
                    "heading": "### Metabolic Syndrome",
                    "bullets": [
                        {
                            "text": packet["abstract"],
                            "source_quote": packet["abstract"],
                            "evidence_scope": "human",
                        }
                    ],
                }
                result = validate_draft_plan(
                    plan,
                    packet=packet,
                    candidate_paths={"Natural Healing/example.md"},
                )
                self.assertIn("preclinical_heading_scope_missing", result.issues)
                self.assertIn("bullet_0_preclinical_scope_must_be_animal", result.issues)

    def test_multi_target_plan_validates_each_target_and_explicit_exclusions(self) -> None:
        quote_one = (
            "Poor aqueous solubility and extensive metabolism limit the clinical "
            "translation of quercetin."
        )
        quote_two = "Clinical study results remain heterogeneous across the reviewed populations."
        packet = dict(self.packet)
        packet["abstract"] = f"{quote_one} {quote_two}"
        plan = {
            "decision": "append_existing",
            "study_type": "Review",
            "target_proposals": [
                {
                    "target_path": "Natural Healing/quercetin.md",
                    "parent_heading": "",
                    "heading": "## Bioavailability",
                    "rationale": "The claim directly concerns quercetin.",
                    "bullets": [
                        {
                            "text": quote_one,
                            "source_quote": quote_one,
                            "evidence_scope": "review_summary",
                        }
                    ],
                    "exclusions": [],
                },
                {
                    "target_path": "Health/clinical-evidence.md",
                    "parent_heading": "",
                    "heading": "## Evidence Limitations",
                    "rationale": "This target covers heterogeneity in clinical evidence.",
                    "bullets": [
                        {
                            "text": quote_two,
                            "source_quote": quote_two,
                            "evidence_scope": "review_summary",
                        }
                    ],
                    "exclusions": [{"source_quote": quote_one, "reason": "Different target entity."}],
                },
            ],
            "exclusions": [],
        }
        valid = validate_draft_plan(
            plan,
            packet=packet,
            candidate_paths={
                "Natural Healing/quercetin.md",
                "Health/clinical-evidence.md",
            },
        )
        self.assertTrue(valid.ok, valid.issues)
        plan["target_proposals"][1]["bullets"][0].update(
            {"text": quote_one, "source_quote": quote_one}
        )
        invalid = validate_draft_plan(
            plan,
            packet=packet,
            candidate_paths={
                "Natural Healing/quercetin.md",
                "Health/clinical-evidence.md",
            },
        )
        self.assertIn(
            "target_1_bullet_0_claim_assigned_to_multiple_targets",
            invalid.issues,
        )

    def test_near_verbatim_uses_word_tokens_without_autojunk(self) -> None:
        repeated = " ".join(["clementine"] * 220 + ["reduced", "weight", "gain"])
        self.assertEqual(word_token_similarity(repeated, repeated), 1.0)
        self.assertLess(
            word_token_similarity(repeated, "weight gain reduced " + " ".join(["grapefruit"] * 220)),
            0.1,
        )

    def test_rat_citrus_blend_yields_review_only_new_article_recommendation(self) -> None:
        packet = {
            "title": "Effects of a clementine and pink grapefruit blend on metabolic alterations in rats",
            "doi": "10.1000/citrus-rat",
            "abstract": (
                "A clementine and pink grapefruit blend was administered to rats "
                "with diet-induced metabolic alterations for eight weeks."
            ),
            "body_markdown": "Clementine and pink grapefruit blend evidence in rats. " * 30,
            "retrieval_issues": [],
            "reference_url": "https://doi.org/10.1000/citrus-rat",
            "url": "https://example.org/citrus-rat",
            "journal": "Example Journal",
            "pub_date": "2026",
            "study_type": "Animal Study: Rat",
            "authors": "A. Author",
            "keywords": "clementine; pink grapefruit; citrus blend; metabolic",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            content = root / "content"
            content.mkdir()
            article = content / "Natural Healing" / "bergamot.md"
            article.parent.mkdir(parents=True)
            article.write_text(
                "---\ntitle: Bergamot\ntags:\n- Citrus\n- Metabolic Health\n---\n\n"
                "# Bergamot\n\nBergamot is a citrus fruit discussed in metabolic research.\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=content, check=True)
            subprocess.run(["git", "add", "."], cwd=content, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.org",
                    "commit",
                    "-qm",
                    "base",
                ],
                cwd=content,
                check=True,
            )
            with patch.object(cli, "CONTENT_INDEX_DIR", root / "cache"), patch.object(
                cli, "scrape_source_packet", return_value=packet
            ), patch.object(
                cli,
                "check_duplicate_paper",
                return_value={"content_hits": [], "paper_result": None},
            ):
                report = run_local_publish(
                    source=packet["url"],
                    alert_name="",
                    content_repo=content,
                    tools_root=root,
                    output_dir=root / "out",
                    base_url="http://127.0.0.1:1/v1",
                    model="unused",
                    publish=False,
                    base_ref="HEAD",
                )
        self.assertEqual(report["status"], "needs_review")
        self.assertEqual(report["reason"], "no_content_candidates")
        recommendation = report["new_article_recommendation"]
        self.assertEqual(recommendation["recommendation"], "consider_new_article")
        self.assertFalse(recommendation["automatic_creation"])
        self.assertFalse(recommendation["automatic_publication"])

    def test_repairs_preserve_history_and_select_best_deterministic_valid_attempt(self) -> None:
        quote = (
            "Poor aqueous solubility and extensive metabolism limit the clinical "
            "translation of quercetin."
        )
        valid_plan = {
            "decision": "append_existing",
            "study_type": "Review",
            "target_proposals": [
                {
                    "target_path": "Natural Healing/quercetin.md",
                    "parent_heading": "",
                    "heading": "## Safety",
                    "rationale": "The target is the studied entity.",
                    "bullets": [
                        {
                            "text": quote,
                            "source_quote": quote,
                            "evidence_scope": "review_summary",
                        }
                    ],
                    "exclusions": [],
                }
            ],
            "exclusions": [],
        }
        invalid_repair = {
            **valid_plan,
            "target_proposals": [
                {**valid_plan["target_proposals"][0], "target_path": "Natural Healing/missing.md"}
            ],
        }
        rejected_critic = {
            "approved": False,
            "recommendation": "needs_review",
            "issues": [{"code": "wrong_heading", "severity": "review"}],
            "mode": "required",
            "target_reviews": [],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            content = root / "content"
            content.mkdir()
            article = content / "Natural Healing" / "quercetin.md"
            article.parent.mkdir(parents=True)
            article.write_text(
                "---\ntitle: Quercetin\ntags:\n- Antioxidant\n---\n\n## Safety\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=content, check=True)
            subprocess.run(["git", "add", "."], cwd=content, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.org",
                    "commit",
                    "-qm",
                    "base",
                ],
                cwd=content,
                check=True,
            )
            client = Mock()
            client.json_completion.side_effect = [valid_plan, invalid_repair]
            with patch.object(cli, "CONTENT_INDEX_DIR", root / "cache"), patch.object(
                cli, "scrape_source_packet", return_value=self.packet
            ), patch.object(
                cli,
                "check_duplicate_paper",
                return_value={"content_hits": [], "paper_result": None},
            ), patch.object(
                local_publish, "LocalLLMClient", return_value=client
            ), patch.object(
                local_publish, "style_context", return_value="guides"
            ), patch.object(
                local_publish, "review_plan_targets", return_value=rejected_critic
            ):
                report = run_local_publish(
                    source=self.packet["url"],
                    alert_name="Quercetin",
                    content_repo=content,
                    tools_root=root,
                    output_dir=root / "out",
                    base_url="http://127.0.0.1:1/v1",
                    model="unused",
                    publish=False,
                    base_ref="HEAD",
                    max_draft_attempts=2,
                )
        self.assertEqual(report["status"], "needs_review")
        self.assertEqual(report["reason"], "critic_quality_gate_failed")
        self.assertEqual(len(report["attempt_history"]), 2)
        self.assertEqual(report["selected_attempt"], 1)
        self.assertEqual(report["best_deterministic_valid_attempt"]["plan"], valid_plan)

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

    def test_pr_audit_separates_rejected_critic_observations(self) -> None:
        critic = {
            "issues": [
                {
                    "code": "medical_overclaim",
                    "severity": "warning",
                    "bullet_index": 0,
                    "explanation": "Keep the animal scope explicit.",
                }
            ],
            "placement_review": {
                "rejected_issues": [
                    {
                        "issue": {
                            "code": "wrong_target_page",
                            "severity": "review",
                            "bullet_index": 0,
                            "explanation": "A cultivar-specific page may be more precise.",
                        },
                        "validation_errors": [
                            "source_quote_not_exact",
                            "wrong_target_requires_source_and_target_quotes",
                        ],
                    }
                ]
            },
            "evidence_review": {"rejected_issues": []},
        }
        audit = format_critic_pr_audit(critic)
        self.assertIn("### Validated critic findings", audit)
        self.assertIn("`warning` `medical_overclaim` (bullet 0)", audit)
        self.assertIn("### Rejected critic observations (non-blocking)", audit)
        self.assertIn("did not affect the publication gate decision", audit)
        self.assertIn("`placement` `review` `wrong_target_page` (bullet 0)", audit)
        self.assertIn("`source_quote_not_exact`", audit)
        self.assertIn("`wrong_target_requires_source_and_target_quotes`", audit)

    def test_pr_audit_retains_override_reason_and_empty_rejected_section(self) -> None:
        audit = format_critic_pr_audit(
            {
                "issues": [{"code": "wrong_heading", "severity": "review", "explanation": "Review placement."}],
                "placement_review": {"rejected_issues": []},
                "evidence_review": {"rejected_issues": []},
            },
            override_applied=True,
            override_reason="Human reviewed target and evidence",
        )
        self.assertIn("Critic rejection override applied", audit)
        self.assertIn("Override reason: Human reviewed target and evidence", audit)
        self.assertIn("### Rejected critic observations (non-blocking)\n\n", audit)
        self.assertTrue(audit.endswith("- None"))

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
