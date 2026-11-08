"""Opt-in acquisition comparison; no article content is written to reports."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

import main as scraper
from content_agent_core.url_safety import public_urlopen, validate_public_url

BACKENDS = ("scrapling", "flaresolverr", "agent-browser")
USER_AGENT = "research-tools-evaluation/0.1"


def robots_policy(url: str) -> tuple[bool, float, str]:
    """Fail closed on unavailable policy; inspect rules for this and browser UAs."""
    try:
        parsed = urlsplit(validate_public_url(url))
        robots_url = urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))
        with public_urlopen(
            Request(robots_url, headers={"User-Agent": USER_AGENT}), timeout=15
        ) as response:
            text = response.read(1024 * 1024).decode("utf-8", "replace")
            if (
                response.headers.get_content_type() == "text/html"
                or len(text) >= 1024 * 1024
                or text.lstrip().lower().startswith(("<html", "<!doctype"))
            ):
                return False, 2.0, "robots_unavailable"
        policy = RobotFileParser()
        policy.parse(text.splitlines())
        agents = (USER_AGENT, "Mozilla/5.0", "*")
        allowed = all(policy.can_fetch(agent, url) for agent in agents)
        delay = max(float(policy.crawl_delay(agent) or 0) for agent in agents)
        return allowed, max(2.0, delay), "allowed" if allowed else "robots_disallowed"
    except HTTPError as exc:
        if exc.code == 404:
            return True, 2.0, "robots_not_found"
        return False, 2.0, "robots_unavailable"
    except (OSError, ValueError):
        return False, 2.0, "robots_unavailable"


def metrics(data: dict, case: dict) -> dict:
    issues = scraper.extraction_issues(data)
    expected_doi = case.get("expected_doi", "").lower()
    expected_title = case.get("expected_title", "")
    if (
        expected_doi
        and scraper.normalize_doi(data.get("doi", "")).lower() != expected_doi
    ):
        issues.append("expected_doi_mismatch")
    if (
        expected_title
        and scraper.title_match_score(data.get("title", ""), expected_title) < 0.6
    ):
        issues.append("expected_title_mismatch")
    abstract = scraper.normalize_whitespace(data.get("abstract", ""))
    body = scraper.normalize_whitespace(data.get("body_markdown", ""))
    content_ok = (
        len(abstract) >= scraper.MIN_ABSTRACT_CHARS
        or len(body) >= scraper.MIN_BODY_MARKDOWN_CHARS
    )
    valid = (
        content_ok
        and data.get("title") not in {None, "", "Unknown Title"}
        and not scraper.has_fatal_extraction_issue(data)
        and not any(issue.startswith("expected_") for issue in issues)
    )
    return {
        "retrieval_success": True,
        "valid_article": valid,
        "issues": issues,
        "score": scraper.extraction_score(data),
        "metadata_fields_present": sum(
            bool(data.get(key))
            for key in ("title", "doi", "authors", "journal", "pub_date")
        ),
        "metadata_fields_total": 5,
        "citation_metadata_issues": scraper.metadata_quality_issues(data),
        "abstract_chars": len(abstract),
        "body_chars": len(body),
        "abstract_complete_heuristic": len(abstract) >= scraper.MIN_ABSTRACT_CHARS
        and not scraper.is_truncated_text(abstract),
        "body_threshold_met": len(body) >= scraper.MIN_BODY_MARKDOWN_CHARS,
        "challenge_detected": bool(
            set(issues)
            & {
                "captcha_page",
                "robot_page",
                "challenge_page",
                "invalid_title",
                "bot_challenge",
            }
        ),
        "failure_class": None
        if valid
        else (issues[0] if issues else "insufficient_article_content"),
    }


def evaluate_one(case: dict, backend: str, fixture_root: Path | None = None) -> dict:
    started, cpu = time.perf_counter(), time.process_time()
    row = {"case": case["id"], "backend": backend}
    acquired = False
    try:
        source, _ = scraper.canonicalize_article_url(case["url"])
        if scraper.looks_like_pdf_source(source):
            # PDF processing is shared, not a browser-vs-browser comparison.
            return {**row, "skipped": "pdf_uses_shared_pipeline", "valid_article": None}
        if fixture_root is not None:
            fixture = case["fixtures"][backend]
            if fixture.startswith("error:"):
                raise RuntimeError(fixture)
            html = (fixture_root / fixture).read_text(encoding="utf-8")
            final_url = source
        else:
            fetch = {
                "scrapling": scraper.fetch_with_scrapling,
                "flaresolverr": scraper.fetch_with_flaresolverr,
                "agent-browser": scraper.fetch_with_agent_browser,
            }[backend]
            final_url, html = fetch(source)
        acquired = True
        fetch_seconds = time.perf_counter() - started
        data = scraper.extract_article_data(
            final_url, html, backend, enrich=False, requested_url=case["url"]
        )
        scraper.validate_requested_identity(data, case["url"])
        row.update(metrics(data, case))
        row["fetch_seconds"] = round(fetch_seconds, 4)
        row["extraction_seconds"] = round(
            time.perf_counter() - started - fetch_seconds, 4
        )
    except Exception as exc:  # noqa: BLE001 - acquisition errors become sanitized report rows
        # Errors may contain URLs, page text, cookies, or proxy credentials.
        # Retain only bounded classifications, never raw exception messages.
        message = str(exc).lower()
        failure = (
            "timeout"
            if "timeout" in message
            else "backend_unavailable"
            if any(
                term in message
                for term in (
                    "not installed",
                    "not configured",
                    "connection refused",
                    "executable doesn't exist",
                )
            )
            else "fetch_error"
            if not acquired
            else "extraction_error"
        )
        row.update(
            retrieval_success=acquired,
            valid_article=False,
            failure_class=failure,
            error_type=type(exc).__name__,
            issues=[],
        )
    row["duration_seconds"] = round(time.perf_counter() - started, 4)
    row["python_cpu_seconds"] = round(time.process_time() - cpu, 4)
    return row


def isolated_live_attempt(case: dict, backend: str, timeout: int) -> dict:
    # Hard wall-clock limit; kill the worker and its owned browser processes.
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        backend,
        json.dumps(case),
    ]
    with subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            stdout, _ = process.communicate(timeout=timeout)
            if process.returncode:
                raise RuntimeError("worker failed")
            return json.loads(stdout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            return {
                "case": case["id"],
                "backend": backend,
                "retrieval_success": False,
                "valid_article": False,
                "failure_class": "timeout",
                "duration_seconds": timeout,
            }

        except (RuntimeError, ValueError):
            return {
                "case": case["id"],
                "backend": backend,
                "retrieval_success": False,
                "valid_article": False,
                "failure_class": "worker_error",
            }


def summarize(rows: list[dict]) -> dict:
    summary = {}
    for backend in BACKENDS:
        attempts = [r for r in rows if r["backend"] == backend and not r.get("skipped")]
        summary[backend] = {
            "attempts": len(attempts),
            "valid_articles": sum(r["valid_article"] for r in attempts),
        }
    primary_failed = recovered = marginal_fs = 0
    for case in dict.fromkeys(row["case"] for row in rows):
        by_backend = {
            r["backend"]: r for r in rows if r["case"] == case and not r.get("skipped")
        }
        primary = by_backend.get("scrapling")
        if primary is not None and not primary["valid_article"]:
            primary_failed += 1
            recovered += any(
                r["valid_article"] for b, r in by_backend.items() if b != "scrapling"
            )
            marginal_fs += bool(by_backend.get("flaresolverr", {}).get("valid_article"))
    return {
        "backends": summary,
        "scrapling_failures": primary_failed,
        "fallback_recoveries": recovered,
        "fallback_recovery_rate": recovered / primary_failed
        if primary_failed
        else None,
        "flaresolverr_recoveries_over_scrapling": marginal_fs,
    }


def backend_versions(live: bool) -> dict:
    versions = {
        name: version(name) for name in ("scrapling", "playwright", "patchright")
    }
    versions["python"] = sys.version.split()[0]
    if live:
        binary = scraper.resolve_agent_browser_binary(scraper.AGENT_BROWSER_COMMAND)
        try:
            versions["agent-browser"] = (
                subprocess.run(
                    [binary, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=True,
                ).stdout.strip()
                if binary
                else "unavailable"
            )
        except (OSError, subprocess.SubprocessError):
            versions["agent-browser"] = "unknown"
        try:
            endpoint = urlsplit(scraper.FLARESOLVERR_URL)
            control_url = urlunsplit((endpoint.scheme, endpoint.netloc, "/", "", ""))
            with urlopen(control_url, timeout=5) as response:
                versions["flaresolverr"] = json.loads(response.read(1024 * 1024))[
                    "version"
                ]
        except (OSError, ValueError, KeyError):
            versions["flaresolverr"] = "unknown"
    return versions


def markdown_summary(report: dict) -> str:
    lines = [
        "# Retrieval evaluation",
        "",
        f"Mode: {report['mode']}. Scrapling: {report['scrapling_version']}.",
        "",
        "| Case | Backend | Valid article | Failure / skip | Seconds |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in report["results"]:
        lines.append(
            f"| {row['case']} | {row['backend']} | {row.get('valid_article')} | {row.get('skipped') or row.get('failure_class') or ''} | {row.get('duration_seconds', '')} |"
        )
    lines += [
        "",
        f"Fallback recoveries: {report['summary']['fallback_recoveries']} / {report['summary']['scrapling_failures']} primary failures.",
        "",
        "Thresholds estimate completeness; they do not prove full text. Metadata enrichment is disabled equally for every backend.",
        "CPU time covers the Python worker only; browser/daemon/container resources are excluded.",
        "Offline fixtures test evaluation logic and cannot establish backend effectiveness.",
        report["recommendation"],
        "",
    ]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Explicitly permit public network evaluation",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="JSON report; Markdown is written alongside it",
    )
    parser.add_argument("--backend", choices=BACKENDS, action="append")
    parser.add_argument(
        "--timeout", type=int, default=180, choices=range(10, 601), metavar="10..600"
    )
    args = parser.parse_args(argv)
    manifest_bytes = args.manifest.read_bytes()
    manifest = json.loads(manifest_bytes)
    if not args.live and manifest.get("mode") != "offline":
        parser.error("Network manifests require --live")
    if args.live and manifest.get("mode") != "live":
        parser.error("Use a live manifest with --live")
    cases = manifest["cases"]
    if len(cases) > 20:
        parser.error("Evaluation is limited to 20 explicit cases; no crawling")
    rows = []
    for case in cases:
        delay = 0
        if args.live:
            if not case.get("access_reviewed"):
                allowed, reason = False, "access_not_reviewed"
            else:
                allowed, delay, reason = robots_policy(
                    scraper.canonicalize_article_url(case["url"])[0]
                )
            if not allowed:
                rows.extend(
                    {
                        "case": case["id"],
                        "backend": backend,
                        "skipped": reason,
                        "valid_article": None,
                    }
                    for backend in args.backend or BACKENDS
                )
                continue
        for backend in args.backend or BACKENDS:
            if args.live:
                time.sleep(delay)
                row = isolated_live_attempt(case, backend, args.timeout)
            else:
                row = evaluate_one(case, backend, args.manifest.resolve().parent)
            rows.append(row)
    summary = summarize(rows)
    recommendation = (
        "Offline fixtures cannot support a FlareSolverr retirement decision."
        if not args.live
        else "Option A: retain FlareSolverr; valid recovery over Scrapling was observed."
        if summary["flaresolverr_recoveries_over_scrapling"]
        else "Option B: retain FlareSolverr as optional; no marginal recovery was observed and coverage is insufficient for retirement."
    )
    report = {
        "schema_version": 1,
        "mode": "live" if args.live else "offline",
        "evaluated_at": datetime.now(UTC).isoformat(),
        "scrapling_version": version("scrapling"),
        "runtime_versions": backend_versions(args.live),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "cases": [
            {
                key: case[key]
                for key in (
                    "id",
                    "url",
                    "expected_doi",
                    "expected_title",
                    "access_reviewed",
                    "note",
                )
                if key in case
            }
            for case in cases
        ],
        "metadata_enrichment": False,
        "results": rows,
        "summary": summary,
        "recommendation": recommendation,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    args.output.with_suffix(".md").write_text(markdown_summary(report))
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        print(json.dumps(evaluate_one(json.loads(sys.argv[3]), sys.argv[2])))
    else:
        raise SystemExit(main())
