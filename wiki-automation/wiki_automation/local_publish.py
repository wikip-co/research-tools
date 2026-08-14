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
}
DOI_PATTERN = re.compile(r"^10\.\d{4,9}/[-._;()/:a-z0-9]+$", re.IGNORECASE)
CRITIC_MODES = {"required", "advisory", "off"}
CLAIM_POLICIES = {"integrated", "strict", "compendium"}
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
        if target in seen_targets:
            issues.append(f"{target_prefix}_duplicate_target")
        seen_targets.add(target)
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
                if not lead_text:
                    issues.append(f"{target_prefix}_missing_lead_text")
                elif target_entity and not phrase_in_text(target_entity, lead_text):
                    issues.append(f"{target_prefix}_lead_missing_target_entity")
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
        if proposal_has_animal_claim(proposal, packet, claim_policy) and not has_preclinical_heading_scope(proposal):
            if includes_background_claims(claim_policy):
                warnings.append(f"{target_prefix}_preclinical_heading_scope_warning")
            else:
                issues.append(f"{target_prefix}_preclinical_heading_scope_missing")
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
            if word_token_similarity(text, quote) < 0.68:
                issues.append(f"{prefix}_not_near_verbatim")
            if scope not in ALLOWED_EVIDENCE_SCOPES:
                issues.append(f"{prefix}_invalid_evidence_scope")
            if target_entity and (
                not phrase_in_text(target_entity, quote)
                or not phrase_in_text(target_entity, text)
                or passage_is_mere_mention(target_entity, quote)
            ):
                issues.append(f"{prefix}_entity_not_supported_by_passage")
            if claim_policy == "strict" and source_is_preclinical(packet) and scope != "animal":
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
                    and scope != "animal"
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


def render_reference(packet: dict[str, Any], ref_num: int) -> str:
    title = str(packet.get("title") or "Untitled Source")
    url = str(packet.get("reference_url") or packet.get("url") or packet.get("requested_url") or "")
    source_url = str(packet.get("url") or packet.get("requested_url") or url)
    journal = str(packet.get("journal") or "Source")
    pub_date = str(packet.get("pub_date") or "Unknown")
    study_type = str(packet.get("study_type") or "Research Article")
    authors = str(packet.get("authors") or "Unknown")
    abstract = str(packet.get("abstract") or "").strip()
    lines = [
        f"[^{ref_num}]: **Title:** [{title}]({url})<br>",
        f"**Publication:** [{journal}]({source_url})<br>",
        f"**Date:** {pub_date}<br>",
        f"**Study Type:** {study_type}<br>",
        f"**Author(s):** {authors}<br>",
    ]
    if abstract:
        lines.append(f"**Abstract:** {abstract}<br>")
    doi = str(packet.get("doi") or "").strip()
    if doi:
        lines.append(f"**DOI:** [{doi}](https://doi.org/{doi})<br>")
    lines.append(f"**Source URL:** [{source_url}]({source_url})")
    return "\n".join(lines)


def bullet_reference_numbers(
    markdown: str,
    plan: dict[str, Any],
    claim_policy: str,
) -> list[int]:
    """Share one paper reference for findings and isolate background provenance."""
    bullets = plan.get("bullets") if isinstance(plan.get("bullets"), list) else []
    first_ref = next_footnote_number(markdown)
    background_indexes = {
        index
        for index, item in enumerate(bullets)
        if includes_background_claims(claim_policy)
        and isinstance(item, dict)
        and item.get("claim_kind") == "background_fact"
    }
    has_source_findings = len(background_indexes) < len(bullets)
    source_ref = first_ref if has_source_findings else None
    next_background_ref = first_ref + (1 if has_source_findings else 0)
    numbers: list[int] = []
    for index in range(len(bullets)):
        if index in background_indexes:
            numbers.append(next_background_ref)
            next_background_ref += 1
        else:
            numbers.append(source_ref or first_ref)
    return numbers


def provenance_inline(value: Any) -> str:
    return " ".join(str(value or "").split()).replace("<", "&lt;").replace(">", "&gt;")


