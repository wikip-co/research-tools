from __future__ import annotations

import json
import io
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen


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
ALLOWED_DECISIONS = {"append_existing", "duplicate", "needs_review"}
ALLOWED_EVIDENCE_SCOPES = {
    "review_summary",
    "human",
    "animal",
    "in_vitro",
    "mechanistic",
}
DOI_PATTERN = re.compile(r"^10\.\d{4,9}/[-._;()/:a-z0-9]+$", re.IGNORECASE)
CRITIC_MODES = {"required", "advisory", "off"}
CRITIC_SEVERITIES = {"warning", "review", "blocking"}
CRITIC_SEVERITY_RANK = {"warning": 0, "review": 1, "blocking": 2}
PLACEMENT_ISSUE_CODES = {
    "wrong_target_page",
    "wrong_heading",
    "existing_content_conflict",
    "duplicate_content",
    "unsafe_context_inference",
}
EVIDENCE_ISSUE_CODES = {
    "unsupported_claim",
    "medical_overclaim",
    "study_type_inflation",
    "merged_ideas",
    "source_integrity_concern",
    "limitation_omitted",
}
MINIMUM_CRITIC_SEVERITY = {
    "wrong_target_page": "review",
    "wrong_heading": "review",
    "existing_content_conflict": "review",
    "duplicate_content": "review",
    "unsafe_context_inference": "review",
    "unsupported_claim": "review",
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


def normalize_evidence(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


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
    if plan.get("decision") == "append_existing" and plan.get("target_path"):
        return [plan]
    return []


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


class LocalLLMClient:
    def __init__(self, base_url: str, model: str, timeout_seconds: int = 600):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds

    def json_completion(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 5000,
    ) -> dict[str, Any]:
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
        return extract_json_object(content)


def validate_draft_plan(
    plan: dict[str, Any],
    *,
    packet: dict[str, Any],
    candidate_paths: set[str],
) -> ValidationResult:
    issues: list[str] = []
    decision = str(plan.get("decision") or "")
    if decision not in ALLOWED_DECISIONS:
        issues.append("invalid_decision")
    if decision != "append_existing":
        return ValidationResult(not issues, issues)
    raw_proposals = plan.get("target_proposals")
    if isinstance(raw_proposals, list):
        for target_index, proposal in enumerate(raw_proposals):
            if not isinstance(proposal, dict):
                issues.append(f"target_{target_index}_invalid")
    proposals = plan_target_proposals(plan)
    if not 1 <= len(proposals) <= 8:
        issues.append("invalid_target_proposal_count")
        return ValidationResult(False, sorted(set(issues)))
    new_contract = isinstance(plan.get("target_proposals"), list)
    if new_contract and not isinstance(plan.get("exclusions"), list):
        issues.append("invalid_exclusions")

    evidence = normalize_evidence(source_evidence(packet))
    assigned_quotes: dict[str, int] = {}
    seen_targets: set[str] = set()
    if new_contract and isinstance(plan.get("exclusions"), list):
        for exclusion_index, exclusion in enumerate(plan["exclusions"]):
            prefix = f"exclusion_{exclusion_index}"
            if not isinstance(exclusion, dict):
                issues.append(f"{prefix}_invalid")
                continue
            quote = normalize_evidence(str(exclusion.get("source_quote") or ""))
            if quote and quote not in evidence:
                issues.append(f"{prefix}_quote_not_in_source")
            if not str(exclusion.get("reason") or "").strip():
                issues.append(f"{prefix}_missing_reason")
    for target_index, proposal in enumerate(proposals):
        target_prefix = f"target_{target_index}"
        target = str(proposal.get("target_path") or "")
        if target not in candidate_paths:
            issues.append(f"{target_prefix}_not_in_candidates")
        if target in seen_targets:
            issues.append(f"{target_prefix}_duplicate_target")
        seen_targets.add(target)
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
                    quote = normalize_evidence(str(exclusion.get("source_quote") or ""))
                    if quote and quote not in evidence:
                        issues.append(f"{prefix}_quote_not_in_source")
                    if not str(exclusion.get("reason") or "").strip():
                        issues.append(f"{prefix}_missing_reason")

        preclinical = source_is_preclinical(packet)
        placement = normalize_evidence(f"{parent_heading} {heading}")
        scoped_preclinical_context = bool(
            re.search(
                r"\b(?:animal evidence|animal model|animal models|preclinical evidence|preclinical context)\b",
                placement,
            )
        )
        if preclinical and not scoped_preclinical_context:
            issues.append(f"{target_prefix}_preclinical_heading_scope_missing")

        bullets = proposal.get("bullets")
        if not isinstance(bullets, list) or not 1 <= len(bullets) <= 8:
            issues.append(f"{target_prefix}_invalid_bullet_count")
            continue
        for bullet_index, item in enumerate(bullets):
            prefix = f"{target_prefix}_bullet_{bullet_index}"
            if not isinstance(item, dict):
                issues.append(f"{prefix}_invalid")
                continue
            text = str(item.get("text") or "").strip()
            quote = str(item.get("source_quote") or "").strip()
            scope = str(item.get("evidence_scope") or "")
            if not text or "[^" in text or text.startswith("-"):
                issues.append(f"{prefix}_invalid_text")
            if len(normalize_evidence(quote)) < 35:
                issues.append(f"{prefix}_quote_too_short")
            elif normalize_evidence(quote) not in evidence:
                issues.append(f"{prefix}_quote_not_in_source")
            if word_token_similarity(text, quote) < 0.68:
                issues.append(f"{prefix}_not_near_verbatim")
            if scope not in ALLOWED_EVIDENCE_SCOPES:
                issues.append(f"{prefix}_invalid_evidence_scope")
            if preclinical and scope != "animal":
                issues.append(f"{prefix}_preclinical_scope_must_be_animal")
            normalized_quote = normalize_evidence(quote)
            if normalized_quote:
                previous_target = assigned_quotes.setdefault(normalized_quote, target_index)
                if previous_target != target_index:
                    issues.append(f"{prefix}_claim_assigned_to_multiple_targets")

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
    return ValidationResult(not issues, sorted(set(issues)))


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


def apply_draft_plan(markdown: str, plan: dict[str, Any], packet: dict[str, Any]) -> str:
    ref_num = next_footnote_number(markdown)
    bullets = "\n".join(
        f"- {str(item['text']).strip()}[^{ref_num}]" for item in plan["bullets"]
    )
    updated = insert_under_heading(
        markdown,
        heading=str(plan["heading"]).strip(),
        parent_heading=str(plan.get("parent_heading") or "").strip(),
        bullet_block=bullets,
    )
    return updated.rstrip() + "\n\n" + render_reference(packet, ref_num) + "\n"


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
) -> ValidationResult:
    issues: list[str] = []
    if frontmatter_block(original) != frontmatter_block(updated):
        issues.append("frontmatter_changed")
    ref_num = next_footnote_number(original)
    if f"[^{ref_num}]:" not in updated:
        issues.append("reference_missing")
    doi = str(packet.get("doi") or "").strip()
    source_url = str(packet.get("url") or packet.get("requested_url") or "").strip()
    if doi and doi not in updated:
        issues.append("doi_missing_from_reference")
    if source_url and source_url not in updated:
        issues.append("source_url_missing_from_reference")
    for index, item in enumerate(plan.get("bullets") or []):
        rendered = f"- {str(item.get('text') or '').strip()}[^{ref_num}]"
        if rendered not in updated:
            issues.append(f"bullet_{index}_citation_missing")
    added_lines = max(0, len(updated.splitlines()) - len(original.splitlines()))
    if added_lines > 160:
        issues.append("change_scope_too_large")
    return ValidationResult(not issues, sorted(set(issues)))


def run_command(args: list[str], *, cwd: Path) -> str:
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "command failed")
    return result.stdout.strip()


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
        archive.extractall(temporary.name)
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
) -> str:
    contract = {
        "decision": "append_existing | duplicate | needs_review",
        "study_type": "source study type",
        "target_proposals": [
            {
                "target_path": "must exactly equal one entity-compatible candidate path",
                "parent_heading": "exact existing ##-##### heading when heading is new, else empty",
                "heading": "exact existing heading, or a new child heading",
                "rationale": "why this entity and scope belong on this target",
                "bullets": [
                    {
                        "text": "one near-verbatim source-supported claim, without citation marker",
                        "source_quote": "an exact contiguous quote from abstract or extracted content",
                        "evidence_scope": "review_summary | human | animal | in_vitro | mechanistic",
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
        "new_article_recommendation": "null for append_existing; structured object only with needs_review",
        "review_notes": [],
    }
    source = dict(packet)
    source["body_markdown"] = str(source.get("body_markdown") or "")[:50000]
    repair_hints: list[str] = []
    issues = prior_issues or []
    if any(re.match(r"bullet_\d+_invalid_text$", issue) for issue in issues):
        repair_hints.append("For bullet text, remove every leading dash and every [^n] citation marker; the renderer adds both.")
    if any("preclinical_heading_scope_missing" in issue for issue in issues):
        repair_hints.append("Put 'Animal Evidence', 'Animal Models', or 'Preclinical Evidence' literally in the proposed heading.")
    if any("quote_not_in_source" in issue for issue in issues):
        repair_hints.append("Copy source_quote as one exact contiguous span from SOURCE_PACKET without ellipses or repairs.")
    if any("not_near_verbatim" in issue for issue in issues):
        repair_hints.append("Make bullet text equal source_quote except for removing a section/list label.")
    return "\n\n".join(
        [
            "Return only one JSON object matching this contract:\n" + json.dumps(contract, indent=2),
            "Rules: For Natural Healing use concise one-idea bullets as close to the source wording as possible. The bullet text should normally equal source_quote exactly, except that a list number or section label may be removed. Do not add citation markers; the renderer does that. Every source_quote must be copied exactly from SOURCE_PACKET. Preserve study limitations and never turn a review, animal, rat, mouse, in-vitro, or mechanistic statement into a human treatment claim. Every preclinical bullet must use evidence_scope animal. Under Disease / Symptom Treatment, the proposed heading must explicitly contain 'Animal Evidence', 'Animal Models', or 'Preclinical Evidence'. Omit any source sentence that appears internally contradictory, corrupted, dangerously mistyped, or statistically misleading; do not silently repair it. A source may support several existing entity pages. Create a separate target_proposal for each entity-compatible page, with its own rationale, claims, evidence scopes, and exclusions. Never reuse the same claim across targets. Do not place cultivar, blend, or isolated-compound claims on a broader botanical page unless the target already materially covers the exact studied entity. Explicitly list material unused claims in exclusions. If no existing page is entity- and scope-compatible, return needs_review with a structured new_article_recommendation; do not create an article.",
            "SOURCE_PACKET:\n" + json.dumps(source, indent=2),
            "MATCH_CANDIDATES:\n" + json.dumps(candidates, indent=2),
            "CANDIDATE_DOCUMENTS:\n" + json.dumps(candidate_documents, indent=2),
            "PREVIOUS_VALIDATION_ISSUES:\n" + json.dumps(prior_issues or []),
            "DETERMINISTIC_REPAIR_HINTS:\n" + json.dumps(repair_hints),
            "PREVIOUS_PLAN_TO_REVISE:\n" + json.dumps(previous_plan or {}, indent=2),
            "PREVIOUS_CRITIC_FEEDBACK:\n" + json.dumps(previous_critic or {}, indent=2),
        ]
    )


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
) -> str:
    source = dict(packet)
    source["body_markdown"] = str(source.get("body_markdown") or "")[:12000]
    return "\n\n".join(
        [
            "Review only target-page and heading placement. Return one JSON object matching this contract:\n"
            + json.dumps(critic_issue_contract(PLACEMENT_ISSUE_CODES), indent=2),
            "Use only the supplied source, selected candidate metadata, and selected target Markdown. Do not use outside botanical or medical knowledge. Check whether a multi-entity plan puts named-cultivar or isolated-compound claims onto a broader page that does not materially cover that entity; require the planner to restrict bullets to the selected target or return needs_review. Every issue must cite exact contiguous text from the source packet or selected target page; wrong_target_page must cite both. Use warning for useful non-gating notes, review for a human placement decision, and blocking only for a clearly unsafe placement. Return an empty issues list when there is no grounded objection. Deterministic checks are authoritative for candidate membership, headings, exact source containment, near-verbatim similarity, and required preclinical heading labels; never recommend removing a required Animal Evidence/Animal Models/Preclinical Evidence label.",
            "SOURCE_PACKET:\n" + json.dumps(source, indent=2),
            "DRAFT_PLAN:\n" + json.dumps(plan, indent=2),
            "SELECTED_CANDIDATE_METADATA:\n" + json.dumps(selected_candidate, indent=2),
            "SELECTED_TARGET_MARKDOWN:\n" + selected_target_markdown[:30000],
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
) -> str:
    source = dict(packet)
    source["body_markdown"] = str(source.get("body_markdown") or "")[:50000]
    return "\n\n".join(
        [
            "Review only evidence support, claim strength, study scope, one-idea bullets, source integrity, and material limitations. Return one JSON object matching this contract:\n"
            + json.dumps(critic_issue_contract(EVIDENCE_ISSUE_CODES), indent=2),
            "Every issue must cite an exact contiguous source_quote from SOURCE_PACKET; target_quote may additionally quote the selected page when surrounding context creates an overclaim. Do not object merely because a near-verbatim bullet does not repeat context already made explicit by its preclinical heading or evidence scope. Do not state that a claim is unsupported and then admit that the source supports it. Use no outside facts. Use warning for useful non-gating notes, review for ambiguity requiring human judgment, and blocking for a materially unsupported or unsafe claim. Return an empty issues list when there is no grounded objection. Deterministic checks are authoritative for exact source containment and near-verbatim similarity.",
            "SOURCE_PACKET:\n" + json.dumps(source, indent=2),
            "DRAFT_PLAN:\n" + json.dumps(plan, indent=2),
            "SELECTED_CANDIDATE_METADATA:\n" + json.dumps(selected_candidate, indent=2),
            "SELECTED_TARGET_MARKDOWN:\n" + selected_target_markdown[:30000],
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
        if code == "wrong_target_page" and (not source_quote or not target_quote):
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
        raw_placement = client.json_completion(
            system="You are an independent target-placement reviewer. Use only supplied evidence and return only the requested JSON.",
            user=placement_critic_prompt(
                packet=packet,
                plan=proposal,
                selected_candidate=selected_candidate,
                selected_target_markdown=selected_target_markdown,
                deterministic_issues=deterministic_issues,
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
    entity_codes = {"wrong_target_page", "unsafe_context_inference"}
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
    max_candidates: int = 5,
    max_draft_attempts: int = 3,
    critic_mode: str = "required",
    allow_critic_rejection: bool = False,
    override_reason: str = "",
    passive_worker: bool = False,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    # Imported lazily to keep the pure validators independently testable.
    from .cli import (
        check_duplicate_paper,
        load_articles,
        match_research_packet,
        scrape_source_packet,
        slugify,
    )
    import argparse

    if critic_mode not in CRITIC_MODES:
        raise ValueError(f"critic_mode must be one of: {', '.join(sorted(CRITIC_MODES))}")
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
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = slugify(Path(source).stem or "research") or "research"
    scrape_markdown = output_dir / f"{slug}-source.md"
    report_path = output_dir / f"{slug}-local-publish-report.json"
    packet = scrape_source_packet(source, scrape_markdown)
    packet_validation = validate_source_packet(packet)
    if not packet_validation.ok:
        report = {
            "source": source,
            "status": "needs_review",
            "reason": "invalid_source_packet",
            "packet": packet,
            "packet_validation": asdict(packet_validation),
            "critic_mode": critic_mode,
            "publication_requested": publish,
            "publication_suppressed": publication_suppressed,
            "report_path": str(report_path),
        }
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report
    if progress:
        progress("matching")

    publication_enabled = publish and critic_mode == "required"
    if publication_enabled:
        run_command(["git", "fetch", "origin", "main"], cwd=content_repo)
    base_snapshot_temp = materialize_git_ref(content_repo=content_repo, git_ref=base_ref)
    base_snapshot = Path(base_snapshot_temp.name)

    def finish(report: dict[str, Any]) -> dict[str, Any]:
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        base_snapshot_temp.cleanup()
        return report

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
            "critic_mode": critic_mode,
            "publication_requested": publish,
            "publication_suppressed": publication_suppressed,
            "report_path": str(report_path),
        }
        return finish(report)

    articles = load_articles(base_snapshot)
    candidates = match_research_packet(
        articles, packet, alert_name=alert_name, limit=max_candidates
    )
    if not candidates:
        report = {
            "status": "needs_review",
            "reason": "no_content_candidates",
            "packet": packet,
            "packet_validation": asdict(packet_validation),
            "critic_mode": critic_mode,
            "publication_requested": publish,
            "publication_suppressed": publication_suppressed,
            "new_article_recommendation": new_article_recommendation(
                packet,
                reason=(
                    "No existing page has a discriminative entity match for this source; "
                    "a human should decide whether a new scoped article is warranted."
                ),
            ),
            "report_path": str(report_path),
        }
        return finish(report)
    candidate_documents: dict[str, str] = {}
    for candidate in candidates:
        path = base_snapshot / candidate["path"]
        candidate_documents[candidate["path"]] = path.read_text(encoding="utf-8")[:30000]

    client = LocalLLMClient(base_url, model)
    if progress:
        progress("drafting")
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
    best_valid_attempt: dict[str, Any] | None = None
    candidate_paths = {str(item["path"]) for item in candidates}
    if max_draft_attempts < 1:
        raise ValueError("max_draft_attempts must be at least 1")
    for attempt_number in range(1, max_draft_attempts + 1):
        plan = client.json_completion(
            system=system,
            user=draft_prompt(
                packet=packet,
                candidates=candidates,
                candidate_documents=candidate_documents,
                prior_issues=prior_issues,
                previous_plan=previous_plan,
                previous_critic=previous_critic,
            ),
        )
        deterministic = validate_draft_plan(
            plan, packet=packet, candidate_paths=candidate_paths
        )
        if deterministic.ok and plan.get("decision") != "append_existing":
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
            critic = review_plan_targets(
                client=client,
                packet=packet,
                plan=plan,
                candidates=candidates,
                candidate_documents=candidate_documents,
                deterministic_issues=deterministic.issues,
                critic_mode=critic_mode,
            )

        attempt_record = {
            "attempt": attempt_number,
            "plan": plan,
            "plan_validation": asdict(deterministic),
            "critic": critic,
        }
        attempt_history.append(attempt_record)
        if deterministic.ok and plan.get("decision") == "append_existing":
            if best_valid_attempt is None or attempt_quality(attempt_record) < attempt_quality(best_valid_attempt):
                best_valid_attempt = attempt_record
        if deterministic.ok and plan.get("decision") != "append_existing":
            break
        if deterministic.ok and critic.get("approved") is True:
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
        "plan": plan,
        "plan_validation": asdict(deterministic),
        "critic": critic,
        "critic_mode": critic_mode,
        "publication_requested": publish,
        "publication_suppressed": publication_suppressed,
        "attempt_history": attempt_history,
        "selected_attempt": selected_attempt.get("attempt") if selected_attempt else None,
        "best_deterministic_valid_attempt": best_valid_attempt,
    }
    if progress:
        progress("validating")
    report["report_path"] = str(report_path)
    if plan.get("decision") != "append_existing":
        report["status"] = str(plan.get("decision") or "needs_review")
        if report["status"] == "needs_review":
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
        if worktree.resolve() not in target.parents or not target.is_file():
            raise ValueError("Validated target escaped the isolated content worktree")
        original = rendered_documents.get(target, target.read_text(encoding="utf-8"))
        updated = apply_draft_plan(original, proposal, packet)
        rendered_validation = validate_rendered_markdown(
            original, updated, plan=proposal, packet=packet
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
        target.write_text(updated, encoding="utf-8")
    report["rendered_validation"] = rendered_results
    diff = run_command(
        ["git", "diff", "--", *[str(path) for path in target_relatives]],
        cwd=worktree,
    )
    if not diff:
        raise RuntimeError("Draft produced no markdown change")
    diff_path = output_dir / f"{slug}-proposed.patch"
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
