from __future__ import annotations

import json
import hashlib
import io
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import yaml


INVALID_PACKET_ISSUES = {
    "invalid_title",
    "captcha_page",
    "robot_page",
    "challenge_page",
    "publisher_error_page",
    "doi_title_mismatch",
}
INVALID_TITLE_PATTERN = re.compile(
    r"^(?:are you a robot\??|access denied|just a moment\.?|new tab|unknown title)$",
    re.IGNORECASE,
)
ALLOWED_DECISIONS = {
    "publish_changes",
    "append_existing",
    "create_new",
    "duplicate",
    "needs_review",
}
CHANGE_DECISIONS = {"publish_changes", "append_existing", "create_new"}
PROPOSAL_OPERATIONS = {"append_existing", "create_new"}
ALLOWED_EVIDENCE_SCOPES = {
    "review_summary",
    "human",
    "animal",
    "in_vitro",
    "mechanistic",
    "composition",
}
DOI_PATTERN = re.compile(r"^10\.\d{4,9}/[-._;()/:a-z0-9]+$", re.IGNORECASE)
CRITIC_MODES = {"required", "advisory", "off"}
CLAIM_POLICIES = {"integrated", "strict", "compendium"}
ABSTRACT_MODES = {"full", "truncated", "omit"}
CLAIM_KINDS = {"source_finding", "background_fact"}
MAX_SOURCE_CLAIM_CONTEXT_CHARS = 90000
MAX_SOURCE_REFERENCE_CONTEXT_CHARS = 60000
MAX_CANDIDATE_CONTEXT_CHARS = 20000
MAX_ALL_CANDIDATE_CONTEXT_CHARS = 80000
MAX_DRAFT_OUTPUT_TOKENS = 10000
CRITIC_SEVERITIES = {"warning", "review", "blocking"}
CRITIC_SEVERITY_RANK = {"warning": 0, "review": 1, "blocking": 2}
PLACEMENT_ISSUE_CODES = {
    "wrong_target_page",
    "entity_not_supported",
    "wrong_heading",
    "existing_content_conflict",
    "duplicate_content",
    "unsafe_context_inference",
    "new_article_not_warranted",
    "wrong_category",
}
EVIDENCE_ISSUE_CODES = {
    "unsupported_claim",
    "missing_source_provenance",
    "claim_origin_misclassified",
    "medical_overclaim",
    "study_type_inflation",
    "merged_ideas",
    "source_integrity_concern",
    "limitation_omitted",
}
# Balance/omitted-qualifier finding classes: a validated finding of one of
# these codes must be repaired or the run must not publish, because the
# missing qualifier would otherwise survive only in the PR body and be lost
# on merge.
BALANCE_FINDING_CODES = {"limitation_omitted"}
MINIMUM_CRITIC_SEVERITY = {
    "wrong_target_page": "review",
    "entity_not_supported": "review",
    "wrong_heading": "review",
    "existing_content_conflict": "review",
    "duplicate_content": "review",
    "unsafe_context_inference": "review",
    "new_article_not_warranted": "review",
    "wrong_category": "review",
    "unsupported_claim": "review",
    "missing_source_provenance": "review",
    "claim_origin_misclassified": "review",
    "medical_overclaim": "review",
    "study_type_inflation": "review",
    "source_integrity_concern": "review",
    "limitation_omitted": "review",
}
PLACEHOLDER_METADATA = {
    "authors": {"author", "authors", "author s", "authors and affiliations", "unknown", "n a"},
    "journal": {"journal", "ovid", "publisher", "source", "unknown", "n a"},
    "pub_date": {"date", "published", "unknown", "n a"},
}


@dataclass
class ValidationResult:
    ok: bool
    issues: list[str]
    warnings: list[str] = field(default_factory=list)