def render_background_reference(packet: dict[str, Any], ref_num: int, item: dict[str, Any]) -> str:
    lines = [render_reference(packet, ref_num) + "<br>"]
    lines.append("**Claim Type:** Background fact summarized by the supplied paper<br>")
    lines.append(f"**Source Section:** {provenance_inline(item.get('source_section'))}<br>")
    lines.append(f"**Source Passage:** {provenance_inline(item.get('source_quote'))}<br>")
    cited_references = bullet_cited_references(item)
    if cited_references:
        earlier_references = []
        for cited_reference in cited_references:
            marker = provenance_inline(cited_reference.get("citation_marker"))
            reference_text = provenance_inline(cited_reference.get("reference_text"))
            reference_url = str(cited_reference.get("reference_url") or "").strip()
            if reference_url:
                reference_text = f"[{reference_text}]({reference_url})"
            earlier = f"{marker}: {reference_text}" if marker else reference_text
            earlier_references.append(earlier)
        lines.append(
            "**Earlier Work Cited in Passage:** " + "; ".join(earlier_references)
        )
    else:
        lines.append("**Earlier Work Cited in Passage:** None stated in the passage")
    return "\n".join(lines)


def compendium_evidence_warning(plan: dict[str, Any], packet: dict[str, Any], claim_policy: str) -> str:
    if (
        includes_background_claims(claim_policy)
        and proposal_has_animal_claim(plan, packet, claim_policy)
        and not has_preclinical_heading_scope(plan)
    ):
        return (
            "> **Evidence warning — animal/preclinical evidence:** These findings do not "
            "by themselves establish effects in humans."
        )
    return ""


def apply_draft_plan(
    markdown: str,
    plan: dict[str, Any],
    packet: dict[str, Any],
    *,
    claim_policy: str = "strict",
) -> str:
    ref_numbers = bullet_reference_numbers(markdown, plan, claim_policy)
    bullet_lines = [
        f"- {str(item['text']).strip()}[^{ref_numbers[index]}]"
        for index, item in enumerate(plan["bullets"])
    ]
    warning = compendium_evidence_warning(plan, packet, claim_policy)
    bullets = "\n".join(([warning, ""] if warning else []) + bullet_lines)
    updated = insert_under_heading(
        markdown,
        heading=str(plan["heading"]).strip(),
        parent_heading=str(plan.get("parent_heading") or "").strip(),
        bullet_block=bullets,
    )
    references: list[str] = []
    rendered_numbers: set[int] = set()
    for index, item in enumerate(plan["bullets"]):
        ref_num = ref_numbers[index]
        if ref_num in rendered_numbers:
            continue
        rendered_numbers.add(ref_num)
        if includes_background_claims(claim_policy) and item.get("claim_kind") == "background_fact":
            references.append(render_background_reference(packet, ref_num, item))
        else:
            references.append(render_reference(packet, ref_num))
    return updated.rstrip() + "\n\n" + "\n\n".join(references) + "\n"


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
    return f"---\n{frontmatter}\n---\n\n{emphasized}[^1]\n\n{heading}\n"


def frontmatter_block(markdown: str) -> str:
    if not markdown.startswith("---\n"):
        return ""
    end = markdown.find("\n---\n", 4)
    return markdown[: end + 5] if end >= 0 else ""


