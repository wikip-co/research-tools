from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main

FIXTURES = Path(__file__).parent / "fixtures"
URL = "https://example.org/article"


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        for name, value in (
            ("validate_public_url", lambda url: url),
            ("resolve_agent_browser_binary", lambda command: "/fake/agent-browser"),
            ("enrich_metadata", lambda data: data),
            ("AGENT_BROWSER_FORCE_DOMAINS", ()),
            ("FLARESOLVERR_URL", "http://127.0.0.1:8191/v1"),
            ("SCRAPLING_MODE_DEFAULT", "auto"),
            ("FLARESOLVERR_MODE_DEFAULT", "auto"),
            ("AGENT_BROWSER_MODE_DEFAULT", "auto"),
        ):
            p = patch.object(main, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.html = (FIXTURES / "complete_article.html").read_text()
        self.valid = main.extract_article_data(URL, self.html, "scrapling")
        self.challenge = main.extract_article_data(
            URL, (FIXTURES / "challenge.html").read_text(), "scrapling"
        )

    def scrape(self, **kwargs):
        return main.scrape_article(
            URL,
            scrapling_mode="auto",
            flaresolverr_mode="auto",
            agent_browser_mode="auto",
            **kwargs,
        )

    def test_good_primary_stops_fallbacks(self):
        with (
            patch.object(main, "scrape_with_scrapling", return_value=self.valid),
            patch.object(main, "fetch_with_flaresolverr") as fs,
            patch.object(main, "fetch_with_agent_browser") as ab,
        ):
            self.assertEqual(self.scrape()["retrieval_backend"], "scrapling")
        fs.assert_not_called()
        ab.assert_not_called()

    def test_primary_error_recovers_with_flaresolverr(self):
        with (
            patch.object(
                main, "scrape_with_scrapling", side_effect=RuntimeError("unavailable")
            ),
            patch.object(
                main, "fetch_with_flaresolverr", return_value=(URL, self.html)
            ),
            patch.object(main, "fetch_with_agent_browser") as ab,
        ):
            result = self.scrape()
        self.assertEqual(result["retrieval_backend"], "flaresolverr")
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["fallback_trigger"], ["primary_fetch_failed"])
        ab.assert_not_called()

    def test_challenge_is_rejected_and_agent_browser_recovers(self):
        with (
            patch.object(main, "scrape_with_scrapling", return_value=self.challenge),
            patch.object(
                main,
                "fetch_with_flaresolverr",
                return_value=(URL, (FIXTURES / "challenge.html").read_text()),
            ),
            patch.object(
                main, "fetch_with_agent_browser", return_value=(URL, self.html)
            ),
        ):
            result = self.scrape()
        self.assertEqual(result["retrieval_backend"], "agent-browser")
        self.assertIn("invalid_title", result["fallback_trigger"])

    def test_flaresolverr_off(self):
        with (
            patch.object(
                main, "scrape_with_scrapling", side_effect=RuntimeError("failed")
            ),
            patch.object(main, "fetch_with_flaresolverr") as fs,
            patch.object(
                main, "fetch_with_agent_browser", return_value=(URL, self.html)
            ),
        ):
            result = main.scrape_article(URL, flaresolverr_mode="off")
        self.assertEqual(result["retrieval_backend"], "agent-browser")
        fs.assert_not_called()

    def test_unavailable_agent_browser_retains_partial_content(self):
        weak = deepcopy(self.valid)
        weak.update(abstract="short", body_markdown="short")
        with (
            patch.object(main, "scrape_with_scrapling", return_value=weak),
            patch.object(
                main, "fetch_with_flaresolverr", side_effect=RuntimeError("failed")
            ),
            patch.object(main, "resolve_agent_browser_binary", return_value=None),
        ):
            result = self.scrape()
        self.assertEqual(result["retrieval_backend"], "scrapling")
        self.assertIn("agent-browser: not installed", result["retrieval_errors"])

    def test_force_selects_only_named_backend(self):
        with (
            patch.object(main, "scrape_with_scrapling") as primary,
            patch.object(main, "fetch_with_flaresolverr") as fs,
            patch.object(
                main, "fetch_with_agent_browser", return_value=(URL, self.html)
            ),
        ):
            result = main.scrape_article(URL, agent_browser_mode="force")
        self.assertEqual(result["retrieval_backend"], "agent-browser")
        primary.assert_not_called()
        fs.assert_not_called()

    def test_scrapling_can_be_disabled_and_forced(self):
        with (
            patch.object(
                main, "scrape_with_scrapling", return_value=self.valid
            ) as primary,
            patch.object(
                main, "fetch_with_flaresolverr", return_value=(URL, self.html)
            ) as fs,
        ):
            self.assertEqual(
                main.scrape_article(URL, scrapling_mode="off")["retrieval_backend"],
                "flaresolverr",
            )
            primary.assert_not_called()
            fs.reset_mock()
            self.assertEqual(
                main.scrape_article(URL, scrapling_mode="force")["retrieval_backend"],
                "scrapling",
            )
            fs.assert_not_called()

    def test_explicit_off_overrides_domain_force(self):
        with (
            patch.object(main, "AGENT_BROWSER_FORCE_DOMAINS", ("example.org",)),
            patch.object(main, "scrape_with_scrapling", return_value=self.valid),
            patch.object(main, "fetch_with_agent_browser") as ab,
        ):
            result = main.scrape_article(URL, agent_browser_mode="off")
        self.assertEqual(result["retrieval_backend"], "scrapling")
        ab.assert_not_called()

    def test_multiple_forces_compare_quality(self):
        with (
            patch.object(main, "scrape_with_scrapling", return_value=self.valid),
            patch.object(
                main,
                "fetch_with_flaresolverr",
                return_value=(URL, (FIXTURES / "article.html").read_text()),
            ),
        ):
            result = main.scrape_article(
                URL,
                scrapling_mode="force",
                flaresolverr_mode="force",
                agent_browser_mode="off",
            )
        self.assertEqual(result["retrieval_backend"], "scrapling")

    def test_valid_content_outranks_larger_invalid_packet(self):
        long_bad = deepcopy(self.challenge)
        long_bad["body_markdown"] *= 200
        self.assertGreater(
            main.retrieval_rank(self.valid), main.retrieval_rank(long_bad)
        )

    def test_quality_issues_outrank_raw_length(self):
        weak = deepcopy(self.valid)
        weak.update(title="Unknown Title", body_markdown="body " * 2000)
        self.assertGreater(main.retrieval_rank(self.valid), main.retrieval_rank(weak))

    def test_pdf_bypasses_all_html_backends(self):
        with (
            patch.object(
                main, "scrape_pdf_source", return_value={"source_kind": "pdf"}
            ),
            patch.object(main, "scrape_with_scrapling") as primary,
            patch.object(main, "fetch_with_flaresolverr") as fs,
        ):
            self.assertEqual(
                main.scrape_article("/tmp/paper.pdf")["source_kind"], "pdf"
            )
        primary.assert_not_called()
        fs.assert_not_called()

    def test_all_invalid_raise(self):
        with (
            patch.object(main, "scrape_with_scrapling", return_value=self.challenge),
            patch.object(main, "fetch_with_flaresolverr", return_value=(URL, "")),
            patch.object(main, "fetch_with_agent_browser", return_value=(URL, "")),
            self.assertRaisesRegex(RuntimeError, "invalid publisher/interstitial"),
        ):
            self.scrape()

    def test_empty_document_is_fatal(self):
        self.assertTrue(
            main.has_fatal_extraction_issue(
                {"title": "Unknown Title", "body_markdown": "", "abstract": ""}
            )
        )

    def test_metadata_only_article_can_recover_abstract(self):
        def enrich(data):
            data["abstract"] = "A complete scientific abstract. " * 10

        html = '<html><head><meta name="citation_title" content="A scientific article"><meta name="citation_doi" content="10.1000/example"></head><body></body></html>'
        with patch.object(main, "enrich_metadata", side_effect=enrich) as enrichment:
            data = main.extract_article_data(URL, html, "fixture")
        enrichment.assert_called_once()
        self.assertFalse(main.has_fatal_extraction_issue(data))

    def test_requested_doi_rejects_wrong_redirected_article(self):
        result = main.validate_requested_identity(
            self.valid, "https://doi.org/10.1000/different"
        )
        self.assertIn("doi_url_mismatch", result["retrieval_issues"])
        self.assertTrue(main.has_fatal_extraction_issue(result))

    def test_disabled_backends_do_not_run(self):
        with (
            patch.object(main, "scrape_with_scrapling") as primary,
            self.assertRaisesRegex(RuntimeError, "No retrieval path"),
        ):
            main.scrape_article(
                URL,
                scrapling_mode="off",
                flaresolverr_mode="off",
                agent_browser_mode="off",
            )
        primary.assert_not_called()

    def test_scrapling_api_and_redirect_url(self):
        page = SimpleNamespace(url=URL + "/final", html_content=self.html)
        with patch.object(main.StealthyFetcher, "fetch", return_value=page) as fetch:
            data = main.scrape_with_scrapling(URL)
        self.assertEqual(data["url"], URL + "/final")
        fetch.assert_called_once_with(
            URL,
            headless=True,
            network_idle=True,
            disable_resources=False,
            timeout=main.SCRAPLING_TIMEOUT_MS,
            solve_cloudflare=False,
            google_search=False,
            page_setup=main._guard_browser_requests,
            additional_args={"service_workers": "block"},
        )

    def test_doi_resolver_identity_survives_redirect_without_metadata(self):
        html = self.html.replace(
            '<meta name="citation_doi" content="10.1000/example-doi" />', ""
        )
        with patch.object(main, "fetch_with_scrapling", return_value=(URL, html)):
            data = main.scrape_with_scrapling("https://doi.org/10.1000/example-doi")
        self.assertEqual(data["doi"], "10.1000/example-doi")
        self.assertEqual(data["url"], URL)
        self.assertEqual(data["requested_url"], "https://doi.org/10.1000/example-doi")

    def test_agent_browser_always_closes_session(self):

        def run(_binary, _session, args, **kwargs):
            if args == ["eval", "document.documentElement.outerHTML"]:
                return {"result": self.html}
            return {"result": URL}

        with patch.object(main, "run_agent_browser_json", side_effect=run) as command:
            main.fetch_with_agent_browser(URL)
        self.assertEqual(command.call_args.args[2], ["close"])
        self.assertEqual(len({call.args[1] for call in command.call_args_list}), 1)

    def test_agent_browser_closes_after_open_error(self):
        with (
            patch.object(
                main, "run_agent_browser_json", side_effect=[RuntimeError("failed"), {}]
            ) as command,
            self.assertRaises(RuntimeError),
        ):
            main.fetch_with_agent_browser(URL)
        self.assertEqual(command.call_args.args[2], ["close"])

    def test_browser_guard_blocks_non_public_requests(self):
        page, route = MagicMock(), MagicMock()
        route.request.url = "http://169.254.169.254/latest/meta-data"
        main._guard_browser_requests(page)
        handler = page.route.call_args.args[1]
        with patch.object(
            main, "validate_public_url", side_effect=ValueError("private")
        ):
            handler(route)
        route.abort.assert_called_once()
        route.continue_.assert_not_called()

    def test_agent_browser_ignores_ambient_auth_state(self):
        env = {
            "AGENT_BROWSER_SESSION_NAME": "saved-login",
            "AGENT_BROWSER_AUTO_CONNECT": "true",
        }

        def run(_binary, _session, args, **kwargs):
            effective = kwargs["env"]
            self.assertNotIn("AGENT_BROWSER_SESSION_NAME", effective)
            self.assertNotIn("AGENT_BROWSER_AUTO_CONNECT", effective)
            self.assertEqual(
                json.loads(Path(effective["AGENT_BROWSER_CONFIG"]).read_text()),
                {"headed": False},
            )
            return {
                "result": self.html
                if args == ["eval", "document.documentElement.outerHTML"]
                else URL
            }

        with (
            patch.object(main, "agent_browser_env", return_value=env),
            patch.object(main, "run_agent_browser_json", side_effect=run),
        ):
            main.fetch_with_agent_browser(URL)

    def test_flaresolverr_payload_and_http_ok_challenge_rejection(self):
        payload = {
            "status": "ok",
            "solution": {
                "url": URL,
                "response": (FIXTURES / "challenge.html").read_text(),
            },
        }
        response = io.BytesIO(json.dumps(payload).encode())
        with (
            patch.object(main, "urlopen", return_value=response) as fetch,
            self.assertRaisesRegex(RuntimeError, "challenge page"),
        ):
            main.fetch_with_flaresolverr(URL)
        self.assertEqual(json.loads(fetch.call_args.args[0].data)["url"], URL)

    def test_cli_contract_and_legacy_output_argument(self):
        with (
            patch.object(main, "scrape_article", return_value=self.valid),
            patch.object(Path, "write_text") as write,
            redirect_stdout(io.StringIO()) as stdout,
        ):
            self.assertEqual(main.main([URL, "/tmp/packet.md"]), 0)
        self.assertTrue(json.loads(stdout.getvalue())["ok"])
        write.assert_called_once()

    def test_cli_failure_contract(self):
        with (
            patch.object(main, "scrape_article", side_effect=RuntimeError("failed")),
            redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(main.main([URL]), 1)
        self.assertEqual(
            json.loads(stderr.getvalue()), {"ok": False, "error": "failed"}
        )


if __name__ == "__main__":
    unittest.main()