def normalize_evidence(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def includes_background_claims(claim_policy: str) -> bool:
    """Integrated is the production policy; compendium is a legacy alias."""
    return claim_policy in {"integrated", "compendium"}


def valid_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def placeholder_metadata(key: str, value: str) -> bool:
    return not value.strip() or normalize_evidence(value) in PLACEHOLDER_METADATA.get(key, set())


def source_evidence(packet: dict[str, Any]) -> str:
    return "\n\n".join(
        str(packet.get(key) or "")
        for key in ("title", "abstract", "body_markdown")
    )


def source_is_preclinical(packet: dict[str, Any]) -> bool:
    """Return whether packet metadata explicitly describes non-human evidence."""
    source_kind = normalize_evidence(
        " ".join(
            str(packet.get(key) or "")
            for key in ("title", "abstract", "study_type", "keywords")
        )
    )
    return bool(
        re.search(
            r"\b(?:animal|animals|preclinical|in vivo|rat|rats|mouse|mice)\b",
            source_kind,
        )
    )


def study_type_matches_packet(plan_study_type: str, packet: dict[str, Any]) -> bool:
    planned = normalize_evidence(plan_study_type)
    recorded = normalize_evidence(str(packet.get("study_type") or ""))
    if planned == recorded:
        return True
    classified = normalize_evidence(classify_study_type(packet))
    if planned and planned == classified:
        return True
    generic_recorded = recorded in {"article", "research article", "original article"}
    preclinical_planned = bool(
        re.search(r"\b(?:animal|preclinical|in vivo|rat|rats|mouse|mice)\b", planned)
    )
    return generic_recorded and source_is_preclinical(packet) and preclinical_planned


def exact_source_passage(packet: dict[str, Any], passage: str) -> bool:
    """Return whether a claim passage is an exact contiguous source span."""
    return bool(passage and passage in source_evidence(packet))


def source_section_present(packet: dict[str, Any], section: str) -> bool:
    """Match a declared section to an extracted Markdown/plain-text heading."""
    normalized_section = normalize_evidence(section)
    if normalized_section in {"title", "abstract"}:
        return bool(str(packet.get(normalized_section) or "").strip())
    if not normalized_section:
        return False
    for line in str(packet.get("body_markdown") or "").splitlines():
        stripped = line.strip()
        is_markdown_heading = bool(re.match(r"^#{1,6}\s+", stripped))
        normalized_line = normalize_evidence(re.sub(r"^#{1,6}\s*", "", stripped))
        is_plain_heading = normalized_line == normalized_section
        if is_plain_heading or (
            is_markdown_heading
            and (
                normalized_section == normalized_line
                or phrase_in_text(normalized_section, normalized_line)
            )
        ):
            return True
    return False


def cited_markers(passage: str) -> list[str]:
    """Extract common numeric and author-year citations from one source passage."""
    markdown_markers = re.findall(
        r"\[[^\]\n]*(?:19|20)\d{2}[a-z]?\]\(#[^)]+\)",
        passage,
    )
    markers = re.findall(
        r"\[(?:\d+(?:\s*[-,]\s*\d+)*)\]"
        r"|\((?:[A-Z][A-Za-z'’-]+(?:\s+et\s+al\.)?\s*,?\s*)?(?:19|20)\d{2}[a-z]?\)",
        passage,
    )
    return list(dict.fromkeys([*markdown_markers, *markers]))


def canonical_citation_marker(value: str) -> str:
    """Compare visible citation labels while tolerating publisher anchor IDs."""
    return re.sub(r"\]\(#[^)]+\)", "]", value.strip())


def citation_marker_matches_reference(marker: str, reference_text: str) -> bool:
    visible = canonical_citation_marker(marker)
    numeric = re.fullmatch(r"\[(\d+)\]", visible)
    if numeric:
        return bool(
            re.match(rf"\s*(?:{numeric.group(1)}\.|\[{numeric.group(1)}\])", reference_text)
        )
    year = re.search(r"\b(?:19|20)\d{2}[a-z]?\b", visible, re.IGNORECASE)
    surname = re.search(r"[A-Za-z'’-]{3,}", visible)
    normalized_reference = normalize_evidence(reference_text[:1000])
    return bool(
        year
        and surname
        and normalize_evidence(year.group(0)) in normalized_reference
        and normalize_evidence(surname.group(0)) in normalized_reference
    )


def source_prompt_sections(markdown: str) -> tuple[list[dict[str, str]], str]:
    """Split claim-bearing Markdown sections from the terminal references block."""
    references_match = re.search(
        r"(?im)^#{1,6}\s+(?:references|bibliography|works cited)\s*$",
        markdown,
    )
    claim_markdown = markdown[: references_match.start()] if references_match else markdown
    references = markdown[references_match.start() :] if references_match else ""
    heading_matches = list(re.finditer(r"(?m)^#{1,6}\s+.+$", claim_markdown))
    if not heading_matches:
        return ([{"heading": "Document", "text": claim_markdown.strip()}], references)
    sections: list[dict[str, str]] = []
    preamble = claim_markdown[: heading_matches[0].start()].strip()
    if preamble:
        sections.append({"heading": "Document metadata", "text": preamble})
    for index, match in enumerate(heading_matches):
        end = heading_matches[index + 1].start() if index + 1 < len(heading_matches) else len(claim_markdown)
        sections.append(
            {
                "heading": re.sub(r"^#{1,6}\s+", "", match.group(0)).strip(),
                "text": claim_markdown[match.start() : end].strip(),
            }
        )
    return sections, references


def prompt_relevant_section(heading: str) -> bool:
    """Exclude navigation, boilerplate, and low-value assay detail from drafting context."""
    normalized = normalize_evidence(heading)
    if normalized in {"document metadata", "outline", "published by"}:
        return False
    return not bool(
        re.search(
            r"(?:^|\s)(?:figures?|tables?|extras?|graphical abstract|"
            r"western blot|extraction of|hplc analysis|gene expression|"
            r"statistical analysis|credit authorship|authorship contribution|"
            r"ethical statements?|uncited references?|declaration of competing|"
            r"supplementary data|data availability|acknowledg|funding)(?:\s|$)",
            normalized,
        )
    )


def source_reference_entries(references: str) -> list[dict[str, Any]]:
    """Normalize numbered reference blocks while preserving exact source text."""
    if not references:
        return []
    starts = list(re.finditer(r"(?m)^\s*(?:\d+\.\s+|\[\d+\]:?\s+)", references))
    entries: list[dict[str, Any]] = []
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(references)
        text = references[match.start() : end].strip()
        if not text:
            continue
        first_line = text.splitlines()[0].strip()
        label_match = re.search(r"\[([^\]]+)\]\(#[^)]+\)", first_line)
        anchors = re.findall(r"#(b+\d+)", text, re.IGNORECASE)
        entries.append(
            {
                "reference_id": anchors[0].lower() if anchors else f"reference-{index + 1}",
                "marker": label_match.group(0) if label_match else first_line,
                "label": label_match.group(1) if label_match else first_line,
                "text": text,
                "urls": list(dict.fromkeys(re.findall(r"https?://[^)\s]+", text))),
                "anchors": [anchor.lower() for anchor in anchors],
            }
        )
    return entries


def prompt_source_packet(packet: dict[str, Any]) -> dict[str, Any]:
    """Build a complete but bounded source context with references retained."""
    source = {key: value for key, value in packet.items() if key != "body_markdown"}
    sections, references = source_prompt_sections(str(packet.get("body_markdown") or ""))
    relevant_sections = [
        section for section in sections if prompt_relevant_section(section["heading"])
    ]
    selected_sections: list[dict[str, str]] = []
    used = 0
    for section in relevant_sections:
        remaining = MAX_SOURCE_CLAIM_CONTEXT_CHARS - used
        if remaining <= 0:
            break
        text = section["text"][:remaining]
        if text:
            selected_sections.append({"heading": section["heading"], "text": text})
            used += len(text)
    entries = source_reference_entries(references)
    associated_ids: set[str] = set()
    for section in selected_sections:
        markers = cited_markers(section["text"])
        section_ids = [
            str(entry["reference_id"])
            for entry in entries
            if any(
                citation_marker_matches_reference(marker, str(entry["text"]))
                for marker in markers
            )
        ]
        section["associated_reference_ids"] = section_ids
        associated_ids.update(section_ids)
    selected_entries: list[dict[str, Any]] = []
    reference_used = 0
    for entry in entries:
        if entry["reference_id"] not in associated_ids:
            continue
        remaining = MAX_SOURCE_REFERENCE_CONTEXT_CHARS - reference_used
        if remaining <= 0:
            break
        if len(entry["text"]) > remaining:
            continue
        selected_entries.append(entry)
        reference_used += len(entry["text"])
    source["claim_sections"] = selected_sections
    source["reference_entries"] = selected_entries
    retained_ids = {str(entry["reference_id"]) for entry in selected_entries}
    for section in selected_sections:
        section["associated_reference_ids"] = [
            reference_id
            for reference_id in section["associated_reference_ids"]
            if reference_id in retained_ids
        ]
    source["prompt_context"] = {
        "claim_chars": used,
        "claim_section_count": len(selected_sections),
        "excluded_section_count": len(sections) - len(relevant_sections),
        "excluded_section_chars": sum(
            len(section["text"])
            for section in sections
            if section not in relevant_sections
        ),
        "reference_chars": reference_used,
        "reference_entry_count": len(selected_entries),
        "full_body_chars": len(str(packet.get("body_markdown") or "")),
    }
    return source


def candidate_document_excerpt(
    markdown: str,
    entity_terms: list[str],
    max_chars: int = MAX_CANDIDATE_CONTEXT_CHARS,
) -> str:
    """Retain complete small pages and relevant windows from unusually large pages."""
    max_chars = max(2000, min(max_chars, MAX_CANDIDATE_CONTEXT_CHARS))
    if len(markdown) <= max_chars:
        return markdown
    windows: list[tuple[int, int]] = [(0, 6000), (max(len(markdown) - 5000, 0), len(markdown))]
    normalized_terms = [term for term in entity_terms if term]
    lowered = markdown.lower()
    for term in normalized_terms:
        cursor = 0
        while len(windows) < 8:
            position = lowered.find(term.lower(), cursor)
            if position < 0:
                break
            windows.append((max(position - 1800, 0), min(position + 3200, len(markdown))))
            cursor = position + len(term)
    merged: list[tuple[int, int]] = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    chunks: list[str] = []
    used = 0
    for start, end in merged:
        chunk = markdown[start:end]
        remaining = max_chars - used
        if remaining <= 0:
            break
        chunks.append(chunk[:remaining])
        used += min(len(chunk), remaining)
    return "\n\n[...context window...]\n\n".join(chunks)


def bullet_cited_references(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Return plural provenance entries with singular-contract compatibility."""
    references = item.get("cited_references")
    if isinstance(references, list):
        return [reference for reference in references if isinstance(reference, dict)]
    reference = item.get("cited_reference")
    return [reference] if isinstance(reference, dict) else []


def phrase_in_text(phrase: str, text: str) -> bool:
    normalized_phrase = normalize_evidence(phrase)
    normalized_text = normalize_evidence(text)
    if not normalized_phrase:
        return False
    variants = {normalized_phrase}
    words = normalized_phrase.split()
    last = words[-1]
    if not last.endswith("s"):
        plural = f"{last[:-1]}ies" if last.endswith("y") and len(last) > 2 else f"{last}s"
        variants.add(" ".join([*words[:-1], plural]))
    return any(
        re.search(rf"(?:^|\s){re.escape(variant)}(?:$|\s)", normalized_text)
        for variant in variants
    )


def passage_is_mere_mention(target_entity: str, passage: str) -> bool:
    """Reject explicit list/mention language that does not state an entity fact."""
    entity = normalize_evidence(target_entity)
    normalized = normalize_evidence(passage)
    if not entity:
        return False
    return bool(
        re.search(
            rf"(?:{re.escape(entity)}\s+(?:was|were|is|are)\s+(?:only\s+)?(?:mentioned|listed|named)"
            rf"|(?:mentioned|listed|named)\s+(?:the\s+)?{re.escape(entity)})",
            normalized,
        )
    )


def target_entity_matches_candidate(
    target_entity: str,
    target_path: str,
    candidate_metadata: dict[str, dict[str, Any]],
) -> bool:
    """Require the claimed entity to match the candidate's primary identity."""
    candidate = candidate_metadata.get(target_path) or {}
    title = str(candidate.get("title") or "")
    stem = Path(target_path).stem.replace("-", " ").replace("_", " ")
    entity = normalize_evidence(target_entity)
    identities = [normalize_evidence(title), normalize_evidence(stem)]
    return bool(
        entity
        and any(
            identity
            and (phrase_in_text(entity, identity) or phrase_in_text(identity, entity))
            for identity in identities
        )
    )


def proposal_has_animal_claim(proposal: dict[str, Any], packet: dict[str, Any], claim_policy: str) -> bool:
    bullets = proposal.get("bullets") if isinstance(proposal.get("bullets"), list) else []
    if includes_background_claims(claim_policy):
        return any(
            isinstance(item, dict) and item.get("evidence_scope") == "animal"
            for item in bullets
        )
    return source_is_preclinical(packet)


def has_preclinical_heading_scope(proposal: dict[str, Any]) -> bool:
    placement = normalize_evidence(
        f"{proposal.get('parent_heading') or ''} {proposal.get('heading') or ''}"
    )
    return bool(
        re.search(
            r"\b(?:animal evidence|animal model|animal models|preclinical evidence|preclinical context)\b",
            placement,
        )
    )


def word_token_similarity(left: str, right: str) -> float:
    """Predictable near-verbatim similarity over normalized word tokens."""
    return SequenceMatcher(
        None,
        normalize_evidence(left).split(),
        normalize_evidence(right).split(),
        autojunk=False,
    ).ratio()


ANIMAL_SUBSECTION_TITLE = "Preclinical Evidence (Animal Studies)"
ANIMAL_EVIDENCE_WARNING = (
    "> **Evidence warning — animal/preclinical evidence:** These findings do not "
    "by themselves establish effects in humans."
)
SPECIES_CUE_PATTERN = (
    r"\b(?:rats?|mice|mouse|murine|rodents?|rabbits?|piglets?|zebrafish|in vivo|animals?)\b"
)
METHODS_STATEMENT_PATTERN = (
    r"\b(?:approach|method|methods|process|processing|procedure|protocol|technique)\b"
    r"[\w\s]{0,80}?\b(?:was|were)\b\s+(?:used|applied|employed|developed|performed)"
)
FORMULATION_TERM_PATTERN = r"\b(?:concentrates?|formulations?|extracts?|blends?)\b"


def packet_animal_model(packet: dict[str, Any]) -> str:
    """The packet's animal model phrase, e.g. 'fructose-fed rats'; '' when absent."""
    text = " ".join(
        str(packet.get(key) or "") for key in ("title", "abstract", "keywords")
    )
    qualified = re.search(
        r"\b([a-z][a-z-]*(?:-fed|-induced|-treated|-deficient)\s+(?:rats|mice))\b",
        text,
        re.IGNORECASE,
    )
    if qualified:
        return qualified.group(1).lower()
    species = re.search(r"\b(rats|mice|rabbits|piglets|zebrafish)\b", text, re.IGNORECASE)
    return species.group(1).lower() if species else ""


def scope_prefix(packet: dict[str, Any]) -> str:
    """The ONE whitelisted, comma-terminated leading scope prefix for this packet."""
    model = packet_animal_model(packet)
    return f"In {model}, " if model else ""


def strip_scope_prefix(text: str, packet: dict[str, Any]) -> str:
    """Remove the packet's whitelisted scope prefix once; other lead-ins remain."""
    prefix = scope_prefix(packet)
    if prefix and text.lower().startswith(prefix.lower()):
        return text[len(prefix):]
    return text


def bullet_is_animal_scope(item: dict[str, Any]) -> bool:
    return bool(
        re.search(
            r"animal|in.?vivo|preclinical",
            str(item.get("evidence_scope") or ""),
            re.IGNORECASE,
        )
    )


def bullet_has_species_cue(text: str) -> bool:
    return bool(re.search(SPECIES_CUE_PATTERN, text, re.IGNORECASE))


def is_methods_statement(text: str) -> bool:
    """Methods/process statements describe how work was done, not a health property."""
    return bool(re.search(METHODS_STATEMENT_PATTERN, normalize_evidence(text)))


def split_bullet_sentences(text: str) -> list[str]:
    """Split a multi-sentence bullet into one-idea sentences, conservatively."""
    raw = re.split(r"(?<=[.!?])\s+(?=[A-Z(])", text.strip())
    merged: list[str] = []
    for part in raw:
        if merged and re.search(r"\b(?:vs|al|e\.g|i\.e|approx|ca|cf|etc|Fig)\.$", merged[-1]):
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    parts = [part.strip() for part in merged if part.strip()]
    if len(parts) < 2 or any(len(normalize_evidence(part).split()) < 4 for part in parts):
        return [text.strip()]
    return parts


def rendered_bullet_texts(item: dict[str, Any], packet: dict[str, Any]) -> list[str]:
    """One-idea bullet texts with the standardized species-scope prefix applied."""
    sentences = split_bullet_sentences(str(item.get("text") or ""))
    if not bullet_is_animal_scope(item):
        return sentences
    prefix = scope_prefix(packet)
    rendered: list[str] = []
    for sentence in sentences:
        if bullet_has_species_cue(sentence) or not prefix:
            rendered.append(sentence)
        else:
            body = strip_scope_prefix(sentence, packet)
            if body[1:2].islower():
                body = body[:1].lower() + body[1:]
            rendered.append(prefix + body)
    return rendered


QUANTITATIVE_PATTERN = (
    r"\d[\d.,]*\s*(?:%|mg|µg|μg|g|kg|ml|mmhg|mmol|µmol|μmol|iu|kcal|fold)\b"
    r"|\d[\d.]*\s*±\s*\d|\bp\s*<\s*0\.\d|\d[\d.]*\s*(?:vs|versus)\s*[\d.]"
)


def has_quantitative_content(text: str) -> bool:
    return bool(re.search(QUANTITATIVE_PATTERN, normalize_evidence(text)))


def results_quantitative_sentences(packet: dict[str, Any], limit: int = 12) -> list[str]:
    """Exact Results-section sentences carrying quantitative outcomes."""
    sections, _references = source_prompt_sections(str(packet.get("body_markdown") or ""))
    sentences: list[str] = []
    for section in sections:
        if not re.search(r"\bresults?\b|\boutcomes?\b", normalize_evidence(section["heading"])):
            continue
        text = re.sub(r"^#{1,6}[^\n]*\n+", "", section["text"])
        for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z(])", text):
            sentence = sentence.strip()
            if (
                "\n" not in sentence
                and has_quantitative_content(sentence)
                and 60 <= len(sentence) <= 420
                and not sentence.startswith(("#", "|", "!", "["))
                and exact_source_passage(packet, sentence)
            ):
                sentences.append(sentence)
            if len(sentences) >= limit:
                return sentences
    return sentences


def packet_has_composition_data(packet: dict[str, Any]) -> bool:
    """True when the source quantifies constituent compounds (Table 1-style data)."""
    body = str(packet.get("body_markdown") or "").replace("\u202f", " ")
    for sentence in re.split(r"(?<=[.!?])\s+", body):
        if len(re.findall(r"\d[\d.,]*\s*(?:mg|µg|μg|g)\b", sentence)) >= 3:
            return True
    return False


HEALTH_CLAIM_PATTERN = (
    r"\b(?:treats?|treatment|cures?|prevents?|prevention|improves?|effective|"
    r"efficacy|therapeutic|heals?|reduces? the risk)\b"
)


def lead_is_definition_form(target_entity: str, lead_text: str) -> bool:
    """A definition lead names the entity and states what it is, not why it matters."""
    entity = re.escape(normalize_evidence(target_entity))
    if not entity:
        return False
    return bool(
        re.match(
            rf"^(?:the )?{entity}(?:es|s)?(?: fruits?| plants?| trees?| species| genus)?\s+"
            r"(?:is|are|include|includes|comprise|comprises|refers to|denotes)\b",
            normalize_evidence(lead_text),
        )
    )


def truncate_at_word(text: str, limit: int) -> str:
    """Truncate at a word boundary within limit, never mid-word."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:.-")


def bullet_subsection_title(item: dict[str, Any]) -> str:
    if bullet_is_animal_scope(item):
        return ANIMAL_SUBSECTION_TITLE
    custom = " ".join(str(item.get("subsection") or "").split()).lstrip("#").strip()
    if custom:
        return custom
    return (
        "Research Findings"
        if item.get("claim_kind") == "source_finding"
        else "Supporting Background"
    )


def grouped_rendering_enabled(plan: dict[str, Any]) -> bool:
    """Evidence-tier grouping applies to Healing Properties and scoped/animal plans."""
    if normalize_evidence(str(plan.get("heading") or "")).endswith("healing properties"):
        return True
    bullets = plan.get("bullets") if isinstance(plan.get("bullets"), list) else []
    return any(
        isinstance(item, dict)
        and (bullet_is_animal_scope(item) or str(item.get("subsection") or "").strip())
        for item in bullets
    )


def plan_target_proposals(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Return new multi-target proposals, with legacy single-target compatibility."""
    proposals = plan.get("target_proposals")
    if isinstance(proposals, list):
        return [item for item in proposals if isinstance(item, dict)]
    if plan.get("decision") in CHANGE_DECISIONS and plan.get("target_path"):
        return [plan]
    return []


def proposal_operation(plan: dict[str, Any], proposal: dict[str, Any]) -> str:
    operation = str(proposal.get("operation") or "")
    if operation:
        return operation
    decision = str(plan.get("decision") or "")
    if decision in {"append_existing", "create_new"}:
        return decision
    return "append_existing"


def safe_new_article_path(target_path: str, domain: str) -> bool:
    path = PurePosixPath(target_path)
    return bool(
        target_path
        and not path.is_absolute()
        and ".." not in path.parts
        and len(path.parts) >= 2
        and path.parts[0] == domain
        and path.suffix.lower() == ".md"
    )


def missing_entity_page_seed(
    *,
    packet: dict[str, Any],
    alert_name: str,
    domain: str,
    category_catalog: list[str],
    existing_paths: set[str],
) -> dict[str, str] | None:
    """Suggest a grounded catch-all path without deciding that it must be created."""
    primary_text = " ".join(
        str(packet.get(key) or "") for key in ("title", "abstract", "keywords")
    )
    options: list[tuple[int, int, str, str]] = []
    alert_entity = normalize_evidence(alert_name)
    for category in category_catalog:
        category_entity = normalize_evidence(PurePosixPath(category).name)
        if not category_entity or category_entity in {"natural healing", "chemicals"}:
            continue
        score = 0
        if alert_entity and (
            phrase_in_text(alert_entity, category_entity)
            or phrase_in_text(category_entity, alert_entity)
        ):
            score += 300
        if phrase_in_text(category_entity, str(packet.get("title") or "")):
            score += 200
        if phrase_in_text(category_entity, primary_text):
            score += 80
        if score:
            options.append((score, len(PurePosixPath(category).parts), category_entity, category))
    if not options:
        return None
    _score, _depth, entity, category = max(options)
    filename = re.sub(r"[^a-z0-9]+", "-", entity).strip("-") + ".md"
    target_path = str(PurePosixPath(category) / filename)
    if target_path in existing_paths or not safe_new_article_path(target_path, domain):
        return None
    return {
        "target_entity": entity,
        "suggested_path": target_path,
        "category_directory": category,
        "basis": "The category entity is asserted in the source title/abstract/keywords; critics still decide whether creation is warranted.",
    }


def validate_source_packet(packet: dict[str, Any]) -> ValidationResult:
    issues: list[str] = []
    title = str(packet.get("title") or "").strip()
    retrieval_issues = set(packet.get("retrieval_issues") or [])
    consistency_issues = set(packet.get("metadata_consistency_issues") or [])
    fatal = sorted((retrieval_issues | consistency_issues) & INVALID_PACKET_ISSUES)
    issues.extend(fatal)
    if not title or INVALID_TITLE_PATTERN.match(title):
        issues.append("invalid_title")
    abstract = str(packet.get("abstract") or "").strip()
    body = str(packet.get("body_markdown") or "").strip()
    if len(abstract) < 120 and len(body) < 800:
        issues.append("insufficient_source_evidence")
    authors = str(packet.get("authors") or "")
    if len(authors) > 1200:
        issues.append("polluted_authors")
    issues.extend(str(issue) for issue in packet.get("citation_metadata_issues") or [])
    doi = str(packet.get("doi") or "").strip()
    if doi and not DOI_PATTERN.fullmatch(doi):
        issues.append("invalid_doi")
    for key in ("authors", "journal", "pub_date"):
        if placeholder_metadata(key, str(packet.get(key) or "")):
            issues.append(f"missing_or_placeholder_{key}")
    reference_url = str(packet.get("reference_url") or "").strip()
    if not valid_http_url(reference_url):
        issues.append("invalid_reference_url")
    elif doi and reference_url.lower() != f"https://doi.org/{doi}".lower():
        issues.append("doi_reference_url_mismatch")
    if abstract.endswith(("...", "…")):
        issues.append("truncated_abstract")
    return ValidationResult(not issues, sorted(set(issues)))


def duplicate_identifiers(packet: dict[str, Any], source: str) -> list[str]:
    doi = str(packet.get("doi") or "").strip()
    values = [
        f"https://doi.org/{doi}" if DOI_PATTERN.fullmatch(doi) else "",
        doi if DOI_PATTERN.fullmatch(doi) else "",
        str(packet.get("reference_url") or "").strip(),
        str(packet.get("url") or "").strip(),
        str(packet.get("requested_url") or "").strip(),
        source.strip(),
    ]
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.lower().rstrip("/")
        if value and key not in seen:
            seen.add(key)
            unique.append(value)
    return unique


def merge_duplicate_checks(checks: list[dict[str, Any]]) -> dict[str, Any]:
    content_hits: list[dict[str, Any]] = []
    seen_hits: set[tuple[str, str, str]] = set()
    paper_result: dict[str, Any] | None = None
    active_states = {"drafted", "committed", "pr_open", "merged"}
    for check in checks:
        for hit in check.get("content_hits") or []:
            key = (
                str(hit.get("path") or ""),
                str(hit.get("ref_num") or ""),
                str(hit.get("ref_url") or ""),
            )
            if key not in seen_hits:
                seen_hits.add(key)
                content_hits.append(hit)
        candidate_paper = check.get("paper_result")
        candidate_state = str(((candidate_paper or {}).get("paper") or {}).get("workflow_state") or "")
        current_state = str(((paper_result or {}).get("paper") or {}).get("workflow_state") or "")
        if candidate_paper and (paper_result is None or candidate_state in active_states and current_state not in active_states):
            paper_result = candidate_paper
    return {
        "identifiers": [str(check.get("identifier") or "") for check in checks],
        "checks": checks,
        "content_hit_count": len(content_hits),
        "content_hits": content_hits,
        "paper_result": paper_result,
    }


def extract_json_object(value: str) -> dict[str, Any]:
    stripped = value.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Local model did not return a JSON object")
        payload = json.loads(stripped[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("Local model response must be a JSON object")
    return payload


class ModelOutputJSONError(ValueError):
    """A model response arrived, but its requested JSON object was malformed."""

    def __init__(self, message: str, raw_output: str):
        super().__init__(message)
        self.raw_output = raw_output


class LocalLLMClient:
    def __init__(self, base_url: str, model: str, timeout_seconds: int = 600):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.calls: list[dict[str, Any]] = []

    def json_completion(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 5000,
    ) -> dict[str, Any]:
        started_at = datetime.now(timezone.utc)
        started_monotonic = time.monotonic()
        call: dict[str, Any] = {
            "call": len(self.calls) + 1,
            "started_at": started_at.isoformat(),
            "model": self.model,
            "system_chars": len(system),
            "user_chars": len(user),
            "max_tokens": max_tokens,
        }
        request = Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(
                {
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0,
                    "max_tokens": max_tokens,
                    "response_format": {"type": "json_object"},
                    "chat_template_kwargs": {"enable_thinking": False},
                    "stream": False,
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
            choices = payload.get("choices") or []
            if not choices:
                raise RuntimeError("Local model returned no choices")
            message = choices[0].get("message") or {}
            content = message.get("content") or ""
            if not content:
                # Some llama.cpp reasoning templates place even a requested final
                # JSON object in reasoning_content. Use it only when content is empty.
                content = message.get("reasoning_content") or ""
            if not content:
                raise RuntimeError("Local model returned no final JSON content")
            try:
                result = extract_json_object(content)
            except ValueError as exc:
                call.update(
                    {
                        "response_chars": len(content),
                        "response_sha256": hashlib.sha256(
                            content.encode("utf-8")
                        ).hexdigest(),
                        "response_text": content,
                        "usage": payload.get("usage") or {},
                        "finish_reason": choices[0].get("finish_reason"),
                    }
                )
                raise ModelOutputJSONError(str(exc), content) from exc
            call.update(
                {
                    "status": "ok",
                    "response_chars": len(content),
                    "usage": payload.get("usage") or {},
                    "finish_reason": choices[0].get("finish_reason"),
                }
            )
            return result
        except Exception as exc:
            call.update({"status": "error", "error": str(exc)[:1000]})
            raise
        finally:
            completed_at = datetime.now(timezone.utc)
            call.update(
                {
                    "completed_at": completed_at.isoformat(),
                    "duration_seconds": round(time.monotonic() - started_monotonic, 3),
                }
            )
            self.calls.append(call)


def validate_draft_plan(
    plan: dict[str, Any],
    *,
    packet: dict[str, Any],
    candidate_paths: set[str],
    candidate_metadata: dict[str, dict[str, Any]] | None = None,
    existing_paths: set[str] | None = None,
    domain: str = "Natural Healing",
    claim_policy: str = "strict",
) -> ValidationResult:
    issues: list[str] = []
    warnings: list[str] = []
    candidate_metadata = candidate_metadata or {}
    existing_paths = existing_paths or set(candidate_paths)
    decision = str(plan.get("decision") or "")
    if decision not in ALLOWED_DECISIONS:
        issues.append("invalid_decision")
    if decision not in CHANGE_DECISIONS:
        return ValidationResult(not issues, issues, warnings)
    raw_proposals = plan.get("target_proposals")
    if isinstance(raw_proposals, list):
        for target_index, proposal in enumerate(raw_proposals):
            if not isinstance(proposal, dict):
                issues.append(f"target_{target_index}_invalid")
    proposals = plan_target_proposals(plan)
    if not 1 <= len(proposals) <= 8:
        issues.append("invalid_target_proposal_count")
        return ValidationResult(False, sorted(set(issues)), sorted(set(warnings)))
    new_contract = isinstance(plan.get("target_proposals"), list)
    if new_contract and not isinstance(plan.get("exclusions"), list):
        issues.append("invalid_exclusions")
    if includes_background_claims(claim_policy) and not study_type_matches_packet(
        str(plan.get("study_type") or ""), packet
    ):
        issues.append("study_type_mismatch")

    evidence = source_evidence(packet)
    assigned_quotes: dict[str, int] = {}
    seen_targets: set[str] = set()
    if new_contract and isinstance(plan.get("exclusions"), list):
        for exclusion_index, exclusion in enumerate(plan["exclusions"]):
            prefix = f"exclusion_{exclusion_index}"
            if not isinstance(exclusion, dict):
                issues.append(f"{prefix}_invalid")
                continue
            quote = str(exclusion.get("source_quote") or "")
            if quote and not exact_source_passage(packet, quote):
                issues.append(f"{prefix}_quote_not_in_source")
            reason = str(exclusion.get("reason") or "").strip()
            if not reason:
                issues.append(f"{prefix}_missing_reason")
            elif re.search(
                r"\b(?:not excluded|already (?:captured|included|integrated)|rather integrated)\b",
                reason,
                re.IGNORECASE,
            ):
                issues.append(f"{prefix}_contradictory_reason")
    for target_index, proposal in enumerate(proposals):
        target_prefix = f"target_{target_index}"
        target = str(proposal.get("target_path") or "")
        operation = proposal_operation(plan, proposal)
        if operation not in PROPOSAL_OPERATIONS:
            issues.append(f"{target_prefix}_invalid_operation")
        elif operation == "append_existing" and target not in candidate_paths:
            issues.append(f"{target_prefix}_not_in_candidates")
        elif operation == "create_new":
            if not safe_new_article_path(target, domain):
                issues.append(f"{target_prefix}_invalid_new_article_path")
            if target in existing_paths:
                issues.append(f"{target_prefix}_new_article_already_exists")
        # One proposal per (page, section): a plan may add several sections —
        # for example ## Composition and ## Healing Properties — to one page.
        section_key = (target, normalize_evidence(str(proposal.get("heading") or "")))
        if section_key in seen_targets:
            issues.append(f"{target_prefix}_duplicate_target")
        seen_targets.add(section_key)
        target_entity = str(proposal.get("target_entity") or "").strip()
        if new_contract and not target_entity:
            issues.append(f"{target_prefix}_missing_target_entity")
        if target_entity and not target_entity_matches_candidate(
            target_entity,
            target,
            candidate_metadata
            if operation == "append_existing"
            else {target: {"title": str(((proposal.get("new_article") or {}).get("title") or ""))}},
        ):
            issues.append(f"{target_prefix}_entity_mismatch")
        if operation == "create_new":
            new_article = proposal.get("new_article")
            if not isinstance(new_article, dict):
                issues.append(f"{target_prefix}_missing_new_article_metadata")
            else:
                title = str(new_article.get("title") or "").strip()
                tags = new_article.get("tags")
                lead_text = str(new_article.get("lead_text") or "").strip()
                lead_quote = str(new_article.get("lead_source_quote") or "").strip()
                if not title:
                    issues.append(f"{target_prefix}_missing_new_article_title")
                if not isinstance(tags, list) or not tags or any(
                    not isinstance(tag, str) or not tag.strip() for tag in tags
                ):
                    issues.append(f"{target_prefix}_invalid_new_article_tags")
                elif target_entity and not any(
                    phrase_in_text(target_entity, str(tag)) for tag in tags
                ):
                    issues.append(f"{target_prefix}_missing_new_article_entity_tag")
                if not str(new_article.get("category_rationale") or "").strip():
                    issues.append(f"{target_prefix}_missing_category_rationale")
                lead_kind = str(new_article.get("lead_kind") or "source_grounded")
                if lead_kind not in {"definition", "source_grounded"}:
                    issues.append(f"{target_prefix}_invalid_lead_kind")
                    lead_kind = "source_grounded"
                if not lead_text:
                    issues.append(f"{target_prefix}_missing_lead_text")
                elif target_entity and not phrase_in_text(target_entity, lead_text):
                    issues.append(f"{target_prefix}_lead_missing_target_entity")
                if lead_text and target_entity and not lead_is_definition_form(
                    target_entity, lead_text
                ):
                    issues.append(f"{target_prefix}_lead_not_definition_form")
                if lead_kind == "definition":
                    # An uncited general-knowledge definition: it must define,
                    # never claim; a health claim requires a cited bullet.
                    if re.search(HEALTH_CLAIM_PATTERN, normalize_evidence(lead_text)):
                        issues.append(f"{target_prefix}_definition_lead_contains_health_claim")
                    if len(split_bullet_sentences(lead_text)) > 1:
                        issues.append(f"{target_prefix}_definition_lead_not_one_sentence")
                else:
                    if len(normalize_evidence(lead_quote)) < 35 or not exact_source_passage(packet, lead_quote):
                        issues.append(f"{target_prefix}_lead_quote_not_in_source")
                    elif word_token_similarity(lead_text, lead_quote) < 0.68:
                        issues.append(f"{target_prefix}_lead_not_near_verbatim")
        heading = str(proposal.get("heading") or "")
        parent_heading = str(proposal.get("parent_heading") or "")
        if not re.match(r"^#{2,6}\s+\S", heading):
            issues.append(f"{target_prefix}_invalid_heading")
        if parent_heading and not re.match(r"^#{2,5}\s+\S", parent_heading):
            issues.append(f"{target_prefix}_invalid_parent_heading")
        if new_contract:
            if not str(proposal.get("rationale") or "").strip():
                issues.append(f"{target_prefix}_missing_rationale")
            if not isinstance(proposal.get("exclusions"), list):
                issues.append(f"{target_prefix}_invalid_exclusions")
            else:
                for exclusion_index, exclusion in enumerate(proposal["exclusions"]):
                    prefix = f"{target_prefix}_exclusion_{exclusion_index}"
                    if not isinstance(exclusion, dict):
                        issues.append(f"{prefix}_invalid")
                        continue
                    quote = str(exclusion.get("source_quote") or "")
                    if quote and not exact_source_passage(packet, quote):
                        issues.append(f"{prefix}_quote_not_in_source")
                    if not str(exclusion.get("reason") or "").strip():
                        issues.append(f"{prefix}_missing_reason")

        bullets = proposal.get("bullets")
        if not isinstance(bullets, list) or not 1 <= len(bullets) <= 8:
            issues.append(f"{target_prefix}_invalid_bullet_count")
            continue
        # The renderer now groups every animal-scope claim under a
        # "### Preclinical Evidence (Animal Studies)" subsection, which
        # satisfies the strict-policy heading requirement deterministically;
        # the warning records that the plan itself did not scope its heading.
        if proposal_has_animal_claim(proposal, packet, claim_policy) and not has_preclinical_heading_scope(proposal):
            warnings.append(f"{target_prefix}_preclinical_heading_scope_warning")
        definition = proposal.get("formulation_definition")
        if definition is not None and not isinstance(definition, dict):
            issues.append(f"{target_prefix}_invalid_formulation_definition")
            definition = None
        if isinstance(definition, dict):
            definition_text = str(definition.get("text") or "").strip()
            definition_quote = str(definition.get("source_quote") or "").strip()
            if not definition_text or "\n" in definition_text:
                issues.append(f"{target_prefix}_formulation_definition_not_one_line")
            if len(normalize_evidence(definition_quote)) < 35 or not exact_source_passage(
                packet, definition_quote
            ):
                issues.append(f"{target_prefix}_formulation_definition_quote_not_in_source")
            elif word_token_similarity(definition_text, definition_quote) < 0.68:
                issues.append(f"{target_prefix}_formulation_definition_not_near_verbatim")
        elif any(
            isinstance(item, dict)
            and bullet_is_animal_scope(item)
            and re.search(
                FORMULATION_TERM_PATTERN, normalize_evidence(str(item.get("text") or ""))
            )
            for item in bullets
        ):
            # Product-specific findings must not read as generic entity claims:
            # the animal subsection has to open by defining the formulation.
            issues.append(f"{target_prefix}_formulation_definition_missing")
        for bullet_index, item in enumerate(bullets):
            prefix = f"{target_prefix}_bullet_{bullet_index}"
            if not isinstance(item, dict):
                issues.append(f"{prefix}_invalid")
                continue
            text = str(item.get("text") or "").strip()
            quote = str(item.get("source_quote") or "").strip()
            scope = str(item.get("evidence_scope") or "")
            claim_kind = str(item.get("claim_kind") or "")
            source_section = str(item.get("source_section") or "").strip()
            if not text or "[^" in text or text.startswith("-"):
                issues.append(f"{prefix}_invalid_text")
            if len(normalize_evidence(quote)) < 35:
                issues.append(f"{prefix}_quote_too_short")
            elif not exact_source_passage(packet, quote):
                issues.append(f"{prefix}_quote_not_in_source")
            # The packet's single whitelisted species-scope prefix is excluded
            # from token comparison; the 0.68 threshold itself is unchanged and
            # applies fully to any other lead-in.
            if word_token_similarity(strip_scope_prefix(text, packet), quote) < 0.68:
                issues.append(f"{prefix}_not_near_verbatim")
            if scope not in ALLOWED_EVIDENCE_SCOPES:
                issues.append(f"{prefix}_invalid_evidence_scope")
            if is_methods_statement(strip_scope_prefix(text, packet)):
                issues.append(f"{prefix}_methods_statement_not_effect_claim")
            subsection = item.get("subsection")
            if subsection is not None and not str(subsection).strip():
                issues.append(f"{prefix}_invalid_subsection")
            if (
                includes_background_claims(claim_policy)
                and claim_kind == "background_fact"
                and not str(subsection or "").strip()
            ):
                warnings.append(f"{prefix}_missing_property_subsection")
            if target_entity and (
                not phrase_in_text(target_entity, quote)
                or not phrase_in_text(target_entity, text)
                or passage_is_mere_mention(target_entity, quote)
            ):
                issues.append(f"{prefix}_entity_not_supported_by_passage")
            # Composition/characterization measurements of the studied product
            # are not outcome claims, so the animal-scope rule does not apply.
            if (
                claim_policy == "strict"
                and source_is_preclinical(packet)
                and scope not in {"animal", "composition"}
            ):
                issues.append(f"{prefix}_preclinical_scope_must_be_animal")
            if claim_policy == "strict" and claim_kind == "background_fact":
                issues.append(f"{prefix}_background_claim_not_allowed")
            if (
                claim_policy == "strict"
                and claim_kind == "source_finding"
                and "introduction" in normalize_evidence(source_section)
            ):
                issues.append(f"{prefix}_claim_origin_misclassified")
            if includes_background_claims(claim_policy):
                if claim_kind not in CLAIM_KINDS:
                    issues.append(f"{prefix}_invalid_claim_kind")
                if not source_section_present(packet, source_section):
                    issues.append(f"{prefix}_source_section_not_found")
                if any(
                    forbidden in normalize_evidence(source_section)
                    for forbidden in ("references", "bibliography", "works cited")
                ):
                    issues.append(f"{prefix}_source_section_not_claim_bearing")
                if (
                    claim_kind == "source_finding"
                    and "introduction" in normalize_evidence(source_section)
                ):
                    issues.append(f"{prefix}_claim_origin_misclassified")
                if (
                    claim_kind == "source_finding"
                    and source_is_preclinical(packet)
                    and scope not in {"animal", "composition"}
                ):
                    issues.append(f"{prefix}_source_finding_scope_must_be_animal")
                cited_references = bullet_cited_references(item)
                raw_cited_references = item.get("cited_references")
                markers = cited_markers(quote) if claim_kind == "background_fact" else []
                if raw_cited_references is not None and not isinstance(raw_cited_references, list):
                    issues.append(f"{prefix}_invalid_cited_references")
                if isinstance(raw_cited_references, list) and any(
                    not isinstance(reference, dict) for reference in raw_cited_references
                ):
                    issues.append(f"{prefix}_invalid_cited_references")
                if claim_kind == "source_finding" and cited_references:
                    issues.append(f"{prefix}_source_finding_has_background_reference")
                if claim_kind == "background_fact" and markers and not cited_references:
                    issues.append(f"{prefix}_missing_cited_reference_provenance")
                represented_markers: set[str] = set()
                canonical_markers = {canonical_citation_marker(marker) for marker in markers}
                for cited_reference in cited_references:
                    marker = str(cited_reference.get("citation_marker") or "").strip()
                    reference_text = str(cited_reference.get("reference_text") or "").strip()
                    reference_url = str(cited_reference.get("reference_url") or "").strip()
                    if marker:
                        represented_markers.add(canonical_citation_marker(marker))
                    if markers and (
                        not marker
                        or canonical_citation_marker(marker) not in canonical_markers
                        or canonical_citation_marker(marker)
                        not in canonical_citation_marker(quote)
                    ):
                        issues.append(f"{prefix}_citation_marker_not_in_passage")
                    if not reference_text or not exact_source_passage(packet, reference_text):
                        issues.append(f"{prefix}_cited_reference_not_in_source")
                    elif marker and not citation_marker_matches_reference(marker, reference_text):
                        issues.append(f"{prefix}_cited_reference_marker_mismatch")
                    if reference_url and (
                        not valid_http_url(reference_url) or reference_url not in evidence
                    ):
                        issues.append(f"{prefix}_cited_reference_url_not_in_source")
                if markers and not canonical_markers.issubset(represented_markers):
                    issues.append(f"{prefix}_missing_cited_reference_provenance")
            normalized_quote = normalize_evidence(quote)
            if normalized_quote:
                previous_target = assigned_quotes.setdefault(normalized_quote, target_index)
                if previous_target != target_index:
                    issues.append(f"{prefix}_claim_assigned_to_multiple_targets")
        if operation == "create_new" and not any(
            isinstance(item, dict) and item.get("claim_kind") == "source_finding"
            for item in bullets
        ):
            issues.append(f"{target_prefix}_new_article_requires_source_finding")
        source_findings = [
            item
            for item in bullets
            if isinstance(item, dict) and str(item.get("claim_kind") or "source_finding") == "source_finding"
        ]
        if (
            source_findings
            and results_quantitative_sentences(packet, limit=1)
            and not any(
                has_quantitative_content(str(item.get("text") or ""))
                for item in source_findings
            )
        ):
            warnings.append(f"{target_prefix}_missing_quantitative_outcome")
    if packet_has_composition_data(packet) and not any(
        "composition" in normalize_evidence(str(proposal.get("heading") or ""))
        for proposal in proposals
    ):
        warnings.append("missing_composition_section")

    # Retain the legacy issue spellings for callers that still submit the old
    # single-target contract while reports use indexed multi-target issues.
    if not new_contract:
        legacy_names = {
            "target_0_not_in_candidates": "target_not_in_candidates",
            "target_0_invalid_heading": "invalid_heading",
            "target_0_invalid_parent_heading": "invalid_parent_heading",
            "target_0_preclinical_heading_scope_missing": "preclinical_heading_scope_missing",
            "target_0_invalid_bullet_count": "invalid_bullet_count",
        }
        issues = [
            legacy_names.get(issue, issue.removeprefix("target_0_"))
            for issue in issues
        ]
    return ValidationResult(
        not issues,
        sorted(set(issues)),
        sorted(set(warnings)),
    )


def next_footnote_number(markdown: str) -> int:
    numbers = [int(value) for value in re.findall(r"(?m)^\[\^(\d+)\]:", markdown)]
    return max(numbers, default=0) + 1


def heading_level(heading: str) -> int:
    match = re.match(r"^(#{1,6})\s", heading)
    return len(match.group(1)) if match else 0


def insert_under_heading(
    markdown: str,
    *,
    heading: str,
    parent_heading: str,
    bullet_block: str,
) -> str:
    lines = markdown.splitlines()
    exact_indexes = [index for index, line in enumerate(lines) if line.strip() == heading]
    if exact_indexes:
        start = exact_indexes[0]
        level = heading_level(heading)
        end = len(lines)
        for index in range(start + 1, len(lines)):
            candidate_level = heading_level(lines[index])
            is_reference = bool(re.match(r"^\[\^\d+\]:", lines[index]))
            if (candidate_level and candidate_level <= level) or is_reference:
                end = index
                break
        insertion = bullet_block.splitlines()
        lines[end:end] = ([""] if end and lines[end - 1].strip() else []) + insertion + [""]
        return "\n".join(lines).rstrip() + "\n"

    if not parent_heading:
        if heading_level(heading) == 2:
            # A new top-level section (e.g. ## Healing Properties after
            # ## Composition on a fresh page) appends before the footnotes.
            end = len(lines)
            for index, line in enumerate(lines):
                if re.match(r"^\[\^\d+\]:", line):
                    end = index
                    break
            block = [heading, "", *bullet_block.splitlines()]
            lines[end:end] = ([""] if end and lines[end - 1].strip() else []) + block + [""]
            return "\n".join(lines).rstrip() + "\n"
        raise ValueError(f"Heading not found and no parent supplied: {heading}")
    parent_indexes = [index for index, line in enumerate(lines) if line.strip() == parent_heading]
    if not parent_indexes:
        raise ValueError(f"Parent heading not found: {parent_heading}")
    parent_level = heading_level(parent_heading)
    if heading_level(heading) != parent_level + 1:
        raise ValueError("New heading must be exactly one level below its parent")
    start = parent_indexes[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        candidate_level = heading_level(lines[index])
        is_reference = bool(re.match(r"^\[\^\d+\]:", lines[index]))
        if (candidate_level and candidate_level <= parent_level) or is_reference:
            end = index
            break
    block = [heading, "", *bullet_block.splitlines(), ""]
    lines[end:end] = ([""] if end and lines[end - 1].strip() else []) + block
    return "\n".join(lines).rstrip() + "\n"


PUBLISHER_TITLE_SUFFIX = re.compile(
    r"\s*[-–—|:]\s*(?:sciencedirect|science ?direct|pubmed(?: central)?|pmc|elsevier|"
    r"springer(?:link)?|wiley online library|nature|mdpi|frontiers(?: media)?|"
    r"oxford academic|taylor & francis(?: online)?|sage journals|jstor|"
    r"google scholar|semantic scholar|researchgate)\s*$",
    re.IGNORECASE,
)

# Content-signal cues mapped onto the style guide's enumerated study types.
# Patterns match normalize_evidence() output (lowercase, punctuation folded to spaces).
STUDY_TYPE_SIGNALS: tuple[tuple[str, str], ...] = (
    ("Meta Analysis", r"\bmeta analys\w*"),
    ("Review", r"\b(?:systematic |scoping |narrative |umbrella )?review\b"),
    (
        "Animal Study",
        r"\b(?:in vivo|rats?|mice|mouse|murine|rodents?|rabbits?|zebrafish|porcine|"
        r"piglets?|canine|animal(?: models?| stud(?:y|ies))?s?)\b",
    ),
    (
        "Human Study",
        r"\b(?:randomi[sz]ed(?: controlled)? trial|double blind|placebo controlled|"
        r"clinical trial|patients?|participants?|volunteers?|cohort|cross sectional|"
        r"human(?: subjects?| trials?| stud(?:y|ies))?s?)\b",
    ),
    ("In Vitro", r"\b(?:in vitro|cell lines?|cell cultures?|cultured cells)\b"),
)


def classify_study_type(packet: dict[str, Any]) -> str:
    """Classify from content signals; publisher genre labels never override a hit."""
    field_texts = [
        normalize_evidence(str(packet.get(key) or ""))
        for key in ("title", "keywords", "abstract")
    ]
    hints = normalize_evidence(
        " ".join(
            str(value)
            for key in ("publication_types", "mesh_terms")
            for value in (packet.get(key) or [])
            if normalize_evidence(str(value)) not in {"article", "journal article"}
        )
    )
    for text in [*field_texts, hints]:
        if not text:
            continue
        for label, pattern in STUDY_TYPE_SIGNALS:
            if re.search(pattern, text):
                return label
    return ""


def clean_source_title(packet: dict[str, Any]) -> str:
    """Strip publisher suffixes, preferring enrichment metadata when it agrees."""
    title = " ".join(str(packet.get("title") or "").split())
    stripped = PUBLISHER_TITLE_SUFFIX.sub("", title).strip() or title
    external = packet.get("external_metadata")
    crossref = external.get("crossref") if isinstance(external, dict) else None
    crossref_title = " ".join(
        str((crossref or {}).get("title") or "").split()
    ) if isinstance(crossref, dict) else ""
    if crossref_title and normalize_evidence(crossref_title) == normalize_evidence(stripped):
        return crossref_title
    return stripped or "Untitled Source"


def normalize_citation_date(value: str) -> str:
    """Emit YYYY-MM-DD, degrading to YYYY-MM or YYYY for partial source dates."""
    text = str(value or "").strip()
    match = re.search(r"\b(\d{4})(?:[/-](\d{1,2})(?:[/-](\d{1,2}))?)?\b", text)
    if not match:
        return text
    year, month, day = match.groups()
    parts = [year]
    if month:
        parts.append(f"{int(month):02d}")
    if day:
        parts.append(f"{int(day):02d}")
    return "-".join(parts)


def source_archive_urls(packet: dict[str, Any]) -> list[str]:
    raw = packet.get("archive_urls")
    candidates = list(raw) if isinstance(raw, list) else [
        packet.get("ipfs_url"),
        packet.get("archive_url"),
    ]
    urls = [str(url).strip() for url in candidates if url]
    return list(dict.fromkeys(url for url in urls if valid_http_url(url)))


def render_reference(
    packet: dict[str, Any],
    ref_num: int,
    *,
    abstract_mode: str = "full",
) -> str:
    title = clean_source_title(packet)
    doi = str(packet.get("doi") or "").strip()
    title_url = f"https://doi.org/{doi}" if doi else str(
        packet.get("reference_url") or packet.get("url") or packet.get("requested_url") or ""
    )
    source_url = str(packet.get("url") or packet.get("requested_url") or title_url)
    journal = str(packet.get("journal") or "Source")
    pub_date = normalize_citation_date(str(packet.get("pub_date") or "Unknown"))
    study_type = classify_study_type(packet) or str(packet.get("study_type") or "Research Article")
    authors = str(packet.get("authors") or "Unknown")
    institutions = str(packet.get("institutions") or packet.get("affiliations") or "").strip()
    abstract = " ".join(str(packet.get("abstract") or "").split())
    lines = [
        f"[^{ref_num}]: **Title:** [{title}]({title_url})<br>",
        f"**Publication:** [{journal}]({source_url})<br>",
        f"**Date:** {pub_date}<br>",
        f"**Study Type:** {study_type}<br>",
        f"**Author(s):** {authors}<br>",
    ]
    if institutions:
        lines.append(f"**Institution(s):** {institutions}<br>")
    if abstract and abstract_mode != "omit":
        if abstract_mode == "truncated" and len(abstract) > 500:
            abstract = abstract[:500].rsplit(" ", 1)[0].rstrip(" ,;:.") + " […]"
        lines.append(f"**Abstract:** {abstract}<br>")
    archives = source_archive_urls(packet)
    if archives:
        labels = ["archive", "archive-mirror"]
        rendered = ", ".join(
            f"[{labels[index] if index < len(labels) else f'archive-{index + 1}'}]({url})"
            for index, url in enumerate(archives)
        )
        lines.append(f"**Copy:** {rendered}<br>")
    if doi:
        lines.append(f"**DOI:** [{doi}](https://doi.org/{doi})<br>")
    lines.append(f"**Source URL:** [{source_url}]({source_url})")
    return "\n".join(lines)


def shared_source_reference(markdown: str, packet: dict[str, Any]) -> tuple[int, bool]:
    """Return the footnote number for this source, reusing an existing block.

    Sources are keyed by DOI with a normalized-URL fallback, so a document never
    accumulates duplicate bibliographic blocks for the same paper.
    """
    doi = str(packet.get("doi") or "").strip().lower()
    url = str(packet.get("url") or packet.get("requested_url") or "").strip().lower().rstrip("/")
    for match in re.finditer(r"(?ms)^\[\^(\d+)\]:(.*?)(?=^\[\^\d+\]:|\Z)", markdown):
        block = match.group(2).lower()
        if (doi and doi in block) or (not doi and url and url in block):
            return int(match.group(1)), True
    return next_footnote_number(markdown), False


def provenance_inline(value: Any) -> str:
    return " ".join(str(value or "").split()).replace("<", "&lt;").replace(">", "&gt;")


def strip_internal_anchors(text: Any) -> str:
    """Collapse source-internal fragment links like [Doe, 2020](#bb0010) to plain text."""
    return re.sub(r"\[([^\]]*)\]\(#[^)]*\)", r"\1", str(text or ""))


def cited_reference_link(reference: dict[str, Any]) -> str:
    url = str(reference.get("reference_url") or "").strip()
    if valid_http_url(url):
        return url
    match = re.search(r"\b10\.\d{4,9}/[^\s\"<>]+", str(reference.get("reference_text") or ""))
    return f"https://doi.org/{match.group(0).rstrip('.,;)')}" if match else ""


def render_bullet_provenance(item: dict[str, Any]) -> str:
    """Emit per-claim provenance as an HTML comment inside the bullet's list item.

    Invisible on the rendered site but reviewable in the PR diff; the two-space
    indent keeps the comment attached to its bullet in CommonMark.
    """
    fields = [
        f"claim_kind: {provenance_inline(item.get('claim_kind') or 'source_finding')}",
        f"source_section: {provenance_inline(item.get('source_section'))}",
        f"source_quote: {provenance_inline(item.get('source_quote'))}",
    ]
    cited: list[str] = []
    for reference in bullet_cited_references(item):
        label = provenance_inline(strip_internal_anchors(reference.get("citation_marker")))
        text = provenance_inline(strip_internal_anchors(reference.get("reference_text")))
        link = cited_reference_link(reference)
        entry = f"{label}: {text}" if label else text
        if link:
            entry = f"{entry} ({link})"
        cited.append(entry)
    fields.append("cited_references: " + ("; ".join(cited) if cited else "none"))
    return "  <!-- provenance | " + " | ".join(fields) + " -->"


def render_critic_annotation(finding: dict[str, Any]) -> str:
    """Persist a published validated critic finding beside its bullet.

    Findings that only live in the PR body disappear once the PR merges; this
    comment keeps the caveat with the content it qualifies.
    """
    fields = [
        f"severity: {provenance_inline(finding.get('severity') or 'review')}",
        f"code: {provenance_inline(finding.get('code') or 'unknown_issue')}",
        f"explanation: {provenance_inline(finding.get('explanation'))}",
    ]
    quote = provenance_inline(finding.get("source_quote"))
    if quote:
        fields.append(f"source_quote: {quote}")
    return "  <!-- critic | " + " | ".join(fields) + " -->"


def indexed_critic_findings(
    critic_findings: list[dict[str, Any]] | None,
    bullet_count: int,
) -> tuple[dict[int, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Split published findings into bullet-adjacent and target-level groups."""
    by_bullet: dict[int, list[dict[str, Any]]] = {}
    target_level: list[dict[str, Any]] = []
    for finding in critic_findings or []:
        if not isinstance(finding, dict) or finding.get("severity") not in {"review", "blocking"}:
            continue
        bullet_index = finding.get("bullet_index")
        if (
            isinstance(bullet_index, int)
            and not isinstance(bullet_index, bool)
            and 0 <= bullet_index < bullet_count
        ):
            by_bullet.setdefault(bullet_index, []).append(finding)
        else:
            target_level.append(finding)
    return by_bullet, target_level


def compendium_evidence_warning(plan: dict[str, Any], packet: dict[str, Any], claim_policy: str) -> str:
    if (
        includes_background_claims(claim_policy)
        and proposal_has_animal_claim(plan, packet, claim_policy)
        and not has_preclinical_heading_scope(plan)
    ):
        return ANIMAL_EVIDENCE_WARNING
    return ""


def apply_draft_plan(
    markdown: str,
    plan: dict[str, Any],
    packet: dict[str, Any],
    *,
    claim_policy: str = "strict",
    abstract_mode: str = "full",
    critic_findings: list[dict[str, Any]] | None = None,
) -> str:
    ref_num, already_defined = shared_source_reference(markdown, packet)
    by_bullet, target_level = indexed_critic_findings(critic_findings, len(plan["bullets"]))

    def item_lines(index: int, item: dict[str, Any]) -> list[str]:
        lines: list[str] = []
        for text in rendered_bullet_texts(item, packet):
            lines.append(f"- {text}[^{ref_num}]")
            lines.append(render_bullet_provenance(item))
        for finding in by_bullet.get(index, []):
            lines.append(render_critic_annotation(finding))
        return lines

    if grouped_rendering_enabled(plan):
        grouped: dict[str, list[int]] = {}
        order: list[str] = []
        for index, item in enumerate(plan["bullets"]):
            title = bullet_subsection_title(item)
            if title not in grouped:
                grouped[title] = []
                order.append(title)
            grouped[title].append(index)
        if ANIMAL_SUBSECTION_TITLE in order:
            order.remove(ANIMAL_SUBSECTION_TITLE)
            order.insert(0, ANIMAL_SUBSECTION_TITLE)
        definition = (
            plan.get("formulation_definition")
            if isinstance(plan.get("formulation_definition"), dict)
            else None
        )
        block_lines: list[str] = []
        for title in order:
            block_lines.extend([f"### {title}", ""])
            animal_group = any(
                bullet_is_animal_scope(plan["bullets"][index]) for index in grouped[title]
            )
            if animal_group and definition is not None:
                block_lines.append(
                    f"{str(definition.get('text') or '').strip()}[^{ref_num}]"
                )
                block_lines.append(
                    render_bullet_provenance(
                        {
                            "claim_kind": "formulation_definition",
                            "source_section": definition.get("source_section"),
                            "source_quote": definition.get("source_quote"),
                        }
                    ).lstrip()
                )
                block_lines.append("")
                definition = None
            if animal_group:
                block_lines.extend([ANIMAL_EVIDENCE_WARNING, ""])
            for index in grouped[title]:
                block_lines.extend(item_lines(index, plan["bullets"][index]))
            block_lines.append("")
        while block_lines and not block_lines[-1]:
            block_lines.pop()
        for finding in target_level:
            block_lines.append(render_critic_annotation(finding).lstrip())
        bullets = "\n".join(block_lines)
    else:
        bullet_lines: list[str] = []
        for index, item in enumerate(plan["bullets"]):
            bullet_lines.extend(item_lines(index, item))
        for finding in target_level:
            bullet_lines.append(render_critic_annotation(finding).lstrip())
        warning = compendium_evidence_warning(plan, packet, claim_policy)
        bullets = "\n".join(([warning, ""] if warning else []) + bullet_lines)
    updated = insert_under_heading(
        markdown,
        heading=str(plan["heading"]).strip(),
        parent_heading=str(plan.get("parent_heading") or "").strip(),
        bullet_block=bullets,
    )
    if already_defined:
        return updated
    reference = render_reference(packet, ref_num, abstract_mode=abstract_mode)
    return updated.rstrip() + "\n\n" + reference + "\n"


def initial_new_article_markdown(proposal: dict[str, Any]) -> str:
    """Render a minimal source-grounded article before applying its first section."""
    metadata = proposal.get("new_article") if isinstance(proposal.get("new_article"), dict) else {}
    title = str(metadata.get("title") or proposal.get("target_entity") or "Untitled").strip()
    tags = [str(tag).strip() for tag in metadata.get("tags") or [] if str(tag).strip()]
    entity = str(proposal.get("target_entity") or title).strip()
    lead = str(metadata.get("lead_text") or "").strip()
    emphasized = re.sub(
        re.escape(entity),
        lambda match: f"**{match.group(0)}**",
        lead,
        count=1,
        flags=re.IGNORECASE,
    )
    frontmatter = yaml.safe_dump(
        {"title": title, "tags": tags},
        sort_keys=False,
        allow_unicode=False,
        default_flow_style=False,
    ).strip()
    heading = str(proposal.get("heading") or "## Research").strip()
    # A general-knowledge definition lead is intentionally uncited; only a
    # source-grounded lead carries the paper's footnote.
    lead_marker = "" if str(metadata.get("lead_kind") or "") == "definition" else "[^1]"
    return f"---\n{frontmatter}\n---\n\n{emphasized}{lead_marker}\n\n{heading}\n"


def frontmatter_block(markdown: str) -> str:
    if not markdown.startswith("---\n"):
        return ""
    end = markdown.find("\n---\n", 4)
    return markdown[: end + 5] if end >= 0 else ""


def strip_html_comments(markdown: str) -> str:
    return re.sub(r"(?s)<!--.*?-->", "", markdown)


def page_anchor_ids(markdown: str) -> set[str]:
    """Collect valid in-page link targets: explicit ids plus heading slugs."""
    visible = strip_html_comments(markdown)
    ids = set(re.findall(r"(?:\bid|\bname)=\"([^\"]+)\"", visible))
    for heading in re.findall(r"(?m)^#{1,6}\s+(.+)$", visible):
        slug = re.sub(r"[^\w\s-]", "", heading.strip().lower())
        ids.add(re.sub(r"\s+", "-", slug).strip("-"))
    return ids


def dead_anchor_links(markdown: str) -> list[str]:
    """Fragment links with no matching in-page anchor (footnotes are separate syntax)."""
    visible = strip_html_comments(markdown)
    anchors = re.findall(r"\]\(#([^)]+)\)", visible)
    valid = page_anchor_ids(markdown)
    return sorted({anchor for anchor in anchors if anchor not in valid})


def heading_section_lines(markdown: str, heading: str) -> list[str]:
    """The lines belonging to a heading, up to the next same-level heading."""
    lines = markdown.splitlines()
    start = next(
        (index for index, line in enumerate(lines) if line.strip() == heading.strip()),
        None,
    )
    if start is None:
        return []
    level = heading_level(heading)
    end = len(lines)
    for index in range(start + 1, len(lines)):
        candidate_level = heading_level(lines[index])
        if (candidate_level and candidate_level <= level) or re.match(
            r"^\[\^\d+\]:", lines[index]
        ):
            end = index
            break
    return lines[start + 1 : end]


def rendered_structure_issues(
    section_lines: list[str],
    packet: dict[str, Any],
    plan: dict[str, Any],
) -> list[str]:
    """Structural rules for evidence-tier subsections under the target heading."""
    issues: set[str] = set()
    prefix = scope_prefix(packet)
    current: str | None = None
    warned: dict[str, bool] = {}
    section_bullets: dict[str, list[str]] = {}
    for line in section_lines:
        stripped = line.strip()
        if line.startswith("### "):
            current = line[4:].strip()
            warned.setdefault(current, False)
            section_bullets.setdefault(current, [])
        elif line.startswith("- "):
            if current is None:
                issues.add("bullet_outside_property_subsection")
            else:
                section_bullets[current].append(line[2:])
            text = re.sub(r"\[\^\d+\]$", "", line[2:]).strip()
            if len(split_bullet_sentences(strip_scope_prefix(text, packet))) > 1:
                issues.add("bullet_not_single_idea")
        elif stripped.startswith("> **Evidence warning"):
            if current is None:
                issues.add("animal_warning_outside_subsection")
            else:
                warned[current] = True
    for title, bullets in section_bullets.items():
        animal_named = bool(re.search(r"preclinical|animal", title, re.IGNORECASE))
        if animal_named:
            if not warned.get(title):
                issues.add("animal_subsection_missing_warning")
            for bullet in bullets:
                if not (
                    bullet_has_species_cue(bullet)
                    or (prefix and bullet.lower().startswith(prefix.lower()))
                ):
                    issues.add("animal_bullet_missing_species_scope")
        elif warned.get(title) and not any(
            bullet_has_species_cue(bullet) for bullet in bullets
        ):
            issues.add("animal_warning_misapplied_to_background")
    definition = (
        plan.get("formulation_definition")
        if isinstance(plan.get("formulation_definition"), dict)
        else None
    )
    if definition is not None:
        definition_text = str(definition.get("text") or "").strip()
        definition_index = next(
            (
                index
                for index, line in enumerate(section_lines)
                if definition_text and definition_text in line and "<!--" not in line
            ),
            None,
        )
        first_animal_bullet = None
        current = None
        for index, line in enumerate(section_lines):
            if line.startswith("### "):
                current = line[4:].strip()
            elif line.startswith("- ") and current and re.search(
                r"preclinical|animal", current, re.IGNORECASE
            ):
                first_animal_bullet = index
                break
        if definition_index is None or (
            first_animal_bullet is not None and definition_index > first_animal_bullet
        ):
            issues.add("formulation_definition_missing_or_misplaced")
    return sorted(issues)


def validate_rendered_markdown(
    original: str,
    updated: str,
    *,
    plan: dict[str, Any],
    packet: dict[str, Any],
    claim_policy: str = "strict",
    critic_findings: list[dict[str, Any]] | None = None,
) -> ValidationResult:
    issues: list[str] = []
    if frontmatter_block(original) != frontmatter_block(updated):
        issues.append("frontmatter_changed")
    ref_num, _ = shared_source_reference(original, packet)
    if f"[^{ref_num}]:" not in updated:
        issues.append(f"reference_{ref_num}_missing")
    doi = str(packet.get("doi") or "").strip()
    if doi:
        doi_blocks = sum(
            1
            for match in re.finditer(r"(?ms)^\[\^\d+\]:(.*?)(?=^\[\^\d+\]:|\Z)", updated)
            if doi.lower() in match.group(1).lower()
        )
        if doi_blocks > 1:
            issues.append("duplicate_source_footnote")
    source_url = str(packet.get("url") or packet.get("requested_url") or "").strip()
    if doi and doi not in updated:
        issues.append("doi_missing_from_reference")
    if source_url and source_url not in updated:
        issues.append("source_url_missing_from_reference")
    for index, item in enumerate(plan.get("bullets") or []):
        for text in rendered_bullet_texts(item, packet):
            if f"- {text}[^{ref_num}]" not in updated:
                issues.append(f"bullet_{index}_citation_missing")
        if render_bullet_provenance(item) not in updated:
            issues.append(f"bullet_{index}_provenance_missing")
    if grouped_rendering_enabled(plan):
        issues.extend(
            rendered_structure_issues(
                heading_section_lines(updated, str(plan.get("heading") or "")),
                packet,
                plan,
            )
        )
    by_bullet, target_level = indexed_critic_findings(
        critic_findings, len(plan.get("bullets") or [])
    )
    for bullet_index, findings in by_bullet.items():
        for finding in findings:
            if render_critic_annotation(finding) not in updated:
                issues.append(f"bullet_{bullet_index}_critic_annotation_missing")
    for finding in target_level:
        if render_critic_annotation(finding).lstrip() not in updated:
            issues.append("target_critic_annotation_missing")
    for anchor in dead_anchor_links(updated):
        issues.append(f"dead_anchor_link_{anchor}")
    warning = compendium_evidence_warning(plan, packet, claim_policy)
    if warning and warning not in updated:
        issues.append("preclinical_evidence_warning_missing")
    added_lines = max(0, len(updated.splitlines()) - len(original.splitlines()))
    if added_lines > 160:
        issues.append("change_scope_too_large")
    return ValidationResult(not issues, sorted(set(issues)))


def run_command(args: list[str], *, cwd: Path) -> str:
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "command failed")
    return result.stdout.strip()


def git_revision(repo: Path, ref: str = "HEAD") -> str:
    """Return a revision for run provenance without making reporting brittle."""
    try:
        return run_command(["git", "rev-parse", ref], cwd=repo)
    except (OSError, RuntimeError):
        return "unknown"


def git_worktree_provenance(repo: Path) -> dict[str, Any]:
    """Describe uncommitted tool code without embedding a potentially huge diff."""
    try:
        status = run_command(["git", "status", "--porcelain"], cwd=repo)
        diff = subprocess.run(
            ["git", "diff", "--binary", "HEAD"],
            cwd=repo,
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, RuntimeError, subprocess.CalledProcessError):
        return {"dirty": None, "status": [], "diff_sha256": "unknown"}
    return {
        "dirty": bool(status),
        "status": status.splitlines(),
        "diff_sha256": hashlib.sha256(diff).hexdigest(),
    }


def materialize_git_ref(*, content_repo: Path, git_ref: str) -> tempfile.TemporaryDirectory[str]:
    result = subprocess.run(
        ["git", "archive", "--format=tar", git_ref],
        cwd=content_repo,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip())
    temporary = tempfile.TemporaryDirectory(prefix="research-content-base-")
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
        destination_root = Path(temporary.name).resolve()
        for member in archive.getmembers():
            destination = (destination_root / member.name).resolve()
            if destination != destination_root and destination_root not in destination.parents:
                raise ValueError(f"Unsafe path in git archive: {member.name}")
            if member.issym() or member.islnk():
                raise ValueError(f"Symlinks are not allowed in the content snapshot: {member.name}")
        archive.extractall(temporary.name, filter="data")
    return temporary


def create_isolated_worktree(
    *,
    content_repo: Path,
    runtime_root: Path,
    slug: str,
    base_ref: str,
) -> tuple[Path, str]:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    branch = f"local-research/{timestamp}-{slug[:45]}"
    worktree = runtime_root / "worktrees" / f"{timestamp}-{slug[:45]}"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if worktree.exists():
        raise FileExistsError(f"Worktree already exists: {worktree}")
    run_command(
        ["git", "worktree", "add", "-b", branch, str(worktree), base_ref],
        cwd=content_repo,
    )
    return worktree, branch


def style_context(tools_root: Path) -> str:
    general = (tools_root / "docs" / "research-publishing-style-guide.md").read_text(
        encoding="utf-8"
    )
    natural = (
        tools_root.parent / "docs" / "natural-healing-content-style-guide.md"
    ).read_text(encoding="utf-8")
    return f"GENERAL GUIDE:\n{general}\n\nNATURAL HEALING GUIDE:\n{natural}"


def draft_prompt(
    *,
    packet: dict[str, Any],
    candidates: list[dict[str, Any]],
    candidate_documents: dict[str, str],
    prior_issues: list[str] | None = None,
    previous_plan: dict[str, Any] | None = None,
    previous_critic: dict[str, Any] | None = None,
    claim_policy: str = "integrated",
    domain: str = "Natural Healing",
    category_catalog: list[str] | None = None,
    new_article_seed: dict[str, str] | None = None,
) -> str:
    contract = {
        "decision": "publish_changes | duplicate | needs_review",
        "study_type": "most specific evidence design supported by the source; a generic publisher label may be refined from explicit rat/mouse/human text",
        "target_proposals": [
            {
                "operation": "append_existing | create_new",
                "target_path": "candidate path for append_existing; safe new .md path under DOMAIN for create_new",
                "target_entity": "exact entity supported by every claim and matching the candidate title/path",
                "new_article": {
                    "title": "required only for create_new",
                    "tags": ["focused domain tags"],
                    "lead_kind": "definition | source_grounded",
                    "lead_text": "definition-form lead: the entity name followed by what it IS (genus, family, class of products); with lead_kind definition it is uncited general knowledge and must contain no health claim",
                    "lead_source_quote": "exact contiguous source passage; required only when lead_kind is source_grounded",
                    "category_rationale": "why this existing or new sub-category is appropriate",
                },
                "parent_heading": "exact existing ##-##### heading when heading is new, else empty",
                "heading": "exact existing heading, or a new child heading",
                "rationale": "why this entity and scope belong on this target",
                "formulation_definition": {
                    "text": "required when animal findings concern a specific formulation: one near-verbatim line defining that formulation; omit the whole object otherwise",
                    "source_quote": "exact contiguous source passage supporting the definition",
                    "source_section": "exact source section heading",
                },
                "bullets": [
                    {
                        "text": "ONE-idea near-verbatim source-supported claim, without citation marker",
                        "source_quote": "an exact contiguous quote from abstract or extracted content",
                        "source_section": "exact source section heading, such as Abstract, Introduction, Results, or Discussion",
                        "claim_kind": "source_finding | background_fact",
                        "evidence_scope": "review_summary | human | animal | in_vitro | mechanistic | composition",
                        "subsection": "for background_fact: short property name for its ### subsection, such as Nutrient Composition; empty for animal findings",
                        "cited_references": [
                            {
                                "citation_marker": "exact marker in source_quote, such as [12]",
                                "reference_text": "exact cited reference entry from SOURCE_PACKET",
                                "reference_url": "exact URL from SOURCE_PACKET, or empty",
                            }
                        ],
                    }
                ],
                "exclusions": [
                    {
                        "source_quote": "exact source text intentionally excluded from this target",
                        "reason": "entity, scope, limitation, corruption, or duplication reason",
                    }
                ],
            }
        ],
        "exclusions": [
            {
                "source_quote": "exact source text excluded from every target, or empty when none",
                "reason": "why it is not proposed anywhere",
            }
        ],
        "review_notes": [],
    }
    source = prompt_source_packet(packet)
    repair_hints: list[str] = []
    issues = prior_issues or []
    if any("invalid_text" in issue for issue in issues):
        repair_hints.append("For bullet text, remove every leading dash and every [^n] citation marker; the renderer adds both.")
    if any("preclinical_heading_scope_missing" in issue for issue in issues):
        repair_hints.append("Put 'Animal Evidence', 'Animal Models', or 'Preclinical Evidence' literally in the proposed heading.")
    if any("methods_statement_not_effect_claim" in issue for issue in issues):
        repair_hints.append(
            "Remove methods/process bullets describing how the formulation was produced or "
            "assessed; keep only effect or property claims, and put essential formulation "
            "detail in formulation_definition."
        )
    if any("formulation_definition" in issue for issue in issues):
        repair_hints.append(
            "Provide formulation_definition with one near-verbatim line defining the studied "
            "formulation and its exact contiguous source_quote."
        )
    if any("quote_not_in_source" in issue for issue in issues):
        repair_hints.append("Copy source_quote as one exact contiguous span from SOURCE_PACKET without ellipses or repairs.")
    if any("not_near_verbatim" in issue for issue in issues):
        repair_hints.append("Make bullet text equal source_quote except for removing a section/list label.")
    if any("lead_not_near_verbatim" in issue for issue in issues):
        repair_hints.append(
            "Set new_article.lead_text equal to new_article.lead_source_quote except for harmless Markdown removal. "
            "Do not turn an intervention-specific passage into a general entity definition or add health claims."
        )
    if any("missing_new_article_entity_tag" in issue for issue in issues):
        repair_hints.append(
            "Include the exact target entity or new-article title as one focused tag."
        )
    if any("contradictory_reason" in issue for issue in issues):
        repair_hints.append(
            "A global exclusion must be genuinely omitted everywhere. Remove entries described as already captured, included, integrated, or not excluded."
        )
    if any("provenance" in issue or "cited_reference" in issue for issue in issues):
        repair_hints.append(
            "For a background passage with a citation marker, copy that marker and its full reference entry exactly from SOURCE_PACKET."
        )
    if any("citation_marker_not_in_passage" in issue for issue in issues):
        repair_hints.append(
            "Copy the visible author-year or numeric citation marker from source_quote; publisher Markdown anchor IDs may be omitted."
        )
    if any("cited_reference_url_not_in_source" in issue for issue in issues):
        repair_hints.append(
            "Set reference_url to an empty string unless that exact URL is visibly present in the supplied reference_text; never infer a DOI URL."
        )
    if any("cited_reference_not_in_source" in issue for issue in issues):
        repair_hints.append(
            "Remove a background claim when its exact cited reference entry is absent; do not write placeholders such as 'Not provided'."
        )
    if any("study_type_mismatch" in issue for issue in issues):
        repair_hints.append(
            "Use the most specific source-supported design: explicit rats/mice may be labeled Animal Study even when publisher metadata only says Research Article."
        )
    if any("entity" in issue for issue in issues):
        repair_hints.append(
            "Use a target only when its primary entity is asserted by every exact source_quote; a mention is insufficient. "
            "When a critic rejected a target's entity scope, retarget those claims to a more precise page or move them to exclusions instead of abandoning the whole plan."
        )
    if any("missing_quantitative_outcome" in issue for issue in issues):
        repair_hints.append(
            "Add at least one bullet quoting a QUANTITATIVE_RESULTS_CANDIDATES sentence exactly."
        )
    if any("missing_composition_section" in issue for issue in issues):
        repair_hints.append(
            "Add a target proposal with heading ## Composition holding the source's constituent-compound measurements as evidence_scope composition bullets."
        )
    if any("lead_not_definition_form" in issue for issue in issues):
        repair_hints.append(
            "Rewrite lead_text as a definition: the entity name followed by what it is; move the paper's framing sentence into a cited bullet or drop it."
        )
    if any("planner_abandoned_valid_plan" in issue for issue in issues):
        repair_hints.append(
            "The previous response returned needs_review even though an earlier attempt produced a deterministically valid plan. "
            "Return a corrected publish plan: keep the targets that drew no critic objection, and rescope, retarget, or exclude only the objected claims."
        )
    if claim_policy == "strict":
        policy_rules = (
            "STRICT CLAIM POLICY: Propose only direct findings of the supplied paper. "
            "Use claim_kind source_finding and do not mine introductions or discussions for background facts. "
            "Every preclinical claim must use evidence_scope animal and the heading context must explicitly "
            "contain Animal Evidence, Animal Models, or Preclinical Evidence."
        )
    else:
        policy_rules = (
            "INTEGRATED / LEGACY COMPENDIUM CLAIM POLICY: Propose both direct source_finding claims and useful "
            "Distinguish the two claim kinds explicitly. Every claim needs an exact source_quote and source_section. "
            "When a background passage cites earlier work, preserve its exact citation marker, exact reference-list "
            "entry, and source-provided URL in cited_references; missing provenance requires needs_review. Background "
            "evidence_scope describes the earlier evidence summarized by the passage, not the supplied paper's study "
            "design. Animal-scoped claims should use an explicit preclinical heading when natural; otherwise the "
            "renderer adds a mandatory animal/preclinical evidence warning."
        )
    sections = [
            (
                "REPAIR DIRECTIVE — THE PREVIOUS PLAN FAILED DETERMINISTIC VALIDATION. Return a materially corrected plan, not the same JSON. Fix or remove every target/bullet named below before doing anything else.\n"
                + "ISSUES:\n"
                + json.dumps(issues, indent=2)
                + "\nREPAIR_HINTS:\n"
                + json.dumps(repair_hints, indent=2)
            )
            if issues
            else "FIRST PASS: build the smallest safe plan that covers the primary direct findings and useful provenance-complete background facts.",
            "Return only one JSON object matching this contract:\n" + json.dumps(contract, indent=2),
            f"DOMAIN: {domain}",
            "AVAILABLE_CATEGORY_DIRECTORIES:\n" + json.dumps(category_catalog or [], indent=2),
            "MISSING_ENTITY_PAGE_SEED (suggestion, not an automatic decision):\n"
            + json.dumps(new_article_seed or {}, indent=2),
            f"CLAIM_POLICY: {claim_policy}\n{policy_rules}",
            "Rules: For Natural Healing use concise one-idea bullets as close to the source wording as possible. Each bullet states exactly one idea; give consecutive sentences their own bullets. A methods or process statement describing how a formulation was produced, prepared, or assessed is never a Healing Properties effect bullet; carry essential formulation detail in formulation_definition instead, which is required whenever animal findings concern a specific formulation rather than the page entity generally. Give every background_fact bullet a short property-named subsection such as Nutrient Composition; animal findings are grouped under a Preclinical Evidence (Animal Studies) subsection automatically and need no subsection value. The bullet text should normally equal source_quote exactly, except that a list number, section label, or citation marker may be removed. Do not add footnote markers; the renderer adds them. Every source_quote must be copied exactly from the normalized source sections. Preserve study limitations and never turn a review, animal, rat, mouse, in-vitro, or mechanistic statement into a human treatment claim. Omit internally contradictory, corrupted, dangerously mistyped, or statistically misleading source sentences. One plan may append to several compatible pages and create one or more missing entity pages. Use operation append_existing only for a supplied candidate. Use create_new when the source's primary entity has no suitable page, including a useful category-level catch-all such as DOMAIN/Fruits/Citrus/citrus.md; choose an existing category directory when appropriate and create a new sub-category only when its semantic scope is clear. When MISSING_ENTITY_PAGE_SEED is nonempty and direct source findings explicitly concern that entity, do not discard those primary findings in favor of component-only background updates: create the seeded catch-all page and keep formulation/cultivar scope explicit in each bullet. Every target_entity must match the target title/stem and be asserted by every quote assigned to that target; a mere mention is insufficient. Never reuse the same claim across targets. Do not place cultivar, blend, or isolated-compound findings on a broader page unless the exact passage supports the broader entity. A citrus blend never belongs on a Bergamot page. When the source body is available, prefer or supplement abstract claims with Results-section sentences carrying concrete quantitative outcomes (values, units, comparisons): include at least one quantitative Results bullet per supplied-paper target whenever QUANTITATIVE_RESULTS_CANDIDATES is nonempty; every quantitative bullet still needs its exact contiguous source_quote. When the source quantifies the entity's constituent compounds, also propose a '## Composition' section for the entity page as its own target proposal (same target_path, heading ## Composition, evidence_scope composition, repeated new_article metadata for a new page); composition bullets are measurements, not effect claims. A new page must include at least one direct source_finding, focused tags including the exact target entity/title, a category rationale, and a safe path under DOMAIN. Its lead must be definition-form: the entity name followed by what it is (genus, family, or class of products), like '**Citrus** is a genus of flowering trees and shrubs in the family Rutaceae whose fruits include oranges, clementines, and grapefruits.' Prefer lead_kind definition (uncited general knowledge, no health claims); use lead_kind source_grounded only when the source itself contains a definition-form sentence. A topic-relevance framing sentence from the paper is never a lead; if kept it becomes a cited bullet. For cited_references, never invent a DOI or URL and never use placeholder reference text; remove the background claim if its exact reference record is unavailable. Explicitly list material unused claims in exclusions. A critic objection to one target is a placement problem, not a stop signal: rescope or retarget the affected claims and keep the remaining safe targets. Return needs_review only when neither guarded existing-page updates nor a well-scoped new page can be proposed safely.",
            "QUANTITATIVE_RESULTS_CANDIDATES (exact Results sentences with concrete outcomes; prefer these for supplied-paper bullets):\n"
            + json.dumps(results_quantitative_sentences(packet), indent=2),
            "SOURCE_PACKET_WITH_NORMALIZED_SECTIONS_AND_REFERENCES:\n" + json.dumps(source, indent=2),
            "MATCH_CANDIDATES:\n" + json.dumps(candidates, indent=2),
            "CANDIDATE_DOCUMENTS:\n" + json.dumps(candidate_documents, indent=2),
            "PREVIOUS_PLAN_TO_REVISE:\n" + json.dumps(previous_plan or {}, indent=2),
            "PREVIOUS_CRITIC_FEEDBACK:\n" + json.dumps(previous_critic or {}, indent=2),
        ]
    return "\n\n".join(sections)


def critic_issue_contract(codes: set[str]) -> dict[str, Any]:
    return {
        "issues": [
            {
                "code": " | ".join(sorted(codes)),
                "severity": "warning | review | blocking",
                "bullet_index": "integer or null",
                "explanation": "concise objection; no external facts",
                "source_quote": "exact contiguous quote from SOURCE_PACKET, or empty",
                "target_quote": "exact contiguous quote from SELECTED_TARGET_MARKDOWN, or empty",
            }
        ]
    }


def placement_critic_prompt(
    *,
    packet: dict[str, Any],
    plan: dict[str, Any],
    selected_candidate: dict[str, Any],
    selected_target_markdown: str,
    deterministic_issues: list[str],
    claim_policy: str = "integrated",
    new_article_seed: dict[str, str] | None = None,
) -> str:
    source = prompt_source_packet(packet)
    return "\n\n".join(
        [
            "Review only target-page and heading placement. Return one JSON object matching this contract:\n"
            + json.dumps(critic_issue_contract(PLACEMENT_ISSUE_CODES), indent=2),
            f"CLAIM_POLICY: {claim_policy}",
            "MISSING_ENTITY_PAGE_SEED (the planner was invited to create this category catch-all page):\n"
            + json.dumps(new_article_seed or {}, indent=2),
            "Use only the supplied source, selected candidate metadata, proposed new-article metadata, and selected target Markdown. Do not use outside botanical or medical knowledge. Each bullet's exact source_quote must assert the target_entity; an entity merely mentioned without support for the claim is entity_not_supported. A target_entity must match the target title/path identity. A citrus blend must never be placed on a Bergamot page. For append_existing, check the page and heading; wrong_target_page must quote both source and target. For create_new, check whether a distinct page is warranted and whether its domain/category/path scope is appropriate; new_article_not_warranted or wrong_category requires an exact source quote but no target quote because the page does not exist. Check whether named-cultivar or isolated-compound claims are being put onto a broader target without direct passage support. A category catch-all page, especially the seeded one, exists precisely to host findings about blends, concentrates, extracts, juices, and other products derived from that category: when a bullet keeps its formulation, cultivar, or processing scope explicit in near-verbatim text, the derived-product-versus-whole-category distinction is at most a warning, not entity_not_supported. Reserve entity_not_supported review or blocking for a quote about a different entity or one that does not involve the target category at all. Use warning for useful non-gating notes, review for human placement judgment, and blocking for clearly unsafe placement. Return an empty issues list when there is no grounded objection. Deterministic checks are authoritative for path safety, candidate membership, exact source containment, near-verbatim similarity, and provenance. In integrated policy, an ordinary heading is acceptable for animal evidence only because the renderer adds a mandatory warning.",
            "SOURCE_PACKET_WITH_NORMALIZED_SECTIONS_AND_REFERENCES:\n" + json.dumps(source, indent=2),
            "DRAFT_PLAN:\n" + json.dumps(plan, indent=2),
            "SELECTED_CANDIDATE_METADATA:\n" + json.dumps(selected_candidate, indent=2),
            "SELECTED_TARGET_MARKDOWN:\n" + selected_target_markdown,
            "DETERMINISTIC_ISSUES:\n" + json.dumps(deterministic_issues),
        ]
    )


def evidence_critic_prompt(
    *,
    packet: dict[str, Any],
    plan: dict[str, Any],
    selected_candidate: dict[str, Any],
    selected_target_markdown: str,
    deterministic_issues: list[str],
    claim_policy: str = "integrated",
) -> str:
    source = prompt_source_packet(packet)
    return "\n\n".join(
        [
            "Review only evidence support, claim strength, study scope, one-idea bullets, source integrity, and material limitations. Return one JSON object matching this contract:\n"
            + json.dumps(critic_issue_contract(EVIDENCE_ISSUE_CODES), indent=2),
            f"CLAIM_POLICY: {claim_policy}",
            "Every issue must cite an exact contiguous source_quote from SOURCE_PACKET; target_quote may additionally quote the selected page when surrounding context creates an overclaim. Verify that source_finding means a direct finding of the supplied paper and background_fact means a statement the paper summarizes. For background_fact, require its source section and preserve every earlier citation marker plus the exact supplied reference entry; missing or invented provenance is missing_source_provenance. Check the evidence_scope against the evidence described by that individual passage. Do not object merely because a near-verbatim bullet does not repeat context already made explicit by its preclinical heading, evidence warning, or evidence scope. Do not state that a claim is unsupported and then admit that the source supports it. Do not raise medical_overclaim against bullet text that preserves the source's own hedged or associative wording near-verbatim; an overclaim requires the draft to state something stronger than its exact source passage. Use no outside facts. Use warning for useful non-gating notes, review for ambiguity requiring human judgment, and blocking for a materially unsupported, misattributed, or unsafe claim. Return an empty issues list when there is no grounded objection. Deterministic checks are authoritative for exact source containment, provenance, and near-verbatim similarity.",
            "SOURCE_PACKET_WITH_NORMALIZED_SECTIONS_AND_REFERENCES:\n" + json.dumps(source, indent=2),
            "DRAFT_PLAN:\n" + json.dumps(plan, indent=2),
            "SELECTED_CANDIDATE_METADATA:\n" + json.dumps(selected_candidate, indent=2),
            "SELECTED_TARGET_MARKDOWN:\n" + selected_target_markdown,
            "DETERMINISTIC_ISSUES:\n" + json.dumps(deterministic_issues),
        ]
    )


def critic_issue_is_self_contradictory(code: str, explanation: str) -> bool:
    lowered = normalize_evidence(explanation)
    contradictions = {
        "unsupported_claim": (
            "source supports",
            "is supported by the source",
            "source supported",
            "text is supported",
            "claim is supported",
            "it is supported",
        ),
        "medical_overclaim": ("does not overclaim", "appropriately scoped", "correctly scoped"),
        "study_type_inflation": ("properly scoped", "correctly scoped", "evidence scope is correct"),
        "merged_ideas": ("ideas are separate", "draft separates", "does not merge"),
        "wrong_target_page": ("target is appropriate", "correct target", "page is appropriate"),
    }
    return any(normalize_evidence(phrase) in lowered for phrase in contradictions.get(code, ()))


def validate_critic_review(
    review: dict[str, Any],
    *,
    review_kind: str,
    packet: dict[str, Any],
    plan: dict[str, Any],
    selected_target_markdown: str,
    seeded_catch_all: bool = False,
) -> dict[str, Any]:
    allowed_codes = PLACEMENT_ISSUE_CODES if review_kind == "placement" else EVIDENCE_ISSUE_CODES
    source = source_evidence(packet)
    bullets = plan.get("bullets") if isinstance(plan.get("bullets"), list) else []
    operation = str(plan.get("operation") or "append_existing")
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    raw_issues = review.get("issues")
    response_valid = isinstance(raw_issues, list)
    if not response_valid:
        raw_issues = []
    for raw in raw_issues:
        errors: list[str] = []
        if not isinstance(raw, dict):
            rejected.append({"issue": raw, "validation_errors": ["issue_not_object"]})
            continue
        code = str(raw.get("code") or "")
        severity = str(raw.get("severity") or "")
        explanation = str(raw.get("explanation") or "").strip()
        source_quote = str(raw.get("source_quote") or "")
        target_quote = str(raw.get("target_quote") or "")
        bullet_index = raw.get("bullet_index")
        validation_warnings: list[str] = []
        if code not in allowed_codes:
            errors.append("invalid_issue_code")
        if severity not in CRITIC_SEVERITIES:
            errors.append("invalid_severity")
        if not explanation:
            errors.append("missing_explanation")
        if bullet_index is not None and (
            not isinstance(bullet_index, int) or isinstance(bullet_index, bool) or bullet_index < 0 or bullet_index >= len(bullets)
        ):
            errors.append("invalid_bullet_index")
        source_quote_exact = bool(source_quote and source_quote in source)
        target_quote_exact = bool(target_quote and target_quote in selected_target_markdown)
        if source_quote and not source_quote_exact:
            if review_kind == "evidence" or code == "wrong_target_page" or not target_quote_exact:
                errors.append("source_quote_not_exact")
            else:
                source_quote = ""
                validation_warnings.append("discarded_optional_nonexact_source_quote")
        if target_quote and not target_quote_exact:
            if code == "wrong_target_page" or not source_quote_exact:
                errors.append("target_quote_not_exact")
            else:
                target_quote = ""
                validation_warnings.append("discarded_optional_nonexact_target_quote")
        if not source_quote and not target_quote:
            errors.append("missing_evidence_quote")
        if review_kind == "evidence" and not source_quote:
            errors.append("evidence_review_requires_source_quote")
        if code == "wrong_target_page" and operation != "create_new" and (not source_quote or not target_quote):
            errors.append("wrong_target_requires_source_and_target_quotes")
        if critic_issue_is_self_contradictory(code, explanation):
            errors.append("self_contradictory_issue")
        heading = normalize_evidence(str(plan.get("heading") or ""))
        if (
            code == "wrong_heading"
            and source_is_preclinical(packet)
            and re.search(r"\b(?:animal evidence|animal models|preclinical evidence)\b", heading)
            and re.search(r"\b(?:redundant|overly specific|non standard|remove|omit)\b", normalize_evidence(explanation))
        ):
            errors.append("contradicts_deterministic_preclinical_scope")
        minimum_severity = MINIMUM_CRITIC_SEVERITY.get(code)
        if (
            severity in CRITIC_SEVERITY_RANK
            and minimum_severity
            and CRITIC_SEVERITY_RANK[severity] < CRITIC_SEVERITY_RANK[minimum_severity]
        ):
            validation_warnings.append(f"severity_promoted_from_{severity}")
            severity = minimum_severity
        # A bullet whose text is near-verbatim to its own exact source passage
        # cannot state more than the source does; an overclaim objection against
        # it contradicts the authoritative near-verbatim gate, so it is kept
        # only as a non-gating note for the human reviewer.
        if (
            code == "medical_overclaim"
            and severity in {"review", "blocking"}
            and not target_quote
            and isinstance(bullet_index, int)
            and not isinstance(bullet_index, bool)
            and 0 <= bullet_index < len(bullets)
            and isinstance(bullets[bullet_index], dict)
        ):
            bullet = bullets[bullet_index]
            bullet_text = str(bullet.get("text") or "")
            bullet_quote = str(bullet.get("source_quote") or "")
            if (
                bullet_text
                and bullet_quote
                and bullet_quote in source
                and word_token_similarity(bullet_text, bullet_quote) >= 0.68
            ):
                validation_warnings.append(
                    f"severity_demoted_from_{severity}_near_verbatim_bullet"
                )
                severity = "warning"
        # The seeded catch-all page is created to host scoped findings about
        # products derived from its category; an entity objection whose own
        # quote contains the target entity is a scope note, not a misplacement.
        target_entity = normalize_evidence(str(plan.get("target_entity") or ""))
        if (
            seeded_catch_all
            and code == "entity_not_supported"
            and severity in {"review", "blocking"}
            and target_entity
            and target_entity in normalize_evidence(source_quote)
        ):
            validation_warnings.append(f"severity_demoted_from_{severity}_seeded_catch_all_scope")
            severity = "warning"
        normalized = {
            "code": code,
            "severity": severity,
            "bullet_index": bullet_index,
            "explanation": explanation,
            "source_quote": source_quote,
            "target_quote": target_quote,
        }
        if validation_warnings:
            normalized["validation_warnings"] = validation_warnings
        if errors:
            rejected.append({"issue": normalized, "validation_errors": sorted(set(errors))})
        else:
            accepted.append(normalized)
    return {
        "kind": review_kind,
        "response_valid": response_valid,
        "issues": accepted,
        "rejected_issues": rejected,
    }


def combine_critic_reviews(placement: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    issues = [*(placement.get("issues") or []), *(evidence.get("issues") or [])]
    actionable = [issue for issue in issues if issue.get("severity") in {"review", "blocking"}]
    responses_valid = placement.get("response_valid") is True and evidence.get("response_valid") is True
    approved = responses_valid and not actionable
    recommendation = "approve" if approved else "needs_review" if (
        not responses_valid
        or any(issue.get("severity") == "blocking" for issue in actionable)
    ) else "revise"
    return {
        "approved": approved,
        "recommendation": recommendation,
        "issues": issues,
        "placement_review": placement,
        "evidence_review": evidence,
        "validation": {
            "responses_valid": responses_valid,
            "rejected_issue_count": len(placement.get("rejected_issues") or [])
            + len(evidence.get("rejected_issues") or []),
            "decision_derived_from_validated_issues": True,
        },
    }


def format_gate_summary(
    *,
    packet: dict[str, Any],
    packet_validation: dict[str, Any],
    duplicate: dict[str, Any] | None,
    plan: dict[str, Any],
    deterministic: ValidationResult,
    rendered_results: list[dict[str, Any]],
) -> str:
    """Per-gate PR-body summary with per-bullet evidence scores."""

    def outcome(ok: bool, detail: str) -> str:
        return f"{'pass' if ok else 'FAIL'} — {detail}"

    duplicate = duplicate or {}
    paper_state = str(
        ((duplicate.get("paper_result") or {}).get("paper") or {}).get("workflow_state")
        or "none"
    )
    lines = [
        "### Gate summary",
        "",
        "- Packet: "
        + outcome(
            bool(packet_validation.get("ok")),
            f"{len(packet_validation.get('issues') or [])} issues",
        ),
        "- Duplicate: "
        + outcome(
            True,
            f"{duplicate.get('content_hit_count', 0)} content hits; prior paper state: {paper_state}",
        ),
        "- Plan (entity, exact-quote, near-verbatim, preclinical placement): "
        + outcome(
            deterministic.ok,
            f"{len(deterministic.issues)} issues, {len(deterministic.warnings)} warnings",
        ),
        "- Rendered Markdown: "
        + outcome(
            all(result.get("ok") for result in rendered_results),
            f"{len(rendered_results)} target render(s)",
        ),
        "",
        "Per-bullet evidence:",
        "",
        "| Target | Bullet | Scope | Entity | Exact quote | Near-verbatim |",
        "|---|---|---|---|---|---|",
    ]
    for proposal in plan_target_proposals(plan):
        target = PurePosixPath(str(proposal.get("target_path") or "")).name or "?"
        target_entity = str(proposal.get("target_entity") or "")
        for index, item in enumerate(proposal.get("bullets") or []):
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "")
            quote = str(item.get("source_quote") or "")
            entity_ok = not target_entity or (
                phrase_in_text(target_entity, quote) and phrase_in_text(target_entity, text)
            )
            exact = exact_source_passage(packet, quote)
            score = word_token_similarity(strip_scope_prefix(text, packet), quote)
            lines.append(
                f"| `{target}` | {index} | {item.get('evidence_scope') or '—'} "
                f"| {'✓' if entity_ok else '✗'} | {'✓' if exact else '✗'} | {score:.2f} |"
            )
    return "\n".join(lines)


def _critic_markdown_inline(value: Any) -> str:
    return " ".join(str(value or "").split()).replace("`", "\\`")


def _format_validated_critic_findings(critic: dict[str, Any]) -> str:
    findings = []
    for item in critic.get("issues") or []:
        if not isinstance(item, dict):
            findings.append(f"- Unstructured validated finding: {_critic_markdown_inline(item)}")
            continue
        severity = _critic_markdown_inline(item.get("severity") or "unknown_severity")
        code = _critic_markdown_inline(item.get("code") or "unknown_issue")
        bullet_index = item.get("bullet_index")
        bullet = f" (bullet {bullet_index})" if isinstance(bullet_index, int) and not isinstance(bullet_index, bool) else ""
        explanation = _critic_markdown_inline(item.get("explanation") or "No explanation supplied.")
        findings.append(f"- `{severity}` `{code}`{bullet}: {explanation}")
    return "\n".join(findings) or "- None"


def _format_rejected_critic_observations(critic: dict[str, Any]) -> str:
    observations = []
    target_reviews = critic.get("target_reviews")
    if isinstance(target_reviews, list):
        for target_index, target_review in enumerate(target_reviews):
            if not isinstance(target_review, dict):
                continue
            nested = _format_rejected_critic_observations(target_review)
            if nested != "- None":
                target_path = _critic_markdown_inline(target_review.get("target_path"))
                observations.append(
                    f"- Target {target_index} `{target_path}`:\n"
                    + "\n".join(f"  {line}" for line in nested.splitlines())
                )
    for review_kind, review_key in (
        ("placement", "placement_review"),
        ("evidence", "evidence_review"),
    ):
        review = critic.get(review_key) if isinstance(critic.get(review_key), dict) else {}
        for rejected in review.get("rejected_issues") or []:
            if not isinstance(rejected, dict):
                observations.append(
                    f"- `{review_kind}` malformed rejected observation: {_critic_markdown_inline(rejected)}"
                )
                continue
            issue = rejected.get("issue")
            errors = [
                _critic_markdown_inline(error)
                for error in rejected.get("validation_errors") or []
                if _critic_markdown_inline(error)
            ]
            error_text = ", ".join(f"`{error}`" for error in errors) or "not specified"
            if not isinstance(issue, dict):
                observations.append(
                    f"- `{review_kind}` malformed critic issue: {_critic_markdown_inline(issue)} "
                    f"Validation errors: {error_text}."
                )
                continue
            severity = _critic_markdown_inline(issue.get("severity") or "unknown_severity")
            code = _critic_markdown_inline(issue.get("code") or "unknown_issue")
            bullet_index = issue.get("bullet_index")
            bullet = (
                f" (bullet {bullet_index})"
                if isinstance(bullet_index, int) and not isinstance(bullet_index, bool)
                else ""
            )
            explanation = _critic_markdown_inline(issue.get("explanation") or "No explanation supplied.")
            observations.append(
                f"- `{review_kind}` `{severity}` `{code}`{bullet}: {explanation} "
                f"Validation errors: {error_text}."
            )
    return "\n".join(observations) or "- None"


def format_critic_pr_audit(
    critic: dict[str, Any],
    *,
    override_applied: bool = False,
    override_reason: str = "",
) -> str:
    """Render authoritative and rejected critic results separately for a PR."""
    validated = _format_validated_critic_findings(critic)
    rejected = _format_rejected_critic_observations(critic)
    sections = []
    if override_applied:
        sections.append(
            "**Critic rejection override applied after human review.**\n\n"
            f"Override reason: {_critic_markdown_inline(override_reason)}"
        )
    sections.extend(
        [
            f"### Validated critic findings\n\n{validated}",
            (
                "### Rejected critic observations (non-blocking)\n\n"
                "These observations failed critic-evidence validation and did not affect "
                "the publication gate decision.\n\n"
                f"{rejected}"
            ),
        ]
    )
    return "\n\n".join(sections)


def deterministic_placement_review_issues(
    *,
    packet: dict[str, Any],
    plan: dict[str, Any],
    selected_target_markdown: str,
) -> list[dict[str, Any]]:
    """Require review before introducing a new isolated compound as a second entity."""
    target_normalized = normalize_evidence(selected_target_markdown)
    bullets = plan.get("bullets") if isinstance(plan.get("bullets"), list) else []
    issues: list[dict[str, Any]] = []
    sentences = [
        sentence
        for key in ("title", "abstract", "body_markdown")
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", str(packet.get(key) or ""))
        if sentence.strip()
    ]
    seen: set[tuple[str, str]] = set()
    for sentence in sentences:
        if not re.search(r"\b(?:component|compound|metabolite|constituent|phytochemical)s?\b", sentence, re.IGNORECASE):
            continue
        for match in re.finditer(
            r"\b([A-Za-z][A-Za-z-]{4,})\s+\(([A-Z][A-Z0-9-]{1,9})\)",
            sentence,
        ):
            name, acronym = match.group(1), match.group(2)
            following_context = sentence[match.end() : match.end() + 40]
            if re.search(
                r"\b(?:cells?|patients?|disease|malignancy|cancer|tumou?r|syndrome|models?)\b",
                following_context,
                re.IGNORECASE,
            ):
                continue
            key = (name.lower(), acronym.lower())
            if key in seen:
                continue
            seen.add(key)
            if normalize_evidence(name) in target_normalized or re.search(
                rf"\b{re.escape(acronym.lower())}\b", target_normalized
            ):
                continue
            matching_indexes = [
                index
                for index, item in enumerate(bullets)
                if isinstance(item, dict)
                and re.search(
                    rf"\b(?:{re.escape(name)}|{re.escape(acronym)})\b",
                    str(item.get("text") or ""),
                    re.IGNORECASE,
                )
            ]
            if len(matching_indexes) < 2:
                continue
            target_quote = next(
                (
                    line.strip()
                    for line in selected_target_markdown.splitlines()
                    if line.strip().lower().startswith("title:")
                    or line.strip().startswith("# ")
                ),
                "",
            )
            if not target_quote:
                target_quote = next(
                    (line.strip() for line in selected_target_markdown.splitlines() if line.strip()),
                    "",
                )
            if not target_quote:
                continue
            issues.append(
                {
                    "code": "unsafe_context_inference",
                    "severity": "review",
                    "bullet_index": matching_indexes[0],
                    "explanation": (
                        f"The plan adds {len(matching_indexes)} {name} ({acronym})-specific bullets "
                        f"to a target that does not currently mention {name} or {acronym}; "
                        "restrict the plan to the selected target's entity or require human placement review."
                    ),
                    "source_quote": sentence,
                    "target_quote": target_quote,
                    "origin": "deterministic_placement_policy",
                }
            )
    return issues


def new_article_recommendation(
    packet: dict[str, Any],
    *,
    reason: str,
    rejected_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a review-only recommendation; this is never an article-creation plan."""
    title = str(packet.get("title") or "Untitled research source").strip()
    keywords = [
        item.strip()
        for item in re.split(r"[,;]", str(packet.get("keywords") or ""))
        if item.strip()
    ]
    return {
        "recommendation": "consider_new_article",
        "proposed_title": title,
        "source_entities": keywords[:8],
        "evidence_scope": "animal" if source_is_preclinical(packet) else str(packet.get("study_type") or "unspecified"),
        "rationale": reason,
        "rejected_existing_targets": [
            {
                "path": str(item.get("path") or ""),
                "title": str(item.get("title") or ""),
                "reason": "no discriminative source-entity match",
            }
            for item in (rejected_candidates or [])
        ],
        "requires_human_review": True,
        "automatic_creation": False,
        "automatic_publication": False,
    }


def review_plan_targets(
    *,
    client: LocalLLMClient,
    packet: dict[str, Any],
    plan: dict[str, Any],
    candidates: list[dict[str, Any]],
    candidate_documents: dict[str, str],
    deterministic_issues: list[str],
    critic_mode: str,
    claim_policy: str = "integrated",
    new_article_seed: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run independent placement and evidence criticism for every target."""
    if critic_mode == "off":
        return {
            "approved": True,
            "recommendation": "skipped",
            "issues": [],
            "mode": "off",
            "target_reviews": [],
            "skipped_reason": "critic_mode_off",
        }
    target_reviews: list[dict[str, Any]] = []
    all_issues: list[dict[str, Any]] = []
    for target_index, proposal in enumerate(plan_target_proposals(plan)):
        selected_path = str(proposal.get("target_path") or "")
        selected_target_markdown = candidate_documents.get(selected_path, "")
        selected_candidate = next(
            (
                candidate
                for candidate in candidates
                if str(candidate.get("path") or "") == selected_path
            ),
            {},
        )
        operation = proposal_operation(plan, proposal)
        if operation == "create_new":
            selected_candidate = {
                "operation": "create_new",
                "path": selected_path,
                "title": str(((proposal.get("new_article") or {}).get("title") or "")),
                "domain": PurePosixPath(selected_path).parts[0] if selected_path else "",
                "new_article": proposal.get("new_article") or {},
            }
        seed = new_article_seed or {}
        seeded_catch_all = operation == "create_new" and bool(seed) and (
            selected_path == str(seed.get("suggested_path") or "")
            or normalize_evidence(str(proposal.get("target_entity") or ""))
            == normalize_evidence(str(seed.get("target_entity") or ""))
        )
        raw_placement = client.json_completion(
            system="You are an independent target-placement reviewer. Use only supplied evidence and return only the requested JSON.",
            user=placement_critic_prompt(
                packet=packet,
                plan=proposal,
                selected_candidate=selected_candidate,
                selected_target_markdown=selected_target_markdown,
                deterministic_issues=deterministic_issues,
                claim_policy=claim_policy,
                new_article_seed=new_article_seed,
            ),
            max_tokens=2500,
        )
        raw_evidence = client.json_completion(
            system="You are an independent evidence and medical-overclaim reviewer. Use only supplied evidence and return only the requested JSON.",
            user=evidence_critic_prompt(
                packet=packet,
                plan=proposal,
                selected_candidate=selected_candidate,
                selected_target_markdown=selected_target_markdown,
                deterministic_issues=deterministic_issues,
                claim_policy=claim_policy,
            ),
            max_tokens=3000,
        )
        placement = validate_critic_review(
            raw_placement,
            review_kind="placement",
            packet=packet,
            plan=proposal,
            selected_target_markdown=selected_target_markdown,
            seeded_catch_all=seeded_catch_all,
        )
        placement["issues"].extend(
            deterministic_placement_review_issues(
                packet=packet,
                plan=proposal,
                selected_target_markdown=selected_target_markdown,
            )
        )
        evidence_review = validate_critic_review(
            raw_evidence,
            review_kind="evidence",
            packet=packet,
            plan=proposal,
            selected_target_markdown=selected_target_markdown,
        )
        target_review = combine_critic_reviews(placement, evidence_review)
        target_review.update(
            {
                "target_index": target_index,
                "target_path": selected_path,
                "raw_reviews": {
                    "placement": raw_placement,
                    "evidence": raw_evidence,
                },
            }
        )
        target_reviews.append(target_review)
        for issue in target_review.get("issues") or []:
            tagged = dict(issue) if isinstance(issue, dict) else {"code": str(issue)}
            tagged["target_index"] = target_index
            tagged["target_path"] = selected_path
            all_issues.append(tagged)
    approved = bool(target_reviews) and all(
        review.get("approved") is True for review in target_reviews
    )
    if approved:
        recommendation = "approve"
    elif any(
        review.get("recommendation") == "needs_review" for review in target_reviews
    ) or not target_reviews:
        recommendation = "needs_review"
    else:
        recommendation = "revise"
    return {
        "approved": approved,
        "recommendation": recommendation,
        "issues": all_issues,
        "mode": critic_mode,
        "target_reviews": target_reviews,
        "validation": {
            "all_targets_reviewed": len(target_reviews) == len(plan_target_proposals(plan)),
            "target_review_count": len(target_reviews),
            "decision_derived_from_validated_issues": True,
        },
    }


def attempt_quality(attempt: dict[str, Any]) -> tuple[int, int, int, int]:
    """Sort deterministic-valid attempts by critic outcome, deterministically."""
    critic = attempt.get("critic") if isinstance(attempt.get("critic"), dict) else {}
    issues = critic.get("issues") if isinstance(critic.get("issues"), list) else []
    blocking = sum(
        1
        for issue in issues
        if isinstance(issue, dict) and issue.get("severity") == "blocking"
    )
    actionable = sum(
        1
        for issue in issues
        if isinstance(issue, dict) and issue.get("severity") in {"review", "blocking"}
    )
    return (
        0 if critic.get("approved") is True else 1,
        blocking,
        actionable,
        int(attempt.get("attempt") or 0),
    )


def critic_blocks_publication(critic: dict[str, Any]) -> bool:
    """Fail closed on blocking findings or invalid critic responses.

    Validated review-severity findings no longer stop the run: they are
    published in the draft PR's critic audit for the human reviewer, who
    remains the final gate because draft PRs are never auto-merged.
    """
    if critic.get("approved") is True:
        return False
    target_reviews = critic.get("target_reviews")
    if not isinstance(target_reviews, list) or not target_reviews:
        return True
    for target_review in target_reviews:
        if not isinstance(target_review, dict):
            return True
        if target_review.get("recommendation") not in {"approve", "revise"}:
            return True
    return False


def unresolved_balance_findings(critic: dict[str, Any]) -> list[dict[str, Any]]:
    """Validated review/blocking findings of the balance/omitted-qualifier class."""
    return [
        issue
        for issue in critic.get("issues") or []
        if isinstance(issue, dict)
        and issue.get("code") in BALANCE_FINDING_CODES
        and issue.get("severity") in {"review", "blocking"}
    ]


def balance_finding_repairable(finding: dict[str, Any], packet: dict[str, Any]) -> bool:
    """A finding is auto-repairable only when its qualifier is verbatim in the source."""
    return exact_source_passage(packet, str(finding.get("source_quote") or ""))


def balance_repair_prompt(
    *,
    packet: dict[str, Any],
    plan: dict[str, Any],
    findings: list[dict[str, Any]],
    claim_policy: str,
) -> str:
    """One bounded repair request: add or extend the omitted qualifier, nothing else."""
    source = prompt_source_packet(packet)
    return (
        "TASK: Repair a validated publish plan that omits balancing qualifier language "
        "the source states explicitly. Return the complete corrected plan as one JSON "
        "object in exactly the same contract as CURRENT_PLAN.\n\n"
        "CURRENT_PLAN:\n" + json.dumps(plan, ensure_ascii=False) + "\n\n"
        "VALIDATED_BALANCE_FINDINGS (each source_quote is an exact source passage):\n"
        + json.dumps(
            [
                {
                    "code": finding.get("code"),
                    "bullet_index": finding.get("bullet_index"),
                    "explanation": finding.get("explanation"),
                    "source_quote": finding.get("source_quote"),
                    "target_index": finding.get("target_index"),
                    "target_path": finding.get("target_path"),
                }
                for finding in findings
            ],
            ensure_ascii=False,
        )
        + "\n\nSOURCE:\n" + json.dumps(source, ensure_ascii=False) + "\n\n"
        f"CLAIM POLICY: {claim_policy}\n"
        "Allowed changes, per finding, exactly one of:\n"
        "(a) Append ONE new bullet to the same target whose text is near-verbatim of the "
        "finding's source_quote, whose source_quote is that exact passage, with the same "
        "claim_kind, evidence scope, and source_section conventions as its neighbors; or\n"
        "(b) Extend the flagged bullet's text with the qualifier clause, updating its "
        "source_quote to one exact contiguous source span that contains the extended text.\n"
        "Every other bullet, target, path, operation, heading, tag, lead, exclusion, and "
        "field must remain byte-identical. Do not drop, reorder, reword, or merge existing "
        "bullets. Do not add any bullet beyond the findings above. Do not add footnote "
        "markers; the renderer adds them."
    )


def waive_qualifier_entity_issues(
    deterministic: ValidationResult,
    repaired_plan: dict[str, Any],
    findings: list[dict[str, Any]],
) -> ValidationResult:
    """Waive entity-assertion issues only for the repair's own qualifier bullets.

    A balancing qualifier sentence rarely restates the target entity; it is
    bound to the flagged bullet by the validated critic finding instead. The
    bullet-count cap may be exceeded only by the qualifier bullets themselves.
    The exact-passage and near-verbatim gates are never waived.
    """
    qualifier_quotes = {
        str(finding.get("source_quote") or "")
        for finding in findings
        if str(finding.get("source_quote") or "")
    }

    def is_qualifier_bullet(bullet: Any) -> bool:
        quote = str(bullet.get("source_quote") or "") if isinstance(bullet, dict) else ""
        return bool(quote) and any(
            quote in qualifier or qualifier in quote for qualifier in qualifier_quotes
        )

    proposals = plan_target_proposals(repaired_plan)
    kept: list[str] = []
    waived: list[str] = []
    for issue in deterministic.issues:
        entity_match = re.fullmatch(
            r"target_(\d+)_bullet_(\d+)_entity_not_supported_by_passage", issue
        )
        count_match = re.fullmatch(r"target_(\d+)_invalid_bullet_count", issue)
        if entity_match:
            target_index, bullet_index = int(entity_match.group(1)), int(entity_match.group(2))
            if target_index < len(proposals):
                bullets = proposals[target_index].get("bullets") or []
                if bullet_index < len(bullets) and is_qualifier_bullet(bullets[bullet_index]):
                    waived.append(f"{issue}_waived_balance_qualifier")
                    continue
        elif count_match:
            target_index = int(count_match.group(1))
            if target_index < len(proposals):
                bullets = proposals[target_index].get("bullets") or []
                qualifier_count = sum(1 for bullet in bullets if is_qualifier_bullet(bullet))
                if bullets and 1 <= len(bullets) - qualifier_count <= 8:
                    waived.append(f"{issue}_waived_balance_qualifier")
                    continue
        kept.append(issue)
    return ValidationResult(not kept, kept, [*deterministic.warnings, *waived])


def validate_balance_repair(
    original_plan: dict[str, Any],
    repaired_plan: dict[str, Any],
    findings: list[dict[str, Any]],
    packet: dict[str, Any],
) -> ValidationResult:
    """Deterministically confirm the repair only added or extended qualifier text."""
    issues: list[str] = []
    if repaired_plan.get("decision") not in CHANGE_DECISIONS:
        return ValidationResult(False, ["balance_repair_abandoned_plan"])
    original_targets = {
        str(proposal.get("target_path") or ""): proposal
        for proposal in plan_target_proposals(original_plan)
    }
    repaired_targets = {
        str(proposal.get("target_path") or ""): proposal
        for proposal in plan_target_proposals(repaired_plan)
    }
    if set(original_targets) != set(repaired_targets):
        issues.append("balance_repair_changed_targets")
    added_bullets = 0
    for path, original in original_targets.items():
        repaired = repaired_targets.get(path)
        if repaired is None:
            continue
        original_bullets = original.get("bullets") if isinstance(original.get("bullets"), list) else []
        repaired_bullets = repaired.get("bullets") if isinstance(repaired.get("bullets"), list) else []
        repaired_texts = [
            str(bullet.get("text") or "")
            for bullet in repaired_bullets
            if isinstance(bullet, dict)
        ]
        for bullet in original_bullets:
            text = str(bullet.get("text") or "") if isinstance(bullet, dict) else ""
            if text and not any(text in candidate for candidate in repaired_texts):
                issues.append("balance_repair_dropped_or_rewrote_bullet")
                break
        added_bullets += max(0, len(repaired_bullets) - len(original_bullets))
    if added_bullets > len(findings):
        issues.append("balance_repair_added_unrelated_bullets")
    for finding in findings:
        quote = str(finding.get("source_quote") or "")
        satisfied = False
        for proposal in plan_target_proposals(repaired_plan):
            for bullet in proposal.get("bullets") or []:
                if not isinstance(bullet, dict):
                    continue
                bullet_quote = str(bullet.get("source_quote") or "")
                bullet_text = str(bullet.get("text") or "")
                if quote and (
                    quote in bullet_quote
                    or word_token_similarity(bullet_text, quote) >= 0.68
                ):
                    satisfied = True
        if not satisfied:
            issues.append("balance_repair_qualifier_missing")
            break
    return ValidationResult(not issues, sorted(set(issues)))


def critic_rejects_all_target_entities(critic: dict[str, Any]) -> bool:
    """Return whether every proposed target has a grounded entity-placement rejection."""
    target_reviews = critic.get("target_reviews")
    if not isinstance(target_reviews, list) or not target_reviews:
        return False
    entity_codes = {
        "wrong_target_page",
        "unsafe_context_inference",
        "new_article_not_warranted",
        "wrong_category",
    }
    for target_review in target_reviews:
        if not isinstance(target_review, dict):
            return False
        if not any(
            isinstance(issue, dict)
            and issue.get("code") in entity_codes
            and issue.get("severity") in {"review", "blocking"}
            for issue in target_review.get("issues") or []
        ):
            return False
    return True


def run_local_publish(
    *,
    source: str,
    alert_name: str,
    content_repo: Path,
    tools_root: Path,
    output_dir: Path,
    base_url: str,
    model: str,
    publish: bool,
    base_ref: str = "origin/main",
    max_candidates: int = 12,
    max_draft_attempts: int = 3,
    critic_mode: str = "required",
    claim_policy: str = "integrated",
    allow_critic_rejection: bool = False,
    override_reason: str = "",
    passive_worker: bool = False,
    domain: str = "Natural Healing",
    abstract_mode: str = "full",
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    # Imported lazily to keep the pure validators independently testable.
    from .cli import (
        check_duplicate_paper,
        load_articles,
        match_research_packet,
        normalize_domain,
        scrape_source_packet,
        slugify,
    )
    import argparse

    if critic_mode not in CRITIC_MODES:
        raise ValueError(f"critic_mode must be one of: {', '.join(sorted(CRITIC_MODES))}")
    if claim_policy not in CLAIM_POLICIES:
        raise ValueError(f"claim_policy must be one of: {', '.join(sorted(CLAIM_POLICIES))}")
    if abstract_mode not in ABSTRACT_MODES:
        raise ValueError(f"abstract_mode must be one of: {', '.join(sorted(ABSTRACT_MODES))}")
    domain = normalize_domain(domain)
    if passive_worker and allow_critic_rejection:
        raise ValueError("Critic rejection overrides are prohibited in the passive database worker")
    if passive_worker and critic_mode != "required":
        raise ValueError("The passive database worker requires --critic-mode required")
    if allow_critic_rejection and critic_mode != "required":
        raise ValueError("--allow-critic-rejection requires --critic-mode required")
    if allow_critic_rejection and not override_reason.strip():
        raise ValueError("--allow-critic-rejection requires a non-empty --override-reason")
    if allow_critic_rejection and not publish:
        raise ValueError("--allow-critic-rejection is only valid with --publish")
    if publish and critic_mode == "off":
        raise ValueError("--critic-mode off is manual dry-run only and cannot be used with --publish")

    publication_suppressed = "critic_mode_advisory" if publish and critic_mode == "advisory" else ""
    run_started = datetime.now(timezone.utc)
    run_started_monotonic = time.monotonic()
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = slugify(Path(source).stem or "research") or "research"
    run_id = (
        run_started.strftime("%Y%m%dT%H%M%S.%fZ")
        + f"-{slug[:40]}-"
        + hashlib.sha256(source.encode("utf-8")).hexdigest()[:10]
    )
    run_dir = output_dir / "runs" / run_id
    run_dir.mkdir(parents=True)
    scrape_markdown = run_dir / "source.md"
    packet_path = run_dir / "packet.json"
    report_path = run_dir / "report.json"
    diff_path = run_dir / "proposed.patch"
    base_snapshot_temp: tempfile.TemporaryDirectory[str] | None = None
    client: LocalLLMClient | None = None
    duplicate: dict[str, Any] | None = None

    def finish(report: dict[str, Any]) -> dict[str, Any]:
        completed_at = datetime.now(timezone.utc)
        status = str(report.get("status") or "needs_review")
        if status == "pr_open":
            publication_outcome = "draft_pr_opened"
        elif status == "validated_draft":
            publication_outcome = "suppressed" if publication_suppressed else "dry_run"
        else:
            publication_outcome = "not_published"
        report.setdefault("source", source)
        report.setdefault("domain", domain)
        report.setdefault("critic_mode", critic_mode)
        report.setdefault("claim_policy", claim_policy)
        report.setdefault("publication_requested", publish)
        report.setdefault("publication_suppressed", publication_suppressed)
        report.setdefault("duplicate", duplicate)
        report.update(
            {
                "run_id": run_id,
                "started_at": run_started.isoformat(),
                "completed_at": completed_at.isoformat(),
                "duration_seconds": round(time.monotonic() - run_started_monotonic, 3),
                "publication_outcome": publication_outcome,
                "model_calls": (
                    list(client.calls)
                    if client is not None and isinstance(client.calls, list)
                    else []
                ),
                "report_path": str(report_path),
                "artifacts": {
                    "run_directory": str(run_dir),
                    "source_markdown": str(scrape_markdown) if scrape_markdown.exists() else "",
                    "packet_json": (
                        str(packet_path) if isinstance(report.get("packet"), dict) else ""
                    ),
                    "report_json": str(report_path),
                    "proposed_patch": str(diff_path) if diff_path.exists() else "",
                },
                "runtime": {
                    "base_url": base_url,
                    "model": model,
                    "tools_revision": git_revision(tools_root),
                    "tools_worktree": git_worktree_provenance(tools_root),
                    "content_base_ref": base_ref,
                    "content_base_revision": git_revision(content_repo, base_ref),
                    "options": {
                        "alert_name": alert_name,
                        "max_candidates": max_candidates,
                        "max_draft_attempts": max_draft_attempts,
                        "critic_mode": critic_mode,
                        "claim_policy": claim_policy,
                        "domain": domain,
                        "publish": publish,
                        "passive_worker": passive_worker,
                    },
                },
            }
        )
        packet_value = report.get("packet")
        if isinstance(packet_value, dict):
            packet_path.write_text(json.dumps(packet_value, indent=2), encoding="utf-8")
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        if base_snapshot_temp is not None:
            base_snapshot_temp.cleanup()
        return report

    try:
        packet = scrape_source_packet(source, scrape_markdown)
    except Exception as exc:
        return finish(
            {
                "status": "needs_review",
                "reason": "source_retrieval_failed",
                "retrieval_error": str(exc)[:4000],
            }
        )
    packet_validation = validate_source_packet(packet)
    if not packet_validation.ok:
        report = {
            "source": source,
            "status": "needs_review",
            "reason": "invalid_source_packet",
            "packet": packet,
            "packet_validation": asdict(packet_validation),
        }
        return finish(report)
    if progress:
        progress("matching")

    publication_enabled = publish and critic_mode == "required"
    if publication_enabled:
        run_command(["git", "fetch", "origin", "main"], cwd=content_repo)
    base_snapshot_temp = materialize_git_ref(content_repo=content_repo, git_ref=base_ref)
    base_snapshot = Path(base_snapshot_temp.name)

    duplicate_checks = [
        check_duplicate_paper(
            argparse.Namespace(
                identifier=identifier,
                limit=max_candidates,
                repo_root=str(base_snapshot),
            )
        )
        for identifier in duplicate_identifiers(packet, source)
    ]
    duplicate = merge_duplicate_checks(duplicate_checks)
    paper_result = duplicate.get("paper_result") or {}
    paper_state = str((paper_result.get("paper") or {}).get("workflow_state") or "")
    if duplicate.get("content_hit_count", 0) > 0 or paper_state in {
        "drafted", "committed", "pr_open", "merged"
    }:
        report = {
            "status": "duplicate",
            "source": source,
            "reason": "content_reference" if duplicate.get("content_hit_count", 0) else f"paper_state:{paper_state}",
            "duplicate": duplicate,
            "packet": packet,
            "packet_validation": asdict(packet_validation),
        }
        return finish(report)

    articles = load_articles(base_snapshot)
    existing_paths = {article.path for article in articles}
    ranked_candidates = match_research_packet(
        articles,
        packet,
        alert_name=alert_name,
        include_background=includes_background_claims(claim_policy),
        domain=domain,
        limit=max(max_candidates * 10, 100),
    )
    candidates = ranked_candidates[:max_candidates]
    candidate_retrieval = {
        "domain": domain,
        "eligible_count": len(ranked_candidates),
        "selected_count": len(candidates),
        "candidate_limit": max_candidates,
        "rejected_candidates": ranked_candidates[max_candidates : max_candidates + 20],
    }
    candidate_documents: dict[str, str] = {}
    per_candidate_context = max(
        2000,
        MAX_ALL_CANDIDATE_CONTEXT_CHARS // max(len(candidates), 1),
    )
    for candidate in candidates:
        path = base_snapshot / candidate["path"]
        candidate_documents[candidate["path"]] = candidate_document_excerpt(
            path.read_text(encoding="utf-8"),
            [str(value) for value in candidate.get("entity_matches") or []],
            max_chars=per_candidate_context,
        )
    candidate_retrieval.update(
        {
            "per_candidate_context_limit_chars": per_candidate_context,
            "selected_context_chars": sum(len(value) for value in candidate_documents.values()),
        }
    )
    category_catalog = sorted(
        {
            str(PurePosixPath(article.path).parent)
            for article in articles
            if PurePosixPath(article.path).parts
            and PurePosixPath(article.path).parts[0] == domain
        }
    )
    new_article_seed = missing_entity_page_seed(
        packet=packet,
        alert_name=alert_name,
        domain=domain,
        category_catalog=category_catalog,
        existing_paths=existing_paths,
    )
    candidate_retrieval["new_article_seed"] = new_article_seed

    client = LocalLLMClient(base_url, model)
    system = (
        "You are a conservative research publishing planner. Follow the supplied style guides and JSON contract exactly.\n\n"
        + style_context(tools_root)
    )
    plan: dict[str, Any] = {}
    deterministic = ValidationResult(False, ["draft_not_attempted"])
    critic: dict[str, Any] = {
        "approved": critic_mode == "off",
        "recommendation": "skipped" if critic_mode == "off" else "not_run",
        "issues": [],
        "mode": critic_mode,
    }
    prior_issues: list[str] = []
    previous_plan: dict[str, Any] = {}
    previous_critic: dict[str, Any] = {}
    attempt_history: list[dict[str, Any]] = []
    format_repairs: list[dict[str, Any]] = []
    best_valid_attempt: dict[str, Any] | None = None
    candidate_paths = {str(item["path"]) for item in candidates}
    if max_draft_attempts < 1:
        raise ValueError("max_draft_attempts must be at least 1")

    def model_failure(phase: str, exc: Exception) -> dict[str, Any]:
        return finish(
            {
                "status": "needs_review",
                "reason": "model_call_failed",
                "failure_phase": phase,
                "model_error": str(exc)[:4000],
                "source": source,
                "packet": packet,
                "packet_validation": asdict(packet_validation),
                "candidates": candidates,
                "candidate_retrieval": candidate_retrieval,
                "source_prompt_context": prompt_source_packet(packet).get(
                    "prompt_context", {}
                ),
                "attempt_history": attempt_history,
                "format_repairs": format_repairs,
            }
        )

    for attempt_number in range(1, max_draft_attempts + 1):
        if progress:
            progress(f"drafting_attempt_{attempt_number}")
        try:
            plan = client.json_completion(
                system=system,
                user=draft_prompt(
                    packet=packet,
                    candidates=candidates,
                    candidate_documents=candidate_documents,
                    prior_issues=prior_issues,
                    previous_plan=previous_plan,
                    previous_critic=previous_critic,
                    claim_policy=claim_policy,
                    domain=domain,
                    category_catalog=category_catalog,
                    new_article_seed=new_article_seed,
                ),
                max_tokens=MAX_DRAFT_OUTPUT_TOKENS,
            )
        except ModelOutputJSONError as exc:
            source_call = client.calls[-1] if client.calls else {}
            repair_record: dict[str, Any] = {
                "attempt": attempt_number,
                "source_call": len(client.calls),
                "status": "attempting",
                "finish_reason": source_call.get("finish_reason"),
                "raw_output_sha256": hashlib.sha256(
                    exc.raw_output.encode("utf-8")
                ).hexdigest(),
                "raw_output_chars": len(exc.raw_output),
            }
            format_repairs.append(repair_record)
            if source_call.get("finish_reason") == "length":
                repair_record.update(
                    {
                        "status": "not_attempted",
                        "reason": "draft_output_truncated_at_completion_limit",
                    }
                )
                return model_failure("draft_output_truncated", exc)
            try:
                if progress:
                    progress(f"format_repair_attempt_{attempt_number}")
                plan = client.json_completion(
                    system=(
                        "You repair JSON syntax only. Preserve every field, string, array, "
                        "claim, quote, citation, and decision exactly as supplied. Return only "
                        "one syntactically valid JSON object. Do not add, remove, reinterpret, "
                        "or fact-check content."
                    ),
                    user="MALFORMED_JSON_TO_REPAIR:\n" + exc.raw_output,
                    max_tokens=MAX_DRAFT_OUTPUT_TOKENS,
                )
                repair_record.update(
                    {
                        "status": "repaired",
                        "repair_call": len(client.calls),
                    }
                )
            except Exception as repair_exc:
                repair_record.update(
                    {
                        "status": "failed",
                        "repair_call": len(client.calls),
                        "error": str(repair_exc)[:1000],
                    }
                )
                return model_failure("draft_format_repair", repair_exc)
        except Exception as exc:
            return model_failure("drafting", exc)
        deterministic = validate_draft_plan(
            plan,
            packet=packet,
            candidate_paths=candidate_paths,
            candidate_metadata={str(item["path"]): item for item in candidates},
            existing_paths=existing_paths,
            domain=domain,
            claim_policy=claim_policy,
        )
        if progress:
            progress(f"deterministic_validation_attempt_{attempt_number}")
        repeated_invalid_plan = bool(
            previous_plan and plan == previous_plan and not deterministic.ok
        )
        if repeated_invalid_plan:
            deterministic = ValidationResult(
                False,
                sorted({*deterministic.issues, "repeated_invalid_plan"}),
                deterministic.warnings,
            )
        if deterministic.ok and plan.get("decision") not in CHANGE_DECISIONS:
            critic = {
                "approved": False,
                "recommendation": "not_run",
                "issues": [],
                "mode": critic_mode,
                "skipped_reason": "non_append_decision",
            }
        elif not deterministic.ok:
            critic = {
                "approved": False,
                "recommendation": "not_run",
                "issues": [],
                "mode": critic_mode,
                "skipped_reason": "deterministic_plan_invalid",
            }
        else:
            try:
                if progress:
                    progress(f"critic_attempt_{attempt_number}")
                critic = review_plan_targets(
                    client=client,
                    packet=packet,
                    plan=plan,
                    candidates=candidates,
                    candidate_documents=candidate_documents,
                    deterministic_issues=deterministic.issues + deterministic.warnings,
                    critic_mode=critic_mode,
                    claim_policy=claim_policy,
                    new_article_seed=new_article_seed,
                )
            except Exception as exc:
                return model_failure("critic", exc)

        attempt_record = {
            "attempt": attempt_number,
            "plan": plan,
            "plan_validation": asdict(deterministic),
            "critic": critic,
        }
        attempt_history.append(attempt_record)
        if deterministic.ok and plan.get("decision") in CHANGE_DECISIONS:
            if best_valid_attempt is None or attempt_quality(attempt_record) < attempt_quality(best_valid_attempt):
                best_valid_attempt = attempt_record
        if deterministic.ok and plan.get("decision") not in CHANGE_DECISIONS:
            if best_valid_attempt is None or attempt_number >= max_draft_attempts:
                break
            # A retry that abandons an earlier deterministically valid plan for
            # needs_review is capitulation, not repair; ask for a rescoped plan.
            prior_issues = ["planner_abandoned_valid_plan"]
            previous_plan = plan
            previous_critic = critic
            continue
        if deterministic.ok and critic.get("approved") is True:
            break
        if repeated_invalid_plan:
            break
        prior_issues = deterministic.issues + [
            str(item.get("code") or item)
            for item in critic.get("issues", [])
        ]
        previous_plan = plan
        previous_critic = critic

    selected_attempt = best_valid_attempt or (attempt_history[-1] if attempt_history else None)
    if selected_attempt is not None:
        plan = selected_attempt["plan"]
        deterministic = ValidationResult(**selected_attempt["plan_validation"])
        critic = selected_attempt["critic"]

    # Bounded balance repair: a validated omitted-qualifier finding gets exactly
    # one constrained repair attempt before the run may proceed. On any repair
    # failure the pre-repair attempt is retained and the unresolved-finding gate
    # below downgrades the run to needs_review.
    balance_repair_record: dict[str, Any] | None = None
    if (
        critic_mode == "required"
        and plan.get("decision") in CHANGE_DECISIONS
        and deterministic.ok
        and not critic_blocks_publication(critic)
    ):
        balance_findings = unresolved_balance_findings(critic)
        if balance_findings:
            balance_repair_record = {
                "findings": balance_findings,
                "status": "not_attempted",
            }
            if not all(
                balance_finding_repairable(finding, packet)
                for finding in balance_findings
            ):
                balance_repair_record.update(
                    {
                        "status": "unrepairable",
                        "reason": "qualifier_not_verbatim_in_source",
                    }
                )
            else:
                if progress:
                    progress("balance_repair")
                repaired_plan: dict[str, Any] | None = None
                try:
                    repaired_plan = client.json_completion(
                        system=(
                            "You repair one validated publish plan. Apply only the "
                            "explicitly allowed qualifier additions and return only the "
                            "corrected JSON plan."
                        ),
                        user=balance_repair_prompt(
                            packet=packet,
                            plan=plan,
                            findings=balance_findings,
                            claim_policy=claim_policy,
                        ),
                        max_tokens=MAX_DRAFT_OUTPUT_TOKENS,
                    )
                except Exception as exc:
                    balance_repair_record.update(
                        {"status": "failed", "error": str(exc)[:1000]}
                    )
                if repaired_plan is not None:
                    repaired_deterministic = validate_draft_plan(
                        repaired_plan,
                        packet=packet,
                        candidate_paths=candidate_paths,
                        candidate_metadata={str(item["path"]): item for item in candidates},
                        existing_paths=existing_paths,
                        domain=domain,
                        claim_policy=claim_policy,
                    )
                    repaired_deterministic = waive_qualifier_entity_issues(
                        repaired_deterministic, repaired_plan, balance_findings
                    )
                    repair_scope = validate_balance_repair(
                        plan, repaired_plan, balance_findings, packet
                    )
                    repaired_critic: dict[str, Any] | None = None
                    if repaired_deterministic.ok and repair_scope.ok:
                        try:
                            repaired_critic = review_plan_targets(
                                client=client,
                                packet=packet,
                                plan=repaired_plan,
                                candidates=candidates,
                                candidate_documents=candidate_documents,
                                deterministic_issues=repaired_deterministic.issues
                                + repaired_deterministic.warnings,
                                critic_mode=critic_mode,
                                claim_policy=claim_policy,
                                new_article_seed=new_article_seed,
                            )
                        except Exception as exc:
                            balance_repair_record.update(
                                {"status": "failed", "error": str(exc)[:1000]}
                            )
                    repair_attempt = {
                        "attempt": len(attempt_history) + 1,
                        "balance_repair": True,
                        "plan": repaired_plan,
                        "plan_validation": asdict(repaired_deterministic),
                        "repair_scope_validation": asdict(repair_scope),
                        "critic": repaired_critic,
                    }
                    attempt_history.append(repair_attempt)
                    if (
                        repaired_critic is not None
                        and repaired_deterministic.ok
                        and repair_scope.ok
                        and not critic_blocks_publication(repaired_critic)
                        and not unresolved_balance_findings(repaired_critic)
                    ):
                        balance_repair_record.update(
                            {"status": "repaired", "attempt": repair_attempt["attempt"]}
                        )
                        plan = repaired_plan
                        deterministic = repaired_deterministic
                        critic = repaired_critic
                        selected_attempt = repair_attempt
                    elif balance_repair_record.get("status") != "failed":
                        balance_repair_record.update(
                            {
                                "status": "failed",
                                "reason": "repair_did_not_validate",
                            }
                        )

    report: dict[str, Any] = {
        "source": source,
        "packet": packet,
        "packet_validation": asdict(packet_validation),
        "candidates": candidates,
        "candidate_retrieval": candidate_retrieval,
        "source_prompt_context": prompt_source_packet(packet).get("prompt_context", {}),
        "domain": domain,
        "plan": plan,
        "plan_validation": asdict(deterministic),
        "critic": critic,
        "critic_mode": critic_mode,
        "claim_policy": claim_policy,
        "abstract_mode": abstract_mode,
        "publication_requested": publish,
        "publication_suppressed": publication_suppressed,
        "attempt_history": attempt_history,
        "format_repairs": format_repairs,
        "selected_attempt": selected_attempt.get("attempt") if selected_attempt else None,
        "best_deterministic_valid_attempt": best_valid_attempt,
        "balance_repair": balance_repair_record,
    }
    if progress:
        progress("validating")
    if plan.get("decision") not in CHANGE_DECISIONS:
        report["status"] = str(plan.get("decision") or "needs_review")
        if report["status"] == "needs_review":
            report["reason"] = str(plan.get("reason") or "model_needs_review")
            report["new_article_recommendation"] = (
                plan.get("new_article_recommendation")
                if isinstance(plan.get("new_article_recommendation"), dict)
                else new_article_recommendation(
                    packet,
                    reason="No model-proposed existing target passed entity and scope review.",
                )
            )
        return finish(report)
    critic_approved = critic.get("approved") is True
    # An override request is preserved for audit, but cannot bypass a failed
    # target critic when publication was requested.
    critic_override_applied = False
    report["critic_override"] = {
        "requested": allow_critic_rejection,
        "applied": critic_override_applied,
        "reason": override_reason.strip() if allow_critic_rejection else "",
        "bypasses_publication_gate": False,
    }
    if not deterministic.ok:
        report["status"] = "needs_review"
        report["reason"] = "deterministic_quality_gate_failed"
        report["new_article_recommendation"] = new_article_recommendation(
            packet,
            reason="No deterministic-valid existing-target plan remained after repairs.",
        )
        return finish(report)
    if critic_mode == "required" and critic_blocks_publication(critic):
        report["status"] = "needs_review"
        report["reason"] = "critic_quality_gate_failed"
        if critic_rejects_all_target_entities(critic):
            report["new_article_recommendation"] = new_article_recommendation(
                packet,
                reason=(
                    "Every proposed existing target received a grounded entity-placement "
                    "rejection; a human should decide whether a new scoped article is warranted."
                ),
                rejected_candidates=candidates,
            )
        return finish(report)
    # A validated balance/omitted-qualifier finding may never publish
    # unresolved: the repair pass either fixed it (and the critic re-approved
    # the repaired plan) or the run stops here for a human.
    if critic_mode == "required" and unresolved_balance_findings(critic):
        report["status"] = "needs_review"
        report["reason"] = "balance_finding_unresolved"
        return finish(report)
    if critic_mode == "required" and not critic_approved:
        report["critic_publication_note"] = (
            "published_with_review_findings: validated review-severity critic findings "
            "are recorded in the draft PR audit for the human reviewer; each finding is "
            "also persisted as a critic comment beside its bullet so the caveat survives merge"
        )

    if publication_enabled and progress:
        progress("publishing")
    runtime_root = tools_root / "runtime" / "local-publisher"
    worktree, branch = create_isolated_worktree(
        content_repo=content_repo,
        runtime_root=runtime_root,
        slug=slugify(str(packet.get("title") or slug)),
        base_ref=base_ref,
    )
    rendered_documents: dict[Path, str] = {}
    rendered_results: list[dict[str, Any]] = []
    target_relatives: list[Path] = []
    for target_index, proposal in enumerate(plan_target_proposals(plan)):
        target_relative = Path(str(proposal["target_path"]))
        target = (worktree / target_relative).resolve()
        operation = proposal_operation(plan, proposal)
        if worktree.resolve() not in target.parents:
            raise ValueError("Validated target escaped the isolated content worktree")
        if operation == "append_existing" and not target.is_file():
            raise ValueError("Validated existing target is missing from the isolated worktree")
        if operation == "create_new" and target.exists():
            raise ValueError("Validated new target already exists in the isolated worktree")
        original = rendered_documents.get(
            target,
            target.read_text(encoding="utf-8")
            if operation == "append_existing"
            else initial_new_article_markdown(proposal),
        )
        published_findings = [
            issue
            for issue in critic.get("issues") or []
            if isinstance(issue, dict) and issue.get("target_index") == target_index
        ]
        updated = apply_draft_plan(
            original,
            proposal,
            packet,
            claim_policy=claim_policy,
            abstract_mode=abstract_mode,
            critic_findings=published_findings,
        )
        rendered_validation = validate_rendered_markdown(
            original,
            updated,
            plan=proposal,
            packet=packet,
            claim_policy=claim_policy,
            critic_findings=published_findings,
        )
        rendered_results.append(
            {
                "target_index": target_index,
                "target_path": str(target_relative),
                **asdict(rendered_validation),
            }
        )
        if not rendered_validation.ok:
            report["rendered_validation"] = rendered_results
            report["status"] = "needs_review"
            report["reason"] = "rendered_markdown_gate_failed"
            return finish(report)
        rendered_documents[target] = updated
        if target_relative not in target_relatives:
            target_relatives.append(target_relative)
    for target, updated in rendered_documents.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(updated, encoding="utf-8")
    report["rendered_validation"] = rendered_results
    run_command(
        ["git", "add", "-N", "--", *[str(path) for path in target_relatives]],
        cwd=worktree,
    )
    diff = run_command(
        ["git", "diff", "--", *[str(path) for path in target_relatives]],
        cwd=worktree,
    )
    if not diff:
        raise RuntimeError("Draft produced no markdown change")
    diff_path.write_text(diff + "\n", encoding="utf-8")
    report.update(
        {
            "status": "validated_draft",
            "target_paths": [str(path) for path in target_relatives],
            "worktree": str(worktree),
            "branch": branch,
            "diff_path": str(diff_path),
        }
    )
    if len(target_relatives) == 1:
        report["target_path"] = str(target_relatives[0])

    if publication_enabled:
        run_command(
            ["git", "add", "--", *[str(path) for path in target_relatives]],
            cwd=worktree,
        )
        commit_title = truncate_at_word(f"Add research on {clean_source_title(packet)}", 72)
        run_command(["git", "commit", "-m", commit_title], cwd=worktree)
        commit_sha = run_command(["git", "rev-parse", "HEAD"], cwd=worktree)
        run_command(["git", "push", "-u", "origin", branch], cwd=worktree)
        gh = shutil.which("gh")
        if not gh:
            raise FileNotFoundError("gh CLI is required to publish a draft PR")
        critic_audit = format_critic_pr_audit(
            critic,
            override_applied=critic_override_applied,
            override_reason=override_reason.strip(),
        )
        gate_summary = format_gate_summary(
            packet=packet,
            packet_validation=asdict(packet_validation),
            duplicate=duplicate,
            plan=plan,
            deterministic=deterministic,
            rendered_results=rendered_results,
        )
        pr_body = (
            "Local research publisher update.\n\n"
            f"Source: {source}\n\n"
            "Changed:\n\n"
            + "\n".join(f"- `{path}`" for path in target_relatives)
            + "\n\n"
            f"{gate_summary}\n\n"
            f"Critic mode: `{critic_mode}`\n\n"
            f"Claim policy: `{claim_policy}`\n\n"
            f"{critic_audit}\n\n"
            "This pull request is intentionally a draft and is never auto-merged."
        )
        pr_url = run_command(
            [
                gh,
                "pr",
                "create",
                "--draft",
                "--base",
                "main",
                "--head",
                branch,
                "--title",
                commit_title,
                "--body",
                pr_body,
            ],
            cwd=worktree,
        )
        report.update({"status": "pr_open", "commit": commit_sha, "pr_url": pr_url})

    return finish(report)


def configured_client_values() -> tuple[str, str]:
    return (
        os.environ.get("LOCAL_LLM_BASE_URL", "http://127.0.0.1:8080/v1"),
        os.environ.get("LOCAL_LLM_MODEL", "qwen3.6-35b-a3b-q8_0-mtp"),
    )
