from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from wiki_automation import cli, local_publish
from wiki_automation.local_publish import (
    ModelOutputJSONError,
    apply_draft_plan,
    canonical_citation_marker,
    citation_marker_matches_reference,
    combine_critic_reviews,
    critic_blocks_publication,
    deterministic_placement_review_issues,
    duplicate_identifiers,
    format_gate_summary,
    format_critic_pr_audit,
    merge_duplicate_checks,
    missing_entity_page_seed,
    run_local_publish,
    normalize_simple_draft_plan,
    simple_draft_prompt,
    simple_prompt_source_packet,
    source_is_preclinical,
    study_type_matches_packet,
    validate_critic_review,
    validate_draft_plan,
    validate_simple_draft_plan,
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

    def test_model_client_preserves_malformed_json_for_bounded_repair(self) -> None:
        malformed = '{"decision": "publish_changes",}'
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(
            {
                "choices": [
                    {
                        "message": {"content": malformed},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        ).encode("utf-8")
        client = local_publish.LocalLLMClient("http://127.0.0.1:1/v1", "test")
        with patch.object(local_publish, "urlopen", return_value=response):
            with self.assertRaises(ModelOutputJSONError):
                client.json_completion(system="system", user="user")
        self.assertEqual(client.calls[0]["response_text"], malformed)
        self.assertEqual(client.calls[0]["response_chars"], len(malformed))
        self.assertEqual(client.calls[0]["status"], "error")

    def test_model_lock_fails_fast_for_overlapping_publishers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "model.lock"
            first = local_publish.LocalLLMClient(
                "http://127.0.0.1:1/v1", "first", lock_path=lock_path
            )
            second = local_publish.LocalLLMClient(
                "http://127.0.0.1:1/v1", "second", lock_path=lock_path
            )
            first._acquire_lock()
            try:
                with self.assertRaises(local_publish.LocalModelBusyError):
                    second._acquire_lock()
            finally:
                first.close()

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

    def test_simple_prompt_is_bounded_and_omits_reference_graph(self) -> None:
        packet = dict(self.packet)
        packet["body_markdown"] = (
            "## Abstract\n\nA direct quercetin finding was reported in this review.\n\n"
            "## 1. Introduction\n\nQuercetin has a traditional background use described by the paper [1].\n\n"
            "## 2. Methods\n\nAssay detail. "
            + ("excluded method text " * 5000)
            + "\n\n## 3. Results\n\nQuercetin produced a measured result of 12 mg in the study.\n\n"
            "## References\n\n1. An earlier paper that must not enter the prompt."
        )
        source = simple_prompt_source_packet(packet)
        rendered = json.dumps(source)
        self.assertLessEqual(source["prompt_context"]["claim_chars"], 55000)
        self.assertEqual(source["prompt_context"]["reference_entry_count"], 0)
        self.assertNotIn("An earlier paper that must not enter the prompt", rendered)
        self.assertNotIn("excluded method text", rendered)

        prompt = simple_draft_prompt(
            packet=packet,
            candidates=[],
            candidate_documents={},
            domain="Natural Healing",
            category_catalog=["Natural Healing/Chemicals"],
            new_article_seed=None,
        )
        self.assertIn("core results", prompt)
        self.assertIn("traditional", prompt)
        self.assertNotIn('"cited_references"', prompt)

    def test_simple_prompt_reserves_context_for_results_and_background(self) -> None:
        packet = dict(self.packet)
        packet["body_markdown"] = (
            "## 1. Introduction\n\n"
            "Quercetin has a traditional background use explicitly described here.\n\n"
            "## 3. Results\n\n"
            + ("Quercetin produced a measured animal result. " * 3000)
        )

        source = simple_prompt_source_packet(packet)
        rendered = json.dumps(source)

        self.assertLessEqual(source["prompt_context"]["claim_chars"], 55000)
        self.assertGreater(source["prompt_context"]["claim_chars_by_group"]["results"], 0)
        self.assertGreater(
            source["prompt_context"]["claim_chars_by_group"]["introduction"], 0
        )
        self.assertIn("measured animal result", rendered)
        self.assertIn("traditional background use", rendered)

    def test_simple_validator_accepts_main_source_only_plan(self) -> None:
        quote = (
            "Poor aqueous solubility and extensive metabolism limit the clinical "
            "translation of quercetin."
        )
        plan = {
            "decision": "publish_changes",
            "study_type": "Review",
            "target_proposals": [
                {
                    "operation": "append_existing",
                    "target_path": "Natural Healing/quercetin.md",
                    "target_entity": "quercetin",
                    "parent_heading": "",
                    "heading": "## Safety",
                    "rationale": "The finding directly concerns quercetin.",
                    "bullets": [
                        {
                            "text": quote,
                            "source_quote": quote,
                            "source_section": "Abstract",
                            "claim_kind": "source_finding",
                            "evidence_scope": "review_summary",
                            "subsection": "",
                        }
                    ],
                }
            ],
        }
        validation = validate_simple_draft_plan(
            plan,
            packet=self.packet,
            candidate_paths={"Natural Healing/quercetin.md"},
            candidate_metadata={
                "Natural Healing/quercetin.md": {"title": "Quercetin"}
            },
            existing_paths={"Natural Healing/quercetin.md"},
            domain="Natural Healing",
        )
        self.assertTrue(validation.ok, validation.issues)

        plan["target_proposals"][0]["bullets"][0]["cited_references"] = [
            {"citation_marker": "[1]", "reference_text": "Earlier work"}
        ]
        validation = validate_simple_draft_plan(
            plan,
            packet=self.packet,
            candidate_paths={"Natural Healing/quercetin.md"},
            candidate_metadata={
                "Natural Healing/quercetin.md": {"title": "Quercetin"}
            },
            existing_paths={"Natural Healing/quercetin.md"},
            domain="Natural Healing",
        )
        self.assertIn(
            "target_0_bullet_0_external_reference_not_allowed", validation.issues
        )

    def test_simple_normalization_uses_only_exact_source_text(self) -> None:
        exact_quote = (
            "Poor aqueous solubility and extensive metabolism limit the clinical "
            "translation of quercetin."
        )
        spaced_source = "Quercetin measured 12\u00a0mg in the source animal study."
        packet = {
            **self.packet,
            "body_markdown": f"{self.packet['body_markdown']}\n{spaced_source}",
        }
        plan = {
            "target_proposals": [
                {
                    "bullets": [
                        {
                            "text": "Quercetin has limited clinical use.",
                            "source_quote": exact_quote,
                            "cited_references": [{"reference_text": "Do not cite this"}],
                        },
                        {
                            "text": "Quercetin measured 12 mg in the source animal study.",
                            "source_quote": "Quercetin measured 12 mg in the source animal study.",
                        },
                        {
                            "text": "Unsupported claim.",
                            "source_quote": "This passage does not occur in the article at all.",
                        },
                    ]
                }
            ]
        }

        actions = normalize_simple_draft_plan(plan, packet=packet)

        bullets = plan["target_proposals"][0]["bullets"]
        self.assertEqual(len(bullets), 2)
        self.assertEqual(bullets[0]["text"], exact_quote)
        self.assertNotIn("cited_references", bullets[0])
        self.assertEqual(bullets[1]["source_quote"], spaced_source)
        self.assertTrue(
            any(item["action"] == "dropped_unprovable_bullet" for item in actions)
        )

    def test_publish_detects_open_pr_before_scraping(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = "https://example.org/article"
            open_pr = {
                "checked": True,
                "matches": [
                    {
                        "identifier": source,
                        "number": 27,
                        "url": "https://github.com/example/content/pull/27",
                    }
                ],
            }
            with patch.object(local_publish, "run_command", return_value="revision"), patch.object(
                local_publish, "find_open_pr_duplicate", return_value=open_pr
            ), patch.object(cli, "scrape_source_packet") as scrape:
                report = run_local_publish(
                    source=source,
                    alert_name="Citrus",
                    content_repo=root / "content",
                    tools_root=root / "tools",
                    output_dir=root / "out",
                    base_url="http://127.0.0.1:8080/v1",
                    model="test",
                    publish=True,
                    pipeline="simple",
                )
        scrape.assert_not_called()
        self.assertEqual(report["status"], "duplicate")
        self.assertEqual(report["reason"], "open_pull_request")
        self.assertEqual(report["pr_url"], "https://github.com/example/content/pull/27")
        self.assertEqual(report["model_calls"], [])

    def test_duplicate_pr_bypass_is_explicit_and_publish_only(self) -> None:
        args = cli.build_parser().parse_args(
            [
                "local-publish",
                "https://example.org/article",
                "--domain",
                "Natural Healing",
                "--publish",
                "--allow-duplicate-pr",
            ]
        )
        self.assertEqual(args.pipeline, "simple")
        self.assertTrue(args.allow_duplicate_pr)
        with self.assertRaisesRegex(ValueError, "requires --publish"):
            run_local_publish(
                source="https://example.org/article",
                alert_name="",
                content_repo=Path("/tmp/content"),
                tools_root=Path("/tmp/tools"),
                output_dir=Path("/tmp/output"),
                base_url="http://127.0.0.1:8080/v1",
                model="test",
                publish=False,
                pipeline="simple",
                allow_duplicate_pr=True,
            )

    def test_gate_summary_labels_duplicate_comparison_as_bypass(self) -> None:
        summary = format_gate_summary(
            packet=self.packet,
            packet_validation={"ok": True, "issues": []},
            duplicate={
                "bypass_requested": True,
                "content_hit_count": 0,
                "open_pull_requests": {"matches": [{"number": 27}]},
            },
            plan={"target_proposals": []},
            deterministic=local_publish.ValidationResult(True, []),
            rendered_results=[],
        )

        self.assertIn("Duplicate: bypass — explicit A/B comparison", summary)
        self.assertIn("1 open PR matches", summary)

    def test_sciencedirect_visible_marker_remains_linked_to_reference(self) -> None:
        source_marker = "[Liu et al., 2016](#bb0115)"
        model_marker = "[Liu et al., 2016]"
        reference = (
            "23. [Liu, Dong, Yang and Pan, 2016](#bbb0115)\n\n"
            "Anti-diabetic effect of citrus pectin in diabetic rats"
        )
        self.assertEqual(
            canonical_citation_marker(source_marker),
            canonical_citation_marker(model_marker),
        )
        self.assertTrue(citation_marker_matches_reference(model_marker, reference))
        self.assertFalse(
            citation_marker_matches_reference(
                "[Fidelix et al., 2020]", reference
            )
        )

    def test_generic_article_type_can_be_refined_by_explicit_rat_evidence(self) -> None:
        packet = {
            **self.packet,
            "study_type": "Research Article",
            "abstract": "The citrus intervention was administered to rats for eight weeks.",
        }
        self.assertTrue(study_type_matches_packet("Animal Study", packet))
        self.assertFalse(study_type_matches_packet("Human Trial", packet))

    def test_existing_citrus_category_seeds_missing_catch_all_page(self) -> None:
        seed = missing_entity_page_seed(
            packet={
                **self.packet,
                "title": "A functional citrus-based food in rats",
                "abstract": "Citrus concentrates were compared with metformin.",
            },
            alert_name="Citrus",
            domain="Natural Healing",
            category_catalog=[
                "Natural Healing/Fruits/Citrus",
                "Natural Healing/Fiber/Pectin",
            ],
            existing_paths={"Natural Healing/Fruits/Citrus/orange.md"},
        )
        self.assertEqual(
            seed["suggested_path"], "Natural Healing/Fruits/Citrus/citrus.md"
        )

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
        unscoped = validate_draft_plan(
            plan,
            packet=packet,
            candidate_paths={"Natural Healing/resveratrol.md"},
        )
        # The rendered Preclinical Evidence subsection now satisfies heading
        # scope deterministically; the plan-level gap is a warning, not a gate.
        self.assertTrue(unscoped.ok, unscoped.issues)
        self.assertIn("target_0_preclinical_heading_scope_warning", unscoped.warnings)
        plan["heading"] = "### Pulmonary Fibrosis (Animal Evidence)"
        valid = validate_draft_plan(
            plan,
            packet=packet,
            candidate_paths={"Natural Healing/resveratrol.md"},
        )
        self.assertTrue(valid.ok, valid.issues)

    def test_methods_statement_is_not_an_effect_bullet(self) -> None:
        text = (
            "A membrane processing approach was used to produce citrus-based "
            "functional food enriched in carotenoids, hesperidin, and pectins."
        )
        packet = dict(self.packet)
        packet["abstract"] = text
        plan = {
            "decision": "append_existing",
            "target_path": "Natural Healing/quercetin.md",
            "parent_heading": "",
            "heading": "## Healing Properties",
            "bullets": [
                {"text": text, "source_quote": text, "evidence_scope": "animal"}
            ],
        }
        result = validate_draft_plan(
            plan, packet=packet, candidate_paths={"Natural Healing/quercetin.md"}
        )
        self.assertIn("bullet_0_methods_statement_not_effect_claim", result.issues)

    def test_scope_prefix_is_whitelisted_in_near_verbatim_gate(self) -> None:
        quote = (
            "Consumption of the citrus concentrates improved glucose tolerance, "
            "fasting glycaemia, insulinaemia, insulin sensitivity, and lipid profile."
        )
        packet = dict(self.packet)
        packet["abstract"] = f"{quote} The study used fructose-fed rats as its model."
        plan = {
            "decision": "append_existing",
            "target_path": "Natural Healing/quercetin.md",
            "parent_heading": "",
            "heading": "## Healing Properties",
            "formulation_definition": {
                "text": quote,
                "source_quote": quote,
                "source_section": "Abstract",
            },
            "bullets": [
                {
                    "text": f"In fructose-fed rats, {quote[:1].lower()}{quote[1:]}",
                    "source_quote": quote,
                    "evidence_scope": "animal",
                }
            ],
        }
        result = validate_draft_plan(
            plan, packet=packet, candidate_paths={"Natural Healing/quercetin.md"}
        )
        self.assertNotIn("bullet_0_not_near_verbatim", result.issues)

    def test_animal_formulation_findings_require_definition(self) -> None:
        quote = "Consumption of the citrus concentrates improved glucose tolerance in rats."
        packet = dict(self.packet)
        packet["abstract"] = quote
        plan = {
            "decision": "append_existing",
            "target_path": "Natural Healing/quercetin.md",
            "parent_heading": "",
            "heading": "## Healing Properties",
            "bullets": [
                {"text": quote, "source_quote": quote, "evidence_scope": "animal"}
            ],
        }
        result = validate_draft_plan(
            plan, packet=packet, candidate_paths={"Natural Healing/quercetin.md"}
        )
        self.assertIn("formulation_definition_missing", result.issues)
        plan["formulation_definition"] = {
            "text": quote,
            "source_quote": quote,
            "source_section": "Abstract",
        }
        repaired = validate_draft_plan(
            plan, packet=packet, candidate_paths={"Natural Healing/quercetin.md"}
        )
        self.assertNotIn("formulation_definition_missing", repaired.issues)

    def test_quantitative_results_preference_and_warning(self) -> None:
        quantitative = (
            "Likewise, supplementation with the citrus concentrates significantly "
            "decreased systolic blood pressure, reaching values of 127.65 ± 3.81 mmHg "
            "in treated fructose-fed rats."
        )
        qualitative = "The citrus concentrates improved the metabolic profile of fructose-fed rats."
        packet = dict(self.packet)
        packet["abstract"] = qualitative
        packet["body_markdown"] = f"## 3. Results\n\n{quantitative}"
        plan = {
            "decision": "append_existing",
            "target_path": "Natural Healing/quercetin.md",
            "parent_heading": "",
            "heading": "## Healing Properties",
            "formulation_definition": {
                "text": qualitative,
                "source_quote": qualitative,
                "source_section": "Abstract",
            },
            "bullets": [
                {"text": qualitative, "source_quote": qualitative, "evidence_scope": "animal"}
            ],
        }
        result = validate_draft_plan(
            plan, packet=packet, candidate_paths={"Natural Healing/quercetin.md"}
        )
        self.assertIn("target_0_missing_quantitative_outcome", result.warnings)
        plan["bullets"].append(
            {"text": quantitative, "source_quote": quantitative, "evidence_scope": "animal"}
        )
        improved = validate_draft_plan(
            plan, packet=packet, candidate_paths={"Natural Healing/quercetin.md"}
        )
        self.assertNotIn("target_0_missing_quantitative_outcome", improved.warnings)
        prompt = local_publish.draft_prompt(
            packet=packet, candidates=[], candidate_documents={}, claim_policy="integrated"
        )
        self.assertIn("QUANTITATIVE_RESULTS_CANDIDATES", prompt)
        self.assertIn("127.65", prompt)

    def test_composition_data_requires_composition_section(self) -> None:
        composition = (
            "Each 2-mL dose contained approximately 0.058 mg cryptoxanthin, "
            "0.037 mg carotene, and 2.70 mg hesperidin from the quercetin extract."
        )
        finding = "The quercetin extract improved glucose tolerance in rats."
        packet = dict(self.packet)
        packet["abstract"] = finding
        packet["study_type"] = "Animal Study"
        packet["body_markdown"] = f"## 2. Methods\n\n{composition}\n\n## 3. Results\n\nText."
        base_proposal = {
            "operation": "append_existing",
            "target_path": "Natural Healing/quercetin.md",
            "target_entity": "quercetin",
            "parent_heading": "",
            "heading": "## Healing Properties",
            "rationale": "Direct findings about quercetin.",
            "formulation_definition": {
                "text": finding,
                "source_quote": finding,
                "source_section": "Abstract",
            },
            "bullets": [
                {
                    "text": finding,
                    "source_quote": finding,
                    "source_section": "Abstract",
                    "claim_kind": "source_finding",
                    "evidence_scope": "animal",
                    "cited_references": [],
                }
            ],
            "exclusions": [],
        }
        plan = {
            "decision": "publish_changes",
            "study_type": "Animal Study",
            "target_proposals": [base_proposal],
            "exclusions": [],
        }
        candidates = {
            "Natural Healing/quercetin.md": {
                "path": "Natural Healing/quercetin.md",
                "title": "Quercetin",
            }
        }
        result = validate_draft_plan(
            plan,
            packet=packet,
            candidate_paths=set(candidates),
            candidate_metadata=candidates,
            claim_policy="integrated",
        )
        self.assertIn("missing_composition_section", result.warnings)
        composition_proposal = json.loads(json.dumps(base_proposal))
        composition_proposal.update(
            {
                "heading": "## Composition",
                "rationale": "Constituent measurements of the studied extract.",
                "bullets": [
                    {
                        "text": composition,
                        "source_quote": composition,
                        "source_section": "2. Methods",
                        "claim_kind": "source_finding",
                        "evidence_scope": "composition",
                        "cited_references": [],
                    }
                ],
            }
        )
        plan["target_proposals"] = [composition_proposal, base_proposal]
        both = validate_draft_plan(
            plan,
            packet=packet,
            candidate_paths=set(candidates),
            candidate_metadata=candidates,
            claim_policy="integrated",
        )
        self.assertTrue(both.ok, both.issues)
        self.assertNotIn("missing_composition_section", both.warnings)

    def test_new_article_lead_requires_definition_form(self) -> None:
        finding = "The citrus blend reduced body-weight gain in rats during the feeding period."
        packet = dict(self.packet)
        packet["title"] = "A citrus blend in rats"
        packet["abstract"] = finding
        packet["body_markdown"] = f"## Abstract\n\n{finding}"

        def plan_with_lead(lead: dict) -> dict:
            return {
                "decision": "publish_changes",
                "study_type": "Animal Study",
                "target_proposals": [
                    {
                        "operation": "create_new",
                        "target_path": "Natural Healing/Fruits/Citrus/citrus.md",
                        "target_entity": "citrus",
                        "new_article": {
                            "title": "Citrus",
                            "tags": ["Citrus"],
                            "category_rationale": "Existing category.",
                            **lead,
                        },
                        "parent_heading": "",
                        "heading": "## Healing Properties",
                        "rationale": "Direct findings about the citrus blend.",
                        "formulation_definition": {
                            "text": finding,
                            "source_quote": finding,
                            "source_section": "Abstract",
                        },
                        "bullets": [
                            {
                                "text": finding,
                                "source_quote": finding,
                                "source_section": "Abstract",
                                "claim_kind": "source_finding",
                                "evidence_scope": "animal",
                                "cited_references": [],
                            }
                        ],
                        "exclusions": [],
                    }
                ],
                "exclusions": [],
            }

        framing = plan_with_lead(
            {
                "lead_kind": "source_grounded",
                "lead_text": (
                    "Citrus fruits and their derived products have attracted significant "
                    "attention due to their potential beneficial effects on metabolic health."
                ),
                "lead_source_quote": finding,
            }
        )
        result = validate_draft_plan(
            framing,
            packet=packet,
            candidate_paths=set(),
            existing_paths={"Natural Healing/Fruits/Citrus/bergamot.md"},
            claim_policy="integrated",
        )
        self.assertIn("target_0_lead_not_definition_form", result.issues)

        health_claim = plan_with_lead(
            {
                "lead_kind": "definition",
                "lead_text": "Citrus is a genus of flowering trees whose fruits treat diabetes.",
            }
        )
        result = validate_draft_plan(
            health_claim,
            packet=packet,
            candidate_paths=set(),
            existing_paths={"Natural Healing/Fruits/Citrus/bergamot.md"},
            claim_policy="integrated",
        )
        self.assertIn("target_0_definition_lead_contains_health_claim", result.issues)

        definition = plan_with_lead(
            {
                "lead_kind": "definition",
                "lead_text": (
                    "Citrus is a genus of flowering trees and shrubs in the family Rutaceae "
                    "whose fruits include oranges, clementines, and grapefruits."
                ),
            }
        )
        result = validate_draft_plan(
            definition,
            packet=packet,
            candidate_paths=set(),
            existing_paths={"Natural Healing/Fruits/Citrus/bergamot.md"},
            claim_policy="integrated",
        )
        self.assertTrue(result.ok, result.issues)
        markdown = local_publish.initial_new_article_markdown(
            definition["target_proposals"][0]
        )
        self.assertIn("**Citrus** is a genus", markdown)
        self.assertNotIn("[^1]", markdown)

    def test_pr_titles_truncate_at_word_boundaries(self) -> None:
        title = (
            "Add research on A functional citrus-based food obtained by membrane "
            "process vs. metformin for the prevention of metabolic syndrome"
        )
        truncated = local_publish.truncate_at_word(title, 72)
        self.assertLessEqual(len(truncated), 72)
        self.assertTrue(truncated.endswith("membrane"))
        self.assertEqual(local_publish.truncate_at_word("short title", 72), "short title")

    def test_documented_claim_policies_match_code_enum(self) -> None:
        doc = (
            Path(__file__).resolve().parents[2] / "docs" / "local-research-publisher.md"
        ).read_text(encoding="utf-8")
        marker = next(
            (
                line
                for line in doc.splitlines()
                if line.startswith("Accepted claim policies:")
            ),
            "",
        )
        self.assertTrue(marker, "docs must declare the accepted claim policies")
        documented = set(re.findall(r"`([a-z_]+)`", marker))
        self.assertEqual(
            documented,
            local_publish.CLAIM_POLICIES,
            "docs/local-research-publisher.md claim-policy set diverges from CLAIM_POLICIES",
        )

    def test_empty_subsection_is_valid_and_nonstring_is_not(self) -> None:
        quote = "The citrus intervention reduced body-weight gain in rats during the feeding period."
        packet = dict(self.packet)
        packet["abstract"] = quote
        packet["study_type"] = "Animal Study"

        def plan_with_subsection(value: object) -> dict:
            return {
                "decision": "append_existing",
                "target_path": "Natural Healing/quercetin.md",
                "parent_heading": "",
                "heading": "## Healing Properties",
                "bullets": [
                    {
                        "text": quote,
                        "source_quote": quote,
                        "evidence_scope": "animal",
                        "subsection": value,
                    }
                ],
            }

        # The plan contract asks for an empty subsection on animal findings.
        empty = validate_draft_plan(
            plan_with_subsection(""),
            packet=packet,
            candidate_paths={"Natural Healing/quercetin.md"},
        )
        self.assertNotIn("bullet_0_invalid_subsection", empty.issues)
        malformed = validate_draft_plan(
            plan_with_subsection(7),
            packet=packet,
            candidate_paths={"Natural Healing/quercetin.md"},
        )
        self.assertIn("bullet_0_invalid_subsection", malformed.issues)

    def test_exclusion_reason_with_independent_justification_is_valid(self) -> None:
        quote = "The greater efficacy may be related to bioactive compounds in the citrus matrix."
        packet = dict(self.packet)
        packet["abstract"] = f"{quote} Another citrus finding about quercetin appears here."

        def plan_with_reason(reason: str) -> dict:
            return {
                "decision": "publish_changes",
                "study_type": "Review",
                "target_proposals": [
                    {
                        "operation": "append_existing",
                        "target_path": "Natural Healing/quercetin.md",
                        "target_entity": "quercetin",
                        "parent_heading": "",
                        "heading": "## Safety",
                        "rationale": "Direct findings.",
                        "bullets": [
                            {
                                "text": "Another citrus finding about quercetin appears here.",
                                "source_quote": "Another citrus finding about quercetin appears here.",
                                "source_section": "Abstract",
                                "claim_kind": "source_finding",
                                "evidence_scope": "review_summary",
                                "cited_references": [],
                            }
                        ],
                        "exclusions": [],
                    }
                ],
                "exclusions": [{"source_quote": quote, "reason": reason}],
            }

        candidates = {
            "Natural Healing/quercetin.md": {
                "path": "Natural Healing/quercetin.md",
                "title": "Quercetin",
            }
        }
        mixed = validate_draft_plan(
            plan_with_reason(
                "Mechanistic hypothesis rather than a direct finding; the direct "
                "finding of broader improvements is already captured."
            ),
            packet=packet,
            candidate_paths=set(candidates),
            candidate_metadata=candidates,
            claim_policy="integrated",
        )
        self.assertNotIn("exclusion_0_contradictory_reason", mixed.issues)
        redundant_only = validate_draft_plan(
            plan_with_reason("This passage is already captured elsewhere."),
            packet=packet,
            candidate_paths=set(candidates),
            candidate_metadata=candidates,
            claim_policy="integrated",
        )
        self.assertIn("exclusion_0_contradictory_reason", redundant_only.issues)

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
                self.assertIn(
                    "target_0_preclinical_heading_scope_warning", result.warnings
                )
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
                    "target_entity": "quercetin",
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
                    "target_entity": "clinical",
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

    def test_missing_citrus_page_is_created_in_existing_category(self) -> None:
        lead_quote = (
            "Citrus fruits include clementines and grapefruits with diverse bioactive compounds."
        )
        finding_quote = (
            "The citrus fruit blend reduced body-weight gain in rats receiving the experimental diet."
        )
        packet = {
            "title": "Effects of a clementine and pink grapefruit blend on metabolic alterations in rats",
            "doi": "10.1000/citrus-rat",
            "abstract": f"{lead_quote} {finding_quote}",
            "body_markdown": f"## Abstract\n\n{lead_quote} {finding_quote}",
            "retrieval_issues": [],
            "reference_url": "https://doi.org/10.1000/citrus-rat",
            "url": "https://example.org/citrus-rat",
            "journal": "Example Journal",
            "pub_date": "2026",
            "study_type": "Animal Study: Rat",
            "authors": "A. Author",
            "keywords": "clementine; pink grapefruit; citrus blend; metabolic",
        }
        plan = {
            "decision": "publish_changes",
            "study_type": "Animal Study: Rat",
            "target_proposals": [
                {
                    "operation": "create_new",
                    "target_path": "Natural Healing/Fruits/Citrus/citrus.md",
                    "target_entity": "citrus",
                    "new_article": {
                        "title": "Citrus",
                        "tags": ["Citrus", "Fruit"],
                        "lead_text": lead_quote,
                        "lead_source_quote": lead_quote,
                        "category_rationale": "Citrus is a fruit family and the category already exists.",
                    },
                    "parent_heading": "",
                    "heading": "## Research",
                    "rationale": "The source directly studies a citrus fruit blend.",
                    "formulation_definition": {
                        "text": lead_quote,
                        "source_quote": lead_quote,
                        "source_section": "Abstract",
                    },
                    "bullets": [
                        {
                            "text": finding_quote,
                            "source_quote": finding_quote,
                            "source_section": "Abstract",
                            "claim_kind": "source_finding",
                            "evidence_scope": "animal",
                            "cited_references": [],
                        }
                    ],
                    "exclusions": [],
                }
            ],
            "exclusions": [],
        }
        approved_critic = {
            "approved": True,
            "recommendation": "approve",
            "issues": [],
            "mode": "required",
            "target_reviews": [],
        }
        missing_entity_tag = json.loads(json.dumps(plan))
        missing_entity_tag["target_proposals"][0]["new_article"]["tags"] = [
            "Metabolic Health"
        ]
        invalid_tags = validate_draft_plan(
            missing_entity_tag,
            packet=packet,
            candidate_paths=set(),
            existing_paths={"Natural Healing/Fruits/Citrus/bergamot.md"},
            domain="Natural Healing",
            claim_policy="integrated",
        )
        self.assertIn(
            "target_0_missing_new_article_entity_tag", invalid_tags.issues
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            content = root / "content"
            content.mkdir()
            article = content / "Natural Healing" / "Fruits" / "Citrus" / "bergamot.md"
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
            client = Mock()
            client.calls = []
            client.json_completion.side_effect = [
                ModelOutputJSONError(
                    "Expecting property name enclosed in double quotes",
                    '{"decision": "publish_changes",}',
                ),
                plan,
            ]
            with patch.object(cli, "CONTENT_INDEX_DIR", root / "cache"), patch.object(
                cli, "scrape_source_packet", return_value=packet
            ), patch.object(
                cli,
                "check_duplicate_paper",
                return_value={"content_hits": [], "paper_result": None},
            ), patch.object(
                local_publish, "LocalLLMClient", return_value=client
            ), patch.object(
                local_publish, "style_context", return_value="guides"
            ), patch.object(
                local_publish, "review_plan_targets", return_value=approved_critic
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
                    domain="Natural Healing",
                )
                created = (
                    Path(report["worktree"])
                    / "Natural Healing/Fruits/Citrus/citrus.md"
                ).read_text(encoding="utf-8")
                report_path = Path(report["report_path"])
                packet_path = Path(report["artifacts"]["packet_json"])
                self.assertTrue(report_path.is_file())
                self.assertTrue(packet_path.is_file())
        self.assertEqual(report["status"], "validated_draft")
        self.assertEqual(report["target_path"], "Natural Healing/Fruits/Citrus/citrus.md")
        self.assertEqual(report["publication_outcome"], "dry_run")
        self.assertEqual(report["format_repairs"][0]["status"], "repaired")
        self.assertEqual(client.json_completion.call_count, 2)
        self.assertIn("title: Citrus", created)
        self.assertIn(f"- {finding_quote}[^1]", created)
        self.assertIn("**Evidence warning — animal/preclinical evidence:**", created)

    def test_compendium_rat_source_supports_independent_background_targets(self) -> None:
        references = {
            "clementine": "[12] Clementine composition review. Journal of Citrus. 2020.",
            "grapefruit": "[13] Grapefruit composition review. Journal of Citrus. 2021.",
            "orange": "[14] Orange composition review. Journal of Citrus. 2022.",
            "citrus": "[15] Citrus composition review. Journal of Citrus. 2023.",
            "lycopene": "[16] Lycopene food sources review. Nutrition Journal. 2024.",
        }
        quotes = {
            "clementine": "Clementine fruit provides flavonoids and vitamin C that contribute to its nutritional composition [12].",
            "grapefruit": "Pink grapefruit contains characteristic carotenoids and flavonoids in its edible tissues [13].",
            "orange": "Orange fruit is a dietary source of vitamin C and several citrus flavonoids [14].",
            "citrus": "Citrus fruits contain diverse flavonoids whose amounts vary among species and cultivars [15].",
            "lycopene": "Lycopene is a carotenoid reported in pink grapefruit and other red-colored foods [16].",
        }
        body = (
            "## Introduction\n\n"
            + "\n\n".join(quotes.values())
            + "\n\nBergamot was only mentioned in the list of search terms."
            + "\n\n## Results\n\nThe citrus intervention reduced body-weight gain in rats receiving the experimental diet."
            + "\n\n## References\n\n"
            + "\n".join(references.values())
        )
        packet = {
            **self.packet,
            "title": "Effects of a clementine and pink grapefruit blend on metabolic alterations in rats",
            "abstract": "A clementine and pink grapefruit blend was administered to rats with diet-induced metabolic alterations for eight weeks.",
            "body_markdown": body,
            "study_type": "Animal Study: Rat",
            "keywords": "clementine; pink grapefruit; citrus blend; metabolic",
        }
        paths = {
            entity: f"Natural Healing/{entity}.md"
            for entity in ("clementine", "grapefruit", "orange", "citrus", "lycopene")
        }
        candidates = {
            path: {"path": path, "title": entity.title()}
            for entity, path in paths.items()
        }
        plan = {
            "decision": "append_existing",
            "study_type": "Animal Study: Rat",
            "target_proposals": [
                {
                    "target_path": paths[entity],
                    "target_entity": entity,
                    "parent_heading": "",
                    "heading": "## Composition",
                    "rationale": f"The passage directly states a background fact about {entity}.",
                    "bullets": [
                        {
                            "text": quotes[entity],
                            "source_quote": quotes[entity],
                            "source_section": "Introduction",
                            "claim_kind": "background_fact",
                            "evidence_scope": "review_summary",
                            "cited_references": [
                                {
                                    "citation_marker": f"[{12 + index}]",
                                    "reference_text": references[entity],
                                    "reference_url": "",
                                }
                            ],
                        }
                    ],
                    "exclusions": [],
                }
                for index, entity in enumerate(paths)
            ],
            "exclusions": [
                {
                    "source_quote": "Bergamot was only mentioned in the list of search terms.",
                    "reason": "A mere mention does not support a Bergamot claim.",
                },
                {
                    "source_quote": "The citrus intervention reduced body-weight gain in rats receiving the experimental diet.",
                    "reason": "The supplied paper studied a blend; no exact existing blend target was provided.",
                },
            ],
        }
        result = validate_draft_plan(
            plan,
            packet=packet,
            candidate_paths=set(candidates),
            candidate_metadata=candidates,
            claim_policy="compendium",
        )
        self.assertTrue(result.ok, result.issues)
        self.assertTrue(
            all(
                warning.endswith("_missing_property_subsection")
                for warning in result.warnings
            ),
            result.warnings,
        )
        self.assertEqual(
            {proposal["target_entity"] for proposal in plan["target_proposals"]},
            {"clementine", "grapefruit", "orange", "citrus", "lycopene"},
        )

        contradictory_exclusion = json.loads(json.dumps(plan))
        contradictory_exclusion["exclusions"][0]["reason"] = (
            "This was already captured and is not excluded."
        )
        invalid = validate_draft_plan(
            contradictory_exclusion,
            packet=packet,
            candidate_paths=set(candidates),
            candidate_metadata=candidates,
            claim_policy="compendium",
        )
        self.assertIn("exclusion_0_contradictory_reason", invalid.issues)

        missing_provenance = {
            **plan,
            "target_proposals": [
                {
                    **plan["target_proposals"][0],
                    "bullets": [
                        {**plan["target_proposals"][0]["bullets"][0], "cited_references": []}
                    ],
                }
            ],
        }
        invalid = validate_draft_plan(
            missing_provenance,
            packet=packet,
            candidate_paths=set(candidates),
            candidate_metadata=candidates,
            claim_policy="compendium",
        )
        self.assertIn(
            "target_0_bullet_0_missing_cited_reference_provenance",
            invalid.issues,
        )
        reference_only = {
            **plan,
            "target_proposals": [
                {
                    **plan["target_proposals"][0],
                    "bullets": [
                        {
                            "text": references["clementine"],
                            "source_quote": references["clementine"],
                            "source_section": "References",
                            "claim_kind": "background_fact",
                            "evidence_scope": "review_summary",
                            "cited_references": [
                                {
                                    "citation_marker": "[12]",
                                    "reference_text": references["clementine"],
                                    "reference_url": "",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        invalid = validate_draft_plan(
            reference_only,
            packet=packet,
            candidate_paths=set(candidates),
            candidate_metadata=candidates,
            claim_policy="compendium",
        )
        self.assertIn(
            "target_0_bullet_0_source_section_not_claim_bearing",
            invalid.issues,
        )

        bergamot_path = "Natural Healing/bergamot.md"
        bergamot_candidate = {bergamot_path: {"path": bergamot_path, "title": "Bergamot"}}
        wrong_entity = {
            "decision": "append_existing",
            "study_type": "Animal Study: Rat",
            "target_proposals": [
                {
                    **plan["target_proposals"][3],
                    "target_path": bergamot_path,
                    "target_entity": "bergamot",
                    "bullets": [
                        {
                            **plan["target_proposals"][3]["bullets"][0],
                            "text": quotes["citrus"],
                            "source_quote": quotes["citrus"],
                        }
                    ],
                }
            ],
            "exclusions": [],
        }
        invalid = validate_draft_plan(
            wrong_entity,
            packet=packet,
            candidate_paths={bergamot_path},
            candidate_metadata=bergamot_candidate,
            claim_policy="compendium",
        )
        self.assertIn(
            "target_0_bullet_0_entity_not_supported_by_passage",
            invalid.issues,
        )
        mention = "Bergamot was only mentioned in the list of search terms."
        mere_mention = {
            **wrong_entity,
            "target_proposals": [
                {
                    **wrong_entity["target_proposals"][0],
                    "bullets": [
                        {
                            "text": mention,
                            "source_quote": mention,
                            "source_section": "Introduction",
                            "claim_kind": "background_fact",
                            "evidence_scope": "review_summary",
                            "cited_references": [],
                        }
                    ],
                }
            ],
        }
        invalid = validate_draft_plan(
            mere_mention,
            packet=packet,
            candidate_paths={bergamot_path},
            candidate_metadata=bergamot_candidate,
            claim_policy="compendium",
        )
        self.assertIn(
            "target_0_bullet_0_entity_not_supported_by_passage",
            invalid.issues,
        )

        misclassified = {
            **plan,
            "target_proposals": [
                {
                    **plan["target_proposals"][0],
                    "bullets": [
                        {
                            **plan["target_proposals"][0]["bullets"][0],
                            "claim_kind": "source_finding",
                            "evidence_scope": "animal",
                            "cited_references": [],
                        }
                    ],
                }
            ],
        }
        invalid = validate_draft_plan(
            misclassified,
            packet=packet,
            candidate_paths=set(candidates),
            candidate_metadata=candidates,
            claim_policy="compendium",
        )
        self.assertIn(
            "target_0_bullet_0_claim_origin_misclassified",
            invalid.issues,
        )

    def test_compendium_preclinical_warning_replaces_heading_rejection(self) -> None:
        background_quote = "Citrus fruits contain diverse flavonoids whose amounts vary among species and cultivars [15]."
        direct_quote = "The citrus intervention reduced body-weight gain in rats receiving the experimental diet."
        reference = "[15] Citrus composition review. Journal of Citrus. 2023."
        packet = {
            **self.packet,
            "title": "Citrus intervention in rats",
            "abstract": "A citrus intervention was administered to rats to evaluate metabolic outcomes during the experimental feeding period.",
            "body_markdown": (
                f"## Introduction\n\n{background_quote}\n\n## Results\n\n{direct_quote}"
                f"\n\n## References\n\n{reference}"
            ),
            "study_type": "Animal Study: Rat",
        }
        plan = {
            "decision": "append_existing",
            "study_type": "Animal Study: Rat",
            "target_proposals": [
                {
                    "target_path": "Natural Healing/citrus.md",
                    "target_entity": "citrus",
                    "parent_heading": "",
                    "heading": "## Composition",
                    "rationale": "Both passages make claims about citrus.",
                    "bullets": [
                        {
                            "text": direct_quote,
                            "source_quote": direct_quote,
                            "source_section": "Results",
                            "claim_kind": "source_finding",
                            "evidence_scope": "animal",
                            "cited_references": [],
                        },
                        {
                            "text": background_quote,
                            "source_quote": background_quote,
                            "source_section": "Introduction",
                            "claim_kind": "background_fact",
                            "evidence_scope": "review_summary",
                            "cited_references": [
                                {
                                    "citation_marker": "[15]",
                                    "reference_text": reference,
                                    "reference_url": "",
                                }
                            ],
                        },
                    ],
                    "exclusions": [],
                }
            ],
            "exclusions": [],
        }
        candidate = {
            "Natural Healing/citrus.md": {
                "path": "Natural Healing/citrus.md",
                "title": "Citrus",
            }
        }
        proposal = plan["target_proposals"][0]
        valid = validate_draft_plan(
            plan,
            packet=packet,
            candidate_paths=set(candidate),
            candidate_metadata=candidate,
            claim_policy="compendium",
        )
        self.assertTrue(valid.ok, valid.issues)
        self.assertIn("target_0_preclinical_heading_scope_warning", valid.warnings)

        strict = validate_draft_plan(
            plan,
            packet=packet,
            candidate_paths=set(candidate),
            candidate_metadata=candidate,
            claim_policy="strict",
        )
        self.assertIn("target_0_preclinical_heading_scope_warning", strict.warnings)

        markdown = "---\ntitle: Citrus\n---\n\n## Composition\n"
        updated = apply_draft_plan(
            markdown, proposal, packet, claim_policy="compendium"
        )
        self.assertIn("**Evidence warning — animal/preclinical evidence:**", updated)
        self.assertIn(f"{direct_quote}[^1]", updated)
        self.assertIn(f"{background_quote}[^1]", updated)
        self.assertEqual(updated.count("**Title:**"), 1)
        self.assertIn("<!-- provenance | claim_kind: source_finding", updated)
        self.assertIn("<!-- provenance | claim_kind: background_fact", updated)
        self.assertIn(reference, updated)
        rendered = validate_rendered_markdown(
            markdown,
            updated,
            plan=proposal,
            packet=packet,
            claim_policy="compendium",
        )
        self.assertTrue(rendered.ok, rendered.issues)

        prompt = local_publish.draft_prompt(
            packet=packet,
            candidates=list(candidate.values()),
            candidate_documents={"Natural Healing/citrus.md": markdown},
            claim_policy="compendium",
        )
        self.assertIn("INTEGRATED / LEGACY COMPENDIUM CLAIM POLICY", prompt)
        self.assertIn("both direct source_finding claims", prompt)
        self.assertIn("normalized source sections", prompt)
        self.assertIn("cited_references", prompt)
        self.assertIn("A citrus blend never belongs on a Bergamot page", prompt)
        self.assertIn("Its lead must be definition-form", prompt)

        repair_prompt = local_publish.draft_prompt(
            packet=packet,
            candidates=list(candidate.values()),
            candidate_documents={"Natural Healing/citrus.md": markdown},
            claim_policy="compendium",
            prior_issues=["target_0_lead_not_near_verbatim"],
        )
        self.assertIn("Do not turn an intervention-specific passage", repair_prompt)

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
                    "operation": "append_existing",
                    "target_entity": "quercetin",
                    "parent_heading": "",
                    "heading": "## Safety",
                    "rationale": "The target is the studied entity.",
                    "bullets": [
                        {
                            "text": quote,
                            "source_quote": quote,
                            "source_section": "Abstract",
                            "claim_kind": "source_finding",
                            "evidence_scope": "review_summary",
                            "cited_references": [],
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

    def _quercetin_valid_plan(self) -> dict:
        quote = (
            "Poor aqueous solubility and extensive metabolism limit the clinical "
            "translation of quercetin."
        )
        return {
            "decision": "append_existing",
            "study_type": "Review",
            "target_proposals": [
                {
                    "target_path": "Natural Healing/quercetin.md",
                    "operation": "append_existing",
                    "target_entity": "quercetin",
                    "parent_heading": "",
                    "heading": "## Safety",
                    "rationale": "The target is the studied entity.",
                    "bullets": [
                        {
                            "text": quote,
                            "source_quote": quote,
                            "source_section": "Abstract",
                            "claim_kind": "source_finding",
                            "evidence_scope": "review_summary",
                            "cited_references": [],
                        }
                    ],
                    "exclusions": [],
                }
            ],
            "exclusions": [],
        }

    def _run_with_mocked_model(
        self,
        *,
        plans: list,
        critic: dict | None = None,
        critics: list | None = None,
        critic_mode: str = "required",
        max_draft_attempts: int = 1,
        pipeline: str = "legacy",
    ) -> tuple[dict, Mock]:
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
            client.calls = []
            client.json_completion.side_effect = plans
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
                local_publish,
                "review_plan_targets",
                **({"side_effect": critics} if critics else {"return_value": critic}),
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
                    max_draft_attempts=max_draft_attempts,
                    critic_mode=critic_mode,
                    pipeline=pipeline,
                )
        return report, client

    def test_simple_pipeline_uses_one_model_call_and_no_repair_loop(self) -> None:
        plan = self._quercetin_valid_plan()
        plan["decision"] = "publish_changes"
        report, client = self._run_with_mocked_model(
            plans=[plan],
            pipeline="simple",
            max_draft_attempts=3,
            critic={
                "approved": True,
                "recommendation": "skipped",
                "issues": [],
                "mode": "off",
            },
        )
        self.assertEqual(report["status"], "validated_draft")
        self.assertEqual(report["pipeline"], "simple")
        self.assertEqual(client.json_completion.call_count, 1)
        self.assertEqual(report["runtime"]["options"]["max_draft_attempts"], 1)

    def test_review_severity_critic_findings_publish_validated_draft(self) -> None:
        revise_critic = {
            "approved": False,
            "recommendation": "revise",
            "issues": [
                {
                    "code": "wrong_heading",
                    "severity": "review",
                    "explanation": "A human should confirm the heading placement.",
                }
            ],
            "mode": "required",
            "target_reviews": [
                {
                    "approved": False,
                    "recommendation": "revise",
                    "target_index": 0,
                    "target_path": "Natural Healing/quercetin.md",
                    "issues": [
                        {
                            "code": "wrong_heading",
                            "severity": "review",
                            "explanation": "A human should confirm the heading placement.",
                        }
                    ],
                    "validation": {"responses_valid": True},
                }
            ],
        }
        report, _client = self._run_with_mocked_model(
            plans=[self._quercetin_valid_plan()],
            critic=revise_critic,
        )
        self.assertEqual(report["status"], "validated_draft")
        self.assertEqual(report["publication_outcome"], "dry_run")
        self.assertIn(
            "published_with_review_findings", report["critic_publication_note"]
        )

    def test_blocking_critic_finding_still_fails_publication_gate(self) -> None:
        blocking_critic = {
            "approved": False,
            "recommendation": "needs_review",
            "issues": [
                {
                    "code": "unsupported_claim",
                    "severity": "blocking",
                    "explanation": "The claim is materially unsupported.",
                }
            ],
            "mode": "required",
            "target_reviews": [
                {
                    "approved": False,
                    "recommendation": "needs_review",
                    "target_index": 0,
                    "target_path": "Natural Healing/quercetin.md",
                    "issues": [
                        {
                            "code": "unsupported_claim",
                            "severity": "blocking",
                            "explanation": "The claim is materially unsupported.",
                        }
                    ],
                    "validation": {"responses_valid": True},
                }
            ],
        }
        report, _client = self._run_with_mocked_model(
            plans=[self._quercetin_valid_plan()],
            critic=blocking_critic,
        )
        self.assertEqual(report["status"], "needs_review")
        self.assertEqual(report["reason"], "critic_quality_gate_failed")

    def test_capitulating_retry_is_asked_for_a_rescoped_plan(self) -> None:
        revise_critic = {
            "approved": False,
            "recommendation": "revise",
            "issues": [
                {
                    "code": "wrong_heading",
                    "severity": "review",
                    "explanation": "A human should confirm the heading placement.",
                }
            ],
            "mode": "required",
            "target_reviews": [
                {
                    "approved": False,
                    "recommendation": "revise",
                    "target_index": 0,
                    "target_path": "Natural Healing/quercetin.md",
                    "issues": [],
                    "validation": {"responses_valid": True},
                }
            ],
        }
        capitulation = {
            "decision": "needs_review",
            "study_type": "Review",
            "target_proposals": [],
            "exclusions": [],
            "review_notes": [],
        }
        report, client = self._run_with_mocked_model(
            plans=[
                self._quercetin_valid_plan(),
                capitulation,
                self._quercetin_valid_plan(),
            ],
            critic=revise_critic,
            max_draft_attempts=3,
        )
        self.assertEqual(len(report["attempt_history"]), 3)
        self.assertEqual(report["status"], "validated_draft")
        third_prompt = client.json_completion.call_args_list[2].kwargs["user"]
        self.assertIn("planner_abandoned_valid_plan", third_prompt)
        self.assertIn("rescope, retarget, or exclude", third_prompt)

    def _limitation_critic(self, quote: str) -> dict:
        finding = {
            "code": "limitation_omitted",
            "severity": "review",
            "bullet_index": 0,
            "explanation": "The bullet omits the source's balancing qualifier.",
            "source_quote": quote,
            "target_index": 0,
            "target_path": "Natural Healing/quercetin.md",
        }
        return {
            "approved": False,
            "recommendation": "revise",
            "issues": [finding],
            "mode": "required",
            "target_reviews": [
                {
                    "approved": False,
                    "recommendation": "revise",
                    "target_index": 0,
                    "target_path": "Natural Healing/quercetin.md",
                    "issues": [finding],
                    "validation": {"responses_valid": True},
                }
            ],
        }

    def _approving_critic(self) -> dict:
        return {
            "approved": True,
            "recommendation": "approve",
            "issues": [],
            "mode": "required",
            "target_reviews": [
                {
                    "approved": True,
                    "recommendation": "approve",
                    "target_index": 0,
                    "target_path": "Natural Healing/quercetin.md",
                    "issues": [],
                    "validation": {"responses_valid": True},
                }
            ],
        }

    def test_repairable_balance_finding_gets_one_repair_pass_and_publishes(self) -> None:
        qualifier = "Clinical study results remain heterogeneous."
        repaired = self._quercetin_valid_plan()
        repaired["target_proposals"][0]["bullets"].append(
            {
                "text": qualifier,
                "source_quote": qualifier,
                "source_section": "Abstract",
                "claim_kind": "source_finding",
                "evidence_scope": "review_summary",
                "cited_references": [],
            }
        )
        report, client = self._run_with_mocked_model(
            plans=[self._quercetin_valid_plan(), repaired],
            critics=[self._limitation_critic(qualifier), self._approving_critic()],
        )
        self.assertEqual(report["status"], "validated_draft")
        self.assertEqual(report["balance_repair"]["status"], "repaired")
        bullets = report["plan"]["target_proposals"][0]["bullets"]
        self.assertEqual(len(bullets), 2)
        self.assertEqual(bullets[1]["text"], qualifier)
        self.assertEqual(client.json_completion.call_count, 2)
        repair_prompt = client.json_completion.call_args_list[1].kwargs["user"]
        self.assertIn("VALIDATED_BALANCE_FINDINGS", repair_prompt)
        self.assertIn("Append ONE new bullet", repair_prompt)

    def test_unrepairable_balance_finding_downgrades_to_needs_review(self) -> None:
        report, client = self._run_with_mocked_model(
            plans=[self._quercetin_valid_plan()],
            critics=[self._limitation_critic("This qualifier is not in the source text.")],
        )
        self.assertEqual(report["status"], "needs_review")
        self.assertEqual(report["reason"], "balance_finding_unresolved")
        self.assertEqual(report["balance_repair"]["status"], "unrepairable")
        self.assertEqual(client.json_completion.call_count, 1)

    def test_failed_balance_repair_retains_prerepair_attempt(self) -> None:
        qualifier = "Clinical study results remain heterogeneous."
        rewritten = self._quercetin_valid_plan()
        rewritten["target_proposals"][0]["bullets"] = [
            {
                "text": qualifier,
                "source_quote": qualifier,
                "source_section": "Abstract",
                "claim_kind": "source_finding",
                "evidence_scope": "review_summary",
                "cited_references": [],
            }
        ]
        report, _client = self._run_with_mocked_model(
            plans=[self._quercetin_valid_plan(), rewritten],
            critics=[self._limitation_critic(qualifier)],
        )
        self.assertEqual(report["status"], "needs_review")
        self.assertEqual(report["reason"], "balance_finding_unresolved")
        self.assertEqual(report["balance_repair"]["status"], "failed")
        self.assertEqual(len(report["plan"]["target_proposals"][0]["bullets"]), 1)
        self.assertEqual(
            report["plan"]["target_proposals"][0]["bullets"][0]["text"],
            self._quercetin_valid_plan()["target_proposals"][0]["bullets"][0]["text"],
        )
        self.assertIn(
            "balance_repair_dropped_or_rewrote_bullet",
            report["attempt_history"][-1]["repair_scope_validation"]["issues"],
        )

    def test_qualifier_waivers_are_scoped_to_repair_bullets(self) -> None:
        findings = [
            {
                "code": "limitation_omitted",
                "severity": "review",
                "source_quote": "Although effects were less pronounced, potential was shown.",
            }
        ]
        plan = {
            "decision": "publish_changes",
            "target_proposals": [
                {
                    "target_path": "Natural Healing/citrus.md",
                    "bullets": [
                        {"source_quote": "Although effects were less pronounced, potential was shown."},
                        {"source_quote": "An unrelated bullet quote."},
                    ],
                }
            ],
        }
        deterministic = local_publish.ValidationResult(
            False,
            [
                "target_0_bullet_0_entity_not_supported_by_passage",
                "target_0_bullet_1_entity_not_supported_by_passage",
                "target_0_invalid_bullet_count",
                "target_0_bullet_1_text_not_near_verbatim",
            ],
            [],
        )
        waived = local_publish.waive_qualifier_entity_issues(deterministic, plan, findings)
        self.assertNotIn("target_0_bullet_0_entity_not_supported_by_passage", waived.issues)
        self.assertNotIn("target_0_invalid_bullet_count", waived.issues)
        self.assertIn("target_0_bullet_1_entity_not_supported_by_passage", waived.issues)
        self.assertIn("target_0_bullet_1_text_not_near_verbatim", waived.issues)
        self.assertIn(
            "target_0_invalid_bullet_count_waived_balance_qualifier", waived.warnings
        )

    def test_advisory_mode_skips_balance_repair(self) -> None:
        report, client = self._run_with_mocked_model(
            plans=[self._quercetin_valid_plan()],
            critics=[self._limitation_critic("Clinical study results remain heterogeneous.")],
            critic_mode="advisory",
        )
        self.assertEqual(report["status"], "validated_draft")
        self.assertIsNone(report["balance_repair"])
        self.assertEqual(client.json_completion.call_count, 1)

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

    def test_invalid_critic_response_recommends_needs_review(self) -> None:
        placement = {
            "kind": "placement",
            "response_valid": False,
            "issues": [],
            "rejected_issues": [],
        }
        evidence = {
            "kind": "evidence",
            "response_valid": True,
            "issues": [],
            "rejected_issues": [],
        }
        invalid = combine_critic_reviews(placement, evidence)
        self.assertFalse(invalid["approved"])
        self.assertEqual(invalid["recommendation"], "needs_review")

    def test_critic_blocks_publication_only_on_blocking_or_invalid(self) -> None:
        self.assertFalse(critic_blocks_publication({"approved": True}))
        review_only = {
            "approved": False,
            "recommendation": "revise",
            "target_reviews": [
                {"approved": False, "recommendation": "revise"},
                {"approved": True, "recommendation": "approve"},
            ],
        }
        self.assertFalse(critic_blocks_publication(review_only))
        blocking = {
            "approved": False,
            "target_reviews": [{"approved": False, "recommendation": "needs_review"}],
        }
        self.assertTrue(critic_blocks_publication(blocking))
        self.assertTrue(
            critic_blocks_publication({"approved": False, "target_reviews": []})
        )
        self.assertTrue(critic_blocks_publication({"approved": False}))

    def test_seeded_catch_all_demotes_entity_scope_objection(self) -> None:
        quote = (
            "Consumption of the citrus concentrates improved glucose tolerance in rats."
        )
        packet = dict(self.packet)
        packet["abstract"] = quote
        plan = {
            "operation": "create_new",
            "target_entity": "citrus",
            "bullets": [{"text": quote, "source_quote": quote}],
        }
        review = {
            "issues": [
                {
                    "code": "entity_not_supported",
                    "severity": "blocking",
                    "bullet_index": 0,
                    "explanation": (
                        "The claim concerns a processed concentrate, not the general "
                        "citrus fruit entity."
                    ),
                    "source_quote": quote,
                    "target_quote": "",
                }
            ]
        }
        demoted = validate_critic_review(
            review,
            review_kind="placement",
            packet=packet,
            plan=plan,
            selected_target_markdown="",
            seeded_catch_all=True,
        )
        self.assertEqual(len(demoted["issues"]), 1)
        self.assertEqual(demoted["issues"][0]["severity"], "warning")
        self.assertIn(
            "severity_demoted_from_blocking_seeded_catch_all_scope",
            demoted["issues"][0]["validation_warnings"],
        )
        combined = combine_critic_reviews(
            demoted,
            {
                "kind": "evidence",
                "response_valid": True,
                "issues": [],
                "rejected_issues": [],
            },
        )
        self.assertTrue(combined["approved"])
        unseeded = validate_critic_review(
            review,
            review_kind="placement",
            packet=packet,
            plan=plan,
            selected_target_markdown="",
            seeded_catch_all=False,
        )
        self.assertEqual(unseeded["issues"][0]["severity"], "blocking")

    def test_overclaim_objection_against_near_verbatim_bullet_is_demoted(self) -> None:
        quote = (
            "Diets rich in citrus fruits have been associated with improved "
            "glycemic control and a reduced risk of several chronic diseases."
        )
        packet = dict(self.packet)
        packet["abstract"] = quote
        plan = {"bullets": [{"text": quote, "source_quote": quote}]}
        issue = {
            "code": "medical_overclaim",
            "severity": "blocking",
            "bullet_index": 0,
            "explanation": (
                "Asserting reduced disease risk from associative evidence is an "
                "overclaim in this context."
            ),
            "source_quote": quote,
            "target_quote": "",
        }
        demoted = validate_critic_review(
            {"issues": [dict(issue)]},
            review_kind="evidence",
            packet=packet,
            plan=plan,
            selected_target_markdown="## Research\n",
        )
        self.assertEqual(demoted["issues"][0]["severity"], "warning")
        self.assertIn(
            "severity_demoted_from_blocking_near_verbatim_bullet",
            demoted["issues"][0]["validation_warnings"],
        )
        contextual = dict(issue)
        contextual["target_quote"] = "## Research"
        kept = validate_critic_review(
            {"issues": [contextual]},
            review_kind="evidence",
            packet=packet,
            plan=plan,
            selected_target_markdown="## Research\n",
        )
        self.assertEqual(kept["issues"][0]["severity"], "blocking")

    def test_unrelated_entity_objection_survives_seeded_catch_all(self) -> None:
        quote = "Metformin remains the reference treatment for type 2 diabetes."
        packet = dict(self.packet)
        packet["abstract"] = quote
        plan = {
            "operation": "create_new",
            "target_entity": "citrus",
            "bullets": [{"text": quote, "source_quote": quote}],
        }
        review = {
            "issues": [
                {
                    "code": "entity_not_supported",
                    "severity": "blocking",
                    "bullet_index": 0,
                    "explanation": "The quote concerns metformin, not citrus.",
                    "source_quote": quote,
                    "target_quote": "",
                }
            ]
        }
        validated = validate_critic_review(
            review,
            review_kind="placement",
            packet=packet,
            plan=plan,
            selected_target_markdown="",
            seeded_catch_all=True,
        )
        self.assertEqual(validated["issues"][0]["severity"], "blocking")

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
        with self.assertRaisesRegex(ValueError, "claim_policy must be one of"):
            run_local_publish(
                **common,
                publish=False,
                claim_policy="background-everything",
            )


class CitationRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        self.packet = {
            "title": (
                "A functional citrus-based food obtained by membrane process vs. metformin "
                "for the prevention of metabolic syndrome/type 2 diabetes in rats - ScienceDirect"
            ),
            "doi": "10.1016/j.foodres.2026.120323",
            "abstract": "A membrane processing approach was used in fructose-fed rats. " * 12,
            "body_markdown": "Full source evidence. " * 100,
            "retrieval_issues": [],
            "reference_url": "https://doi.org/10.1016/j.foodres.2026.120323",
            "url": "https://www.sciencedirect.com/science/article/pii/S0963996926020107",
            "journal": "Food Research International",
            "pub_date": "2026/08/07",
            "study_type": "Research Article",
            "authors": "C. Dhuique-Mayer, K. Lambert",
            "external_metadata": {
                "crossref": {
                    "title": (
                        "A functional citrus-based food obtained by membrane process vs. metformin "
                        "for the prevention of metabolic syndrome/type 2 diabetes in rats"
                    )
                }
            },
        }
        self.plan = {
            "heading": "## Healing Properties",
            "parent_heading": "",
            "bullets": [
                {
                    "text": "Consumption of the citrus concentrates improved glucose tolerance.",
                    "source_quote": "Consumption of the citrus concentrates improved glucose tolerance.",
                    "source_section": "Abstract",
                    "claim_kind": "source_finding",
                    "cited_references": [],
                },
                {
                    "text": "Diets rich in citrus fruits have been associated with improved glycemic control.",
                    "source_quote": (
                        "Diets rich in citrus fruits have been associated with improved glycemic "
                        "control ([Aruoma et al., 2012](#bb0010))."
                    ),
                    "source_section": "1. Introduction",
                    "claim_kind": "background_fact",
                    "cited_references": [
                        {
                            "citation_marker": "[Aruoma et al., 2012](#bb0010)",
                            "reference_text": (
                                "2. [Aruoma et al., 2012](#bbb0010) O.I. Aruoma et al. Functional "
                                "benefits of citrus fruits. Preventive Medicine, 54 (2012). "
                                "https://doi.org/10.1016/j.ypmed.2011.12.012"
                            ),
                            "reference_url": "",
                        }
                    ],
                },
            ],
        }
        self.markdown = "---\ntitle: Citrus\n---\n\n**Citrus** fruits.[^1]\n\n## Healing Properties\n"

    def test_single_footnote_per_source_with_adjacent_provenance_comments(self) -> None:
        updated = apply_draft_plan(self.markdown, self.plan, self.packet, claim_policy="integrated")
        self.assertEqual(updated.count("**Title:**"), 1)
        self.assertIn("glucose tolerance.[^1]", updated)
        self.assertIn("glycemic control.[^1]", updated)
        lines = updated.splitlines()
        for index, line in enumerate(lines):
            if line.startswith("- "):
                self.assertTrue(
                    lines[index + 1].startswith("  <!-- provenance | claim_kind:"),
                    lines[index + 1],
                )
        rendered = validate_rendered_markdown(
            self.markdown, updated, plan=self.plan, packet=self.packet, claim_policy="integrated"
        )
        self.assertTrue(rendered.ok, rendered.issues)

    def test_supplied_paper_findings_carry_full_provenance_fields(self) -> None:
        updated = apply_draft_plan(self.markdown, self.plan, self.packet, claim_policy="integrated")
        self.assertIn(
            "<!-- provenance | claim_kind: source_finding | source_section: Abstract | "
            "source_quote: Consumption of the citrus concentrates improved glucose tolerance. | "
            "cited_references: none -->",
            updated,
        )

    def test_repeated_application_reuses_the_existing_source_footnote(self) -> None:
        first = apply_draft_plan(self.markdown, self.plan, self.packet, claim_policy="integrated")
        second_plan = {
            "heading": "## Composition",
            "parent_heading": "",
            "bullets": [
                {
                    "text": "Citrus juices are nutrient-dense beverages.",
                    "source_quote": "Citrus juices are nutrient-dense beverages.",
                    "source_section": "1. Introduction",
                    "claim_kind": "background_fact",
                    "cited_references": [],
                }
            ],
        }
        second = apply_draft_plan(
            first + "\n## Composition\n", second_plan, self.packet, claim_policy="integrated"
        )
        self.assertIn("nutrient-dense beverages.[^1]", second)
        self.assertEqual(second.count("**Title:**"), 1)
        rendered = validate_rendered_markdown(
            first, second, plan=second_plan, packet=self.packet, claim_policy="integrated"
        )
        self.assertNotIn("duplicate_source_footnote", rendered.issues)

    def test_study_type_classified_from_content_over_publisher_label(self) -> None:
        updated = apply_draft_plan(self.markdown, self.plan, self.packet, claim_policy="integrated")
        self.assertIn("**Study Type:** Animal Study<br>", updated)
        self.assertNotIn("Research Article", updated)
        self.assertEqual(
            local_publish.classify_study_type(
                {"title": "Vitamin D supplementation: a randomized controlled trial in adults"}
            ),
            "Human Study",
        )
        self.assertEqual(
            local_publish.classify_study_type(
                {"title": "Polyphenols and gut health: a systematic review and meta-analysis"}
            ),
            "Meta Analysis",
        )
        self.assertEqual(
            local_publish.classify_study_type({"title": "Curcumin effects in cultured cells"}),
            "In Vitro",
        )
        self.assertEqual(
            local_publish.classify_study_type({"title": "An untyped source", "abstract": ""}),
            "",
        )

    def test_publisher_suffix_stripped_and_title_links_doi(self) -> None:
        updated = apply_draft_plan(self.markdown, self.plan, self.packet, claim_policy="integrated")
        self.assertNotIn(" - ScienceDirect", updated)
        self.assertIn(
            "**Title:** [A functional citrus-based food obtained by membrane process vs. metformin "
            "for the prevention of metabolic syndrome/type 2 diabetes in rats]"
            "(https://doi.org/10.1016/j.foodres.2026.120323)",
            updated,
        )

    def test_citation_dates_are_normalized_to_iso(self) -> None:
        self.assertEqual(local_publish.normalize_citation_date("2026/08/07"), "2026-08-07")
        self.assertEqual(local_publish.normalize_citation_date("2026-8-7"), "2026-08-07")
        self.assertEqual(local_publish.normalize_citation_date("2026-08"), "2026-08")
        self.assertEqual(local_publish.normalize_citation_date("2026"), "2026")
        self.assertEqual(local_publish.normalize_citation_date("Unknown"), "Unknown")
        updated = apply_draft_plan(self.markdown, self.plan, self.packet, claim_policy="integrated")
        self.assertIn("**Date:** 2026-08-07<br>", updated)

    def test_cited_reference_anchors_become_plain_text_with_doi_links(self) -> None:
        updated = apply_draft_plan(self.markdown, self.plan, self.packet, claim_policy="integrated")
        comment = next(
            line for line in updated.splitlines() if "claim_kind: background_fact" in line
        )
        cited_segment = comment.split("cited_references:", 1)[1]
        self.assertNotIn("](#", cited_segment)
        self.assertIn("Aruoma et al., 2012: 2. Aruoma et al., 2012 O.I. Aruoma et al.", cited_segment)
        self.assertIn("(https://doi.org/10.1016/j.ypmed.2011.12.012)", cited_segment)

    def test_dead_anchor_gate_rejects_untargeted_fragment_links(self) -> None:
        updated = apply_draft_plan(self.markdown, self.plan, self.packet, claim_policy="integrated")
        broken = updated.replace(
            "glycemic control.[^1]",
            "glycemic control ([Aruoma et al., 2012](#bb0010)).[^1]",
        )
        rendered = validate_rendered_markdown(
            self.markdown, broken, plan=self.plan, packet=self.packet, claim_policy="integrated"
        )
        self.assertIn("dead_anchor_link_bb0010", rendered.issues)
        with_heading_link = updated.replace(
            "**Citrus** fruits.[^1]",
            "**Citrus** fruits ([details](#healing-properties)).[^1]",
        )
        self.assertEqual(local_publish.dead_anchor_links(with_heading_link), [])
        self.assertEqual(local_publish.dead_anchor_links(updated), [])

    def test_published_critic_findings_persist_as_bullet_adjacent_comments(self) -> None:
        findings = [
            {
                "code": "limitation_omitted",
                "severity": "review",
                "bullet_index": 0,
                "explanation": "The efficacy claim omits the weaker-than-metformin qualifier.",
                "source_quote": "generally less pronounced than those observed with metformin",
            },
            {
                "code": "wrong_heading",
                "severity": "review",
                "explanation": "A human should confirm the heading placement.",
            },
            {
                "code": "duplicate_content",
                "severity": "warning",
                "explanation": "Non-gating note that must not be rendered.",
            },
        ]
        updated = apply_draft_plan(
            self.markdown, self.plan, self.packet,
            claim_policy="integrated", critic_findings=findings,
        )
        lines = updated.splitlines()
        index = next(i for i, line in enumerate(lines) if line.startswith("- Consumption"))
        self.assertTrue(lines[index + 1].startswith("  <!-- provenance |"))
        self.assertTrue(
            lines[index + 2].startswith("  <!-- critic | severity: review | code: limitation_omitted"),
            lines[index + 2],
        )
        self.assertIn("\n<!-- critic | severity: review | code: wrong_heading", updated)
        self.assertNotIn("duplicate_content", updated)
        rendered = validate_rendered_markdown(
            self.markdown, updated, plan=self.plan, packet=self.packet,
            claim_policy="integrated", critic_findings=findings,
        )
        self.assertTrue(rendered.ok, rendered.issues)
        without_annotations = apply_draft_plan(
            self.markdown, self.plan, self.packet, claim_policy="integrated"
        )
        missing = validate_rendered_markdown(
            self.markdown, without_annotations, plan=self.plan, packet=self.packet,
            claim_policy="integrated", critic_findings=findings,
        )
        self.assertIn("bullet_0_critic_annotation_missing", missing.issues)
        self.assertIn("target_critic_annotation_missing", missing.issues)

    def _scoped_plan(self) -> dict:
        return {
            "heading": "## Healing Properties",
            "parent_heading": "",
            "formulation_definition": {
                "text": (
                    "A citrus formulation composed of clementine and pink grapefruit "
                    "was assessed in fructose-fed rats."
                ),
                "source_quote": (
                    "A citrus formulation composed of clementine and pink grapefruit "
                    "was assessed in fructose-fed rats."
                ),
                "source_section": "Abstract",
            },
            "bullets": [
                {
                    "text": "Consumption of the citrus concentrates improved glucose tolerance.",
                    "source_quote": "Consumption of the citrus concentrates improved glucose tolerance.",
                    "source_section": "Abstract",
                    "claim_kind": "source_finding",
                    "evidence_scope": "animal",
                    "cited_references": [],
                },
                {
                    "text": "The citrus concentrates enhanced hepatic vitamin A stores in prediabetic rats.",
                    "source_quote": "The citrus concentrates enhanced hepatic vitamin A stores in prediabetic rats.",
                    "source_section": "Abstract",
                    "claim_kind": "source_finding",
                    "evidence_scope": "animal",
                    "cited_references": [],
                },
                {
                    "text": "Diets rich in citrus fruits have been associated with improved glycemic control.",
                    "source_quote": "Diets rich in citrus fruits have been associated with improved glycemic control.",
                    "source_section": "1. Introduction",
                    "claim_kind": "background_fact",
                    "evidence_scope": "review_summary",
                    "subsection": "Glycemic Control",
                    "cited_references": [],
                },
            ],
        }

    def test_bullets_group_into_evidence_tier_subsections(self) -> None:
        plan = self._scoped_plan()
        updated = apply_draft_plan(self.markdown, plan, self.packet, claim_policy="integrated")
        lines = updated.splitlines()
        animal_heading = lines.index("### Preclinical Evidence (Animal Studies)")
        background_heading = lines.index("### Glycemic Control")
        self.assertLess(animal_heading, background_heading)
        warning_index = next(
            i for i, line in enumerate(lines) if line.startswith("> **Evidence warning")
        )
        self.assertLess(animal_heading, warning_index)
        self.assertLess(warning_index, background_heading)
        background_bullet = next(
            i for i, line in enumerate(lines) if line.startswith("- Diets rich")
        )
        self.assertGreater(background_bullet, background_heading)
        rendered = validate_rendered_markdown(
            self.markdown, updated, plan=plan, packet=self.packet, claim_policy="integrated"
        )
        self.assertTrue(rendered.ok, rendered.issues)

    def test_animal_bullets_without_species_cue_get_standard_prefix(self) -> None:
        plan = self._scoped_plan()
        updated = apply_draft_plan(self.markdown, plan, self.packet, claim_policy="integrated")
        self.assertIn(
            "- In fructose-fed rats, consumption of the citrus concentrates improved "
            "glucose tolerance.[^1]",
            updated,
        )
        self.assertIn(
            "- The citrus concentrates enhanced hepatic vitamin A stores in prediabetic rats.[^1]",
            updated,
        )
        self.assertIn(
            "- Diets rich in citrus fruits have been associated with improved glycemic control.[^1]",
            updated,
        )

    def test_formulation_definition_opens_the_animal_subsection(self) -> None:
        plan = self._scoped_plan()
        updated = apply_draft_plan(self.markdown, plan, self.packet, claim_policy="integrated")
        lines = updated.splitlines()
        heading = lines.index("### Preclinical Evidence (Animal Studies)")
        definition = next(
            i for i, line in enumerate(lines) if line.startswith("A citrus formulation composed")
        )
        warning = next(
            i for i, line in enumerate(lines) if line.startswith("> **Evidence warning")
        )
        first_bullet = next(i for i, line in enumerate(lines) if line.startswith("- "))
        self.assertLess(heading, definition)
        self.assertLess(definition, warning)
        self.assertLess(warning, first_bullet)
        misplaced = updated.replace(
            "A citrus formulation composed of clementine and pink grapefruit was assessed "
            "in fructose-fed rats.[^1]\n",
            "",
        )
        gated = validate_rendered_markdown(
            self.markdown, misplaced, plan=plan, packet=self.packet, claim_policy="integrated"
        )
        self.assertIn("formulation_definition_missing_or_misplaced", gated.issues)

    def test_flat_before_fixture_fails_gate_and_grouped_after_passes(self) -> None:
        plan = self._scoped_plan()
        before = (
            self.markdown.rstrip()
            + "\n\n> **Evidence warning — animal/preclinical evidence:** These findings do "
            "not by themselves establish effects in humans.\n\n"
            "- Consumption of the citrus concentrates improved glucose tolerance.[^1]\n"
            "- Diets rich in citrus fruits have been associated with improved glycemic "
            "control.[^1]\n\n"
            "[^1]: **Title:** [t](https://doi.org/10.1016/j.foodres.2026.120323)<br>\n"
            "**DOI:** [10.1016/j.foodres.2026.120323]"
            "(https://doi.org/10.1016/j.foodres.2026.120323)<br>\n"
            "**Source URL:** [u](https://www.sciencedirect.com/science/article/pii/"
            "S0963996926020107)\n"
        )
        gated = validate_rendered_markdown(
            self.markdown, before, plan=plan, packet=self.packet, claim_policy="integrated"
        )
        self.assertIn("bullet_outside_property_subsection", gated.issues)
        self.assertIn("animal_warning_outside_subsection", gated.issues)
        after = apply_draft_plan(self.markdown, plan, self.packet, claim_policy="integrated")
        rendered = validate_rendered_markdown(
            self.markdown, after, plan=plan, packet=self.packet, claim_policy="integrated"
        )
        self.assertTrue(rendered.ok, rendered.issues)

    def test_multi_sentence_bullets_split_into_one_idea_bullets(self) -> None:
        plan = self._scoped_plan()
        plan["bullets"][2]["text"] = (
            "Citrus fruits are also important dietary sources of carotenoids. "
            "Provitamin A carotenoids are abundant in many mandarin varieties."
        )
        plan["bullets"][2]["source_quote"] = plan["bullets"][2]["text"]
        updated = apply_draft_plan(self.markdown, plan, self.packet, claim_policy="integrated")
        self.assertIn(
            "- Citrus fruits are also important dietary sources of carotenoids.[^1]", updated
        )
        self.assertIn(
            "- Provitamin A carotenoids are abundant in many mandarin varieties.[^1]", updated
        )
        rendered = validate_rendered_markdown(
            self.markdown, updated, plan=plan, packet=self.packet, claim_policy="integrated"
        )
        self.assertTrue(rendered.ok, rendered.issues)
        fused = updated.replace(
            "- Citrus fruits are also important dietary sources of carotenoids.[^1]",
            "- Citrus fruits are also important dietary sources of carotenoids. "
            "Provitamin A carotenoids are abundant in many mandarin varieties.[^1]",
        )
        gated = validate_rendered_markdown(
            self.markdown, fused, plan=plan, packet=self.packet, claim_policy="integrated"
        )
        self.assertIn("bullet_not_single_idea", gated.issues)

    def test_abstract_modes_truncate_or_omit_published_abstract(self) -> None:
        truncated = apply_draft_plan(
            self.markdown, self.plan, self.packet, claim_policy="integrated", abstract_mode="truncated"
        )
        abstract_line = next(
            line for line in truncated.splitlines() if line.startswith("**Abstract:**")
        )
        self.assertLessEqual(len(abstract_line), len("**Abstract:** ") + 510)
        self.assertTrue(abstract_line.endswith("[…]<br>"))
        omitted = apply_draft_plan(
            self.markdown, self.plan, self.packet, claim_policy="integrated", abstract_mode="omit"
        )
        self.assertNotIn("**Abstract:**", omitted)
        rendered = validate_rendered_markdown(
            self.markdown, omitted, plan=self.plan, packet=self.packet, claim_policy="integrated"
        )
        self.assertTrue(rendered.ok, rendered.issues)


if __name__ == "__main__":
    unittest.main()
