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


@dataclass
class ValidationResult:
    ok: bool
    issues: list[str]


def normalize_evidence(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def source_evidence(packet: dict[str, Any]) -> str:
    return "\n\n".join(
        str(packet.get(key) or "")
        for key in ("title", "abstract", "body_markdown")
    )


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
    return ValidationResult(not issues, sorted(set(issues)))


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
    target = str(plan.get("target_path") or "")
    if target not in candidate_paths:
        issues.append("target_not_in_candidates")
    heading = str(plan.get("heading") or "")
    parent_heading = str(plan.get("parent_heading") or "")
    if not re.match(r"^#{2,6}\s+\S", heading):
        issues.append("invalid_heading")
    if parent_heading and not re.match(r"^#{2,5}\s+\S", parent_heading):
        issues.append("invalid_parent_heading")
    source_kind = normalize_evidence(
        " ".join(
            str(packet.get(key) or "")
            for key in ("title", "abstract", "study_type")
        )
    )
    if re.search(r"\b(?:animal|animals|preclinical|in vivo)\b", source_kind):
        placement = normalize_evidence(f"{parent_heading} {heading}")
        if not re.search(r"\b(?:animal|animals|preclinical)\b", placement):
            issues.append("preclinical_heading_scope_missing")

    evidence = normalize_evidence(source_evidence(packet))
    bullets = plan.get("bullets")
    if not isinstance(bullets, list) or not 1 <= len(bullets) <= 8:
        issues.append("invalid_bullet_count")
        return ValidationResult(False, sorted(set(issues)))
    for index, item in enumerate(bullets):
        prefix = f"bullet_{index}"
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
        similarity = SequenceMatcher(
            None, normalize_evidence(text), normalize_evidence(quote)
        ).ratio()
        if similarity < 0.68:
            issues.append(f"{prefix}_not_near_verbatim")
        if scope not in ALLOWED_EVIDENCE_SCOPES:
            issues.append(f"{prefix}_invalid_evidence_scope")
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
        "target_path": "must exactly equal one candidate path",
        "parent_heading": "exact existing ##-##### heading when heading is new, else empty",
        "heading": "exact existing heading, or a new child heading",
        "study_type": "source study type",
        "bullets": [
            {
                "text": "one near-verbatim source-supported claim, without citation marker",
                "source_quote": "an exact contiguous quote from abstract or extracted content",
                "evidence_scope": "review_summary | human | animal | in_vitro | mechanistic",
            }
        ],
        "review_notes": [],
    }
    source = dict(packet)
    source["body_markdown"] = str(source.get("body_markdown") or "")[:50000]
    return "\n\n".join(
        [
            "Return only one JSON object matching this contract:\n" + json.dumps(contract, indent=2),
            "Rules: For Natural Healing use concise one-idea bullets as close to the source wording as possible. The bullet text should normally equal source_quote exactly, except that a list number or section label may be removed. Do not add citation markers; the renderer does that. Every source_quote must be copied exactly from SOURCE_PACKET. Preserve study limitations and never turn a review, animal, in-vitro, or mechanistic statement into a human treatment claim. For animal or preclinical evidence, the proposed heading must explicitly contain 'Animal Evidence', 'Animal Models', or 'Preclinical Evidence' so the surrounding Disease / Symptom Treatment section cannot imply human efficacy. Omit any source sentence that appears internally contradictory, corrupted, dangerously mistyped, or statistically misleading; do not silently repair it. Choose only a clearly appropriate existing content home. If uncertain, return needs_review. Do not create a new article.",
            "SOURCE_PACKET:\n" + json.dumps(source, indent=2),
            "MATCH_CANDIDATES:\n" + json.dumps(candidates, indent=2),
            "CANDIDATE_DOCUMENTS:\n" + json.dumps(candidate_documents, indent=2),
            "PREVIOUS_VALIDATION_ISSUES:\n" + json.dumps(prior_issues or []),
            "PREVIOUS_PLAN_TO_REVISE:\n" + json.dumps(previous_plan or {}, indent=2),
            "PREVIOUS_CRITIC_FEEDBACK:\n" + json.dumps(previous_critic or {}, indent=2),
        ]
    )