def validate_rendered_markdown(
    original: str,
    updated: str,
    *,
    plan: dict[str, Any],
    packet: dict[str, Any],
    claim_policy: str = "strict",
) -> ValidationResult:
    issues: list[str] = []
    if frontmatter_block(original) != frontmatter_block(updated):
        issues.append("frontmatter_changed")
    ref_numbers = bullet_reference_numbers(original, plan, claim_policy)
    for ref_num in sorted(set(ref_numbers)):
        if f"[^{ref_num}]:" not in updated:
            issues.append(f"reference_{ref_num}_missing")
    doi = str(packet.get("doi") or "").strip()
    source_url = str(packet.get("url") or packet.get("requested_url") or "").strip()
    if doi and doi not in updated:
        issues.append("doi_missing_from_reference")
    if source_url and source_url not in updated:
        issues.append("source_url_missing_from_reference")
    for index, item in enumerate(plan.get("bullets") or []):
        rendered = f"- {str(item.get('text') or '').strip()}[^{ref_numbers[index]}]"
        if rendered not in updated:
            issues.append(f"bullet_{index}_citation_missing")
        if includes_background_claims(claim_policy) and item.get("claim_kind") == "background_fact":
            passage = provenance_inline(item.get("source_quote"))
            if passage not in updated:
                issues.append(f"bullet_{index}_background_passage_missing")
            for cited_reference in bullet_cited_references(item):
                reference_text = provenance_inline(cited_reference.get("reference_text"))
                if reference_text not in updated:
                    issues.append(f"bullet_{index}_cited_reference_missing")
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
                    "lead_text": "one near-verbatim source-grounded definition or description",
                    "lead_source_quote": "exact contiguous source passage supporting lead_text",
                    "category_rationale": "why this existing or new sub-category is appropriate",
                },
                "parent_heading": "exact existing ##-##### heading when heading is new, else empty",
                "heading": "exact existing heading, or a new child heading",
                "rationale": "why this entity and scope belong on this target",
                "bullets": [
                    {
                        "text": "one near-verbatim source-supported claim, without citation marker",
                        "source_quote": "an exact contiguous quote from abstract or extracted content",
                        "source_section": "exact source section heading, such as Abstract, Introduction, Results, or Discussion",
                        "claim_kind": "source_finding | background_fact",
                        "evidence_scope": "review_summary | human | animal | in_vitro | mechanistic",
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
            "Use a target only when its primary entity is asserted by every exact source_quote; a mention is insufficient."
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
            "Rules: For Natural Healing use concise one-idea bullets as close to the source wording as possible. The bullet text should normally equal source_quote exactly, except that a list number, section label, or citation marker may be removed. Do not add footnote markers; the renderer adds them. Every source_quote must be copied exactly from the normalized source sections. Preserve study limitations and never turn a review, animal, rat, mouse, in-vitro, or mechanistic statement into a human treatment claim. Omit internally contradictory, corrupted, dangerously mistyped, or statistically misleading source sentences. One plan may append to several compatible pages and create one or more missing entity pages. Use operation append_existing only for a supplied candidate. Use create_new when the source's primary entity has no suitable page, including a useful category-level catch-all such as DOMAIN/Fruits/Citrus/citrus.md; choose an existing category directory when appropriate and create a new sub-category only when its semantic scope is clear. When MISSING_ENTITY_PAGE_SEED is nonempty and direct source findings explicitly concern that entity, do not discard those primary findings in favor of component-only background updates: create the seeded catch-all page and keep formulation/cultivar scope explicit in each bullet. Every target_entity must match the target title/stem and be asserted by every quote assigned to that target; a mere mention is insufficient. Never reuse the same claim across targets. Do not place cultivar, blend, or isolated-compound findings on a broader page unless the exact passage supports the broader entity. A citrus blend never belongs on a Bergamot page. A new page must include at least one direct source_finding, a source-grounded lead, focused tags including the exact target entity/title, a category rationale, and a safe path under DOMAIN. Its lead_text must normally equal lead_source_quote except for harmless Markdown removal; never generalize an intervention-specific quote into a broad definition or health claim. For cited_references, never invent a DOI or URL and never use placeholder reference text; remove the background claim if its exact reference record is unavailable. Explicitly list material unused claims in exclusions. Return needs_review only when neither guarded existing-page updates nor a well-scoped new page can be proposed safely.",
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
) -> str:
    source = prompt_source_packet(packet)
    return "\n\n".join(
        [
            "Review only target-page and heading placement. Return one JSON object matching this contract:\n"
            + json.dumps(critic_issue_contract(PLACEMENT_ISSUE_CODES), indent=2),
            f"CLAIM_POLICY: {claim_policy}",
            "Use only the supplied source, selected candidate metadata, proposed new-article metadata, and selected target Markdown. Do not use outside botanical or medical knowledge. Each bullet's exact source_quote must assert the target_entity; an entity merely mentioned without support for the claim is entity_not_supported. A target_entity must match the target title/path identity. A citrus blend must never be placed on a Bergamot page. For append_existing, check the page and heading; wrong_target_page must quote both source and target. For create_new, check whether a distinct page is warranted and whether its domain/category/path scope is appropriate; new_article_not_warranted or wrong_category requires an exact source quote but no target quote because the page does not exist. Check whether named-cultivar or isolated-compound claims are being put onto a broader target without direct passage support. Use warning for useful non-gating notes, review for human placement judgment, and blocking for clearly unsafe placement. Return an empty issues list when there is no grounded objection. Deterministic checks are authoritative for path safety, candidate membership, exact source containment, near-verbatim similarity, and provenance. In integrated policy, an ordinary heading is acceptable for animal evidence only because the renderer adds a mandatory warning.",
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
            "Every issue must cite an exact contiguous source_quote from SOURCE_PACKET; target_quote may additionally quote the selected page when surrounding context creates an overclaim. Verify that source_finding means a direct finding of the supplied paper and background_fact means a statement the paper summarizes. For background_fact, require its source section and preserve every earlier citation marker plus the exact supplied reference entry; missing or invented provenance is missing_source_provenance. Check the evidence_scope against the evidence described by that individual passage. Do not object merely because a near-verbatim bullet does not repeat context already made explicit by its preclinical heading, evidence warning, or evidence scope. Do not state that a claim is unsupported and then admit that the source supports it. Use no outside facts. Use warning for useful non-gating notes, review for ambiguity requiring human judgment, and blocking for a materially unsupported, misattributed, or unsafe claim. Return an empty issues list when there is no grounded objection. Deterministic checks are authoritative for exact source containment, provenance, and near-verbatim similarity.",
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
    recommendation = "approve" if approved else "needs_review" if any(
        issue.get("severity") == "blocking" for issue in actionable
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
        if proposal_operation(plan, proposal) == "create_new":
            selected_candidate = {
                "operation": "create_new",
                "path": selected_path,
                "title": str(((proposal.get("new_article") or {}).get("title") or "")),
                "domain": PurePosixPath(selected_path).parts[0] if selected_path else "",
                "new_article": proposal.get("new_article") or {},
            }
        raw_placement = client.json_completion(
            system="You are an independent target-placement reviewer. Use only supplied evidence and return only the requested JSON.",
            user=placement_critic_prompt(
                packet=packet,
                plan=proposal,
                selected_candidate=selected_candidate,
                selected_target_markdown=selected_target_markdown,
                deterministic_issues=deterministic_issues,
                claim_policy=claim_policy,
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
    return {
        "approved": approved,
        "recommendation": "approve" if approved else "needs_review",
        "issues": all_issues,
        "mode": critic_mode,
        "target_reviews": target_reviews,
        "validation": {
            "all_targets_reviewed": len(target_reviews) == len(plan_target_proposals(plan)),
            "target_review_count": len(target_reviews),
            "decision_derived_from_validated_issues": True,
        },
    }


def attempt_quality(attempt: dict[str, Any]) -> tuple[int, int, int]:
    """Sort deterministic-valid attempts by critic outcome, deterministically."""
    critic = attempt.get("critic") if isinstance(attempt.get("critic"), dict) else {}
    issues = critic.get("issues") if isinstance(critic.get("issues"), list) else []
    actionable = sum(
        1
        for issue in issues
        if isinstance(issue, dict) and issue.get("severity") in {"review", "blocking"}
    )
    return (0 if critic.get("approved") is True else 1, actionable, int(attempt.get("attempt") or 0))


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
            break
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
        "publication_requested": publish,
        "publication_suppressed": publication_suppressed,
        "attempt_history": attempt_history,
        "format_repairs": format_repairs,
        "selected_attempt": selected_attempt.get("attempt") if selected_attempt else None,
        "best_deterministic_valid_attempt": best_valid_attempt,
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
    if critic_mode == "required" and not critic_approved:
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
        updated = apply_draft_plan(
            original, proposal, packet, claim_policy=claim_policy
        )
        rendered_validation = validate_rendered_markdown(
            original,
            updated,
            plan=proposal,
            packet=packet,
            claim_policy=claim_policy,
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
        commit_title = f"Add research on {packet.get('title', 'source')}"[:72]
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
        pr_body = (
            "Local research publisher update.\n\n"
            f"Source: {source}\n\n"
            "Changed:\n\n"
            + "\n".join(f"- `{path}`" for path in target_relatives)
            + "\n\n"
            "The deterministic packet, citation metadata, exact-quotation, near-verbatim, and rendered-Markdown gates passed.\n\n"
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