def critic_prompt(
    *,
    packet: dict[str, Any],
    plan: dict[str, Any],
    deterministic_issues: list[str],
) -> str:
    source = dict(packet)
    source["body_markdown"] = str(source.get("body_markdown") or "")[:50000]
    return "\n\n".join(
        [
            "Audit this proposed Natural Healing research update. Return only JSON: {\"approved\": boolean, \"issues\": [{\"code\": string, \"bullet_index\": integer_or_null, \"explanation\": string}], \"recommendation\": \"approve|revise|needs_review\"}.",
            "Reject unsupported claims, medical overclaims, study-type inflation, merged ideas, wrong target pages, and any claim whose source_quote does not support it. Treat the deterministic validator as authoritative for literal source containment and near-verbatim similarity; do not invent a near-verbatim issue when that validator reports none. Animal/preclinical findings may be included only when the heading explicitly scopes them as animal or preclinical evidence. If the source itself appears internally contradictory, statistically misleading, or dangerously mistyped, require omission of that claim rather than silently correcting it.",
            "SOURCE_PACKET:\n" + json.dumps(source, indent=2),
            "DRAFT_PLAN:\n" + json.dumps(plan, indent=2),
            "DETERMINISTIC_ISSUES:\n" + json.dumps(deterministic_issues),
        ]
    )


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

    output_dir.mkdir(parents=True, exist_ok=True)
    slug = slugify(Path(source).stem or "research") or "research"
    scrape_markdown = output_dir / f"{slug}-source.md"
    packet = scrape_source_packet(source, scrape_markdown)
    packet_validation = validate_source_packet(packet)
    if not packet_validation.ok:
        raise ValueError(f"Invalid source packet: {', '.join(packet_validation.issues)}")
    if progress:
        progress("matching")

    if publish:
        run_command(["git", "fetch", "origin", "main"], cwd=content_repo)
    base_snapshot_temp = materialize_git_ref(content_repo=content_repo, git_ref=base_ref)
    base_snapshot = Path(base_snapshot_temp.name)

    duplicate = check_duplicate_paper(
        argparse.Namespace(
            identifier=packet.get("reference_url") or packet.get("url") or source,
            limit=max_candidates,
            repo_root=str(base_snapshot),
        )
    )
    paper_result = duplicate.get("paper_result") or {}
    paper_state = str((paper_result.get("paper") or {}).get("workflow_state") or "")
    if duplicate.get("content_hit_count", 0) > 0 or paper_state in {
        "drafted", "committed", "pr_open", "merged"
    }:
        return {
            "status": "duplicate",
            "source": source,
            "reason": "content_reference" if duplicate.get("content_hit_count", 0) else f"paper_state:{paper_state}",
            "duplicate": duplicate,
            "packet": packet,
        }

    articles = load_articles(base_snapshot)
    candidates = match_research_packet(
        articles, packet, alert_name=alert_name, limit=max_candidates
    )
    if not candidates:
        return {
            "status": "needs_review",
            "reason": "no_content_candidates",
            "packet": packet,
        }
    candidate_documents: dict[str, str] = {}
    for candidate in candidates[:3]:
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
    critic: dict[str, Any] = {}
    prior_issues: list[str] = []
    previous_plan: dict[str, Any] = {}
    previous_critic: dict[str, Any] = {}
    candidate_paths = {str(item["path"]) for item in candidates}
    if max_draft_attempts < 1:
        raise ValueError("max_draft_attempts must be at least 1")
    for _attempt in range(max_draft_attempts):
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
        critic = client.json_completion(
            system="You are an independent evidence and medical-overclaim critic. Be strict and return only the requested JSON.",
            user=critic_prompt(
                packet=packet,
                plan=plan,
                deterministic_issues=deterministic.issues,
            ),
            max_tokens=2500,
        )
        critic_approved = critic.get("approved") is True and critic.get("recommendation") == "approve"
        if deterministic.ok and critic_approved:
            break
        prior_issues = deterministic.issues + [
            str(item.get("code") or item)
            for item in critic.get("issues", [])
        ]
        previous_plan = plan
        previous_critic = critic

    report: dict[str, Any] = {
        "source": source,
        "packet": packet,
        "packet_validation": asdict(packet_validation),
        "candidates": candidates,
        "plan": plan,
        "plan_validation": asdict(deterministic),
        "critic": critic,
    }
    if progress:
        progress("validating")
    report_path = output_dir / f"{slug}-local-publish-report.json"
    report["report_path"] = str(report_path)
    if plan.get("decision") != "append_existing":
        report["status"] = str(plan.get("decision") or "needs_review")
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report
    if not deterministic.ok or critic.get("approved") is not True or critic.get("recommendation") != "approve":
        report["status"] = "needs_review"
        report["reason"] = "quality_gate_failed"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report

    if publish and progress:
        progress("publishing")
    runtime_root = tools_root / "runtime" / "local-publisher"
    worktree, branch = create_isolated_worktree(
        content_repo=content_repo,
        runtime_root=runtime_root,
        slug=slugify(str(packet.get("title") or slug)),
        base_ref=base_ref,
    )
    target_relative = Path(str(plan["target_path"]))
    target = (worktree / target_relative).resolve()
    if worktree.resolve() not in target.parents or not target.is_file():
        raise ValueError("Validated target escaped the isolated content worktree")
    original = target.read_text(encoding="utf-8")
    updated = apply_draft_plan(original, plan, packet)
    rendered_validation = validate_rendered_markdown(
        original, updated, plan=plan, packet=packet
    )
    report["rendered_validation"] = asdict(rendered_validation)
    if not rendered_validation.ok:
        report["status"] = "needs_review"
        report["reason"] = "rendered_markdown_gate_failed"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report
    target.write_text(updated, encoding="utf-8")
    diff = run_command(["git", "diff", "--", str(target_relative)], cwd=worktree)
    if not diff:
        raise RuntimeError("Draft produced no markdown change")
    diff_path = output_dir / f"{slug}-proposed.patch"
    diff_path.write_text(diff + "\n", encoding="utf-8")
    report.update(
        {
            "status": "validated_draft",
            "target_path": str(target_relative),
            "worktree": str(worktree),
            "branch": branch,
            "diff_path": str(diff_path),
        }
    )

    if publish:
        run_command(["git", "add", "--", str(target_relative)], cwd=worktree)
        commit_title = f"Add research on {packet.get('title', 'source')}"[:72]
        run_command(["git", "commit", "-m", commit_title], cwd=worktree)
        commit_sha = run_command(["git", "rev-parse", "HEAD"], cwd=worktree)
        run_command(["git", "push", "-u", "origin", branch], cwd=worktree)
        gh = shutil.which("gh")
        if not gh:
            raise FileNotFoundError("gh CLI is required to publish a draft PR")
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
                f"Local research publisher update.\n\nSource: {source}\n\nChanged: `{target_relative}`\n\nThe deterministic evidence gate and local-model critic both approved this draft.",
            ],
            cwd=worktree,
        )
        report.update({"status": "pr_open", "commit": commit_sha, "pr_url": pr_url})

    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def configured_client_values() -> tuple[str, str]:
    return (
        os.environ.get("LOCAL_LLM_BASE_URL", "http://127.0.0.1:8080/v1"),
        os.environ.get("LOCAL_LLM_MODEL", "qwen3.6-35b-a3b-q8_0-mtp"),
    )
