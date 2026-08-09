import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


class WebScraperHelperTests(unittest.TestCase):
    def test_large_captcha_packet_is_always_invalid(self) -> None:
        data = {
            "title": "Are you a robot?",
            "abstract": "",
            "body_markdown": (
                "Please confirm you are a human by completing the captcha challenge.\n"
                "Enable JavaScript and cookies to continue.\n"
                + ("bundled javascript " * 20000)
            ),
        }

        issues = main.extraction_issues(data)

        self.assertIn("invalid_title", issues)
        self.assertIn("captcha_page", issues)
        self.assertTrue(main.has_fatal_extraction_issue(data))
        self.assertLess(main.extraction_score(data), 0)

    def test_bad_interstitial_skips_crossref_enrichment(self) -> None:
        html = """
        <html><head><title>Are you a robot?</title></head>
        <body><h1>Are you a robot?</h1>
        <p>Please confirm you are a human by completing the captcha challenge below.</p>
        </body></html>
        """

        with patch.object(main, "fetch_json") as fetch_json:
            data = main.extract_article_data(
                "https://example.org/article",
                html,
                retrieval_backend="fixture",
            )

        fetch_json.assert_not_called()
        self.assertEqual(data["enrichment_skipped"], "fatal_retrieval_issue")
        self.assertFalse(data.get("doi"))

    def test_doi_metadata_title_mismatch_invalidates_packet(self) -> None:
        data = {
            "url": "https://example.org/article",
            "title": "Quercetin and preeclampsia prevention",
            "doi": "10.1000/example",
            "abstract": "A sufficiently detailed abstract about quercetin.",
            "body_markdown": "article body " * 100,
        }
        unrelated = {
            "doi": "10.1000/example",
            "title": "Continuous delivery practices in software teams",
        }
        with (
            patch.object(main, "fetch_crossref_metadata", return_value=unrelated),
            patch.object(main, "fetch_pubmed_metadata", return_value={}),
            patch.object(main, "fetch_unpaywall_metadata", return_value={}),
        ):
            main.enrich_metadata(data)

        self.assertIn("doi_title_mismatch", main.extraction_issues(data))
        self.assertTrue(main.has_fatal_extraction_issue(data))

    def test_looks_like_pdf_source_for_url_and_file(self) -> None:
        self.assertTrue(main.looks_like_pdf_source("https://example.com/paper.pdf"))
        self.assertTrue(main.looks_like_pdf_source("/tmp/paper.pdf"))
        self.assertFalse(main.looks_like_pdf_source("https://example.com/paper"))

    def test_extract_pdf_abstract_and_keywords(self) -> None:
        sample = """
        Abstract
        This randomized controlled trial evaluated a nutrition intervention in adults.

        Keywords: nutrition, adults, randomized trial

        Introduction
        More body text follows here.
        """
        self.assertIn(
            "randomized controlled trial",
            main.extract_pdf_abstract(sample).lower(),
        )
        self.assertIn("nutrition", main.extract_pdf_keywords(sample).lower())

    def test_citation_url_prefers_doi(self) -> None:
        self.assertEqual(
            main.citation_url({"doi": "10.1000/xyz123", "url": "https://example.com"}),
            "https://doi.org/10.1000/xyz123",
        )

    def test_doi_labels_are_not_accepted_as_identifiers(self) -> None:
        self.assertEqual(main.normalize_doi("DOI:"), "")
        self.assertEqual(
            main.normalize_doi("https://doi.org/10.1097/MS9.0000000000005494"),
            "10.1097/MS9.0000000000005494",
        )

    def test_ovid_style_placeholders_and_truncation_are_recovered(self) -> None:
        url = (
            "https://www.ovid.com/jnls/example/fulltext/"
            "10.1097/ms9.0000000000005494~significant-anti-myeloma"
        )
        full_title = (
            "Significant anti-myeloma potential of green tea infusion and its "
            "component dihydromyricetin"
        )
        full_abstract = (
            "Multiple myeloma is a hematological malignancy. This study compared "
            "four green tea infusions in cell and xenograft mouse models and found "
            "that Biluochun showed the strongest preclinical activity."
        )
        html = f"""
        <html><head>
          <meta property="og:title" content="Significant anti-myeloma potential... : Journal">
          <meta property="og:description" content="Multiple myeloma is a hematological malignancy...">
          <meta property="og:site_name" content="Ovid">
        </head><body><article>
          <h1>{full_title}</h1>
          <div class="author">Authors and Affiliations</div>
          <p>Annals of Medicine &amp; Surgery, August 06, 2026. DOI: 10.1097/MS9.0000000000005494</p>
          <div>Abstract</div><div>In Brief</div><p>{full_abstract}</p>
          <h2>Introduction</h2><p>{'body evidence ' * 100}</p>
        </article></body></html>
        """
        crossref = {
            "doi": "10.1097/MS9.0000000000005494",
            "title": full_title,
            "authors": "Juan Xiao, Hongyu Zhu",
            "journal": "Annals of Medicine & Surgery",
            "pub_date": "2026-08-06",
            "abstract": "",
        }
        with (
            patch.object(main, "fetch_crossref_metadata", return_value=crossref),
            patch.object(main, "fetch_pubmed_metadata", return_value={}),
            patch.object(main, "fetch_unpaywall_metadata", return_value={}),
        ):
            data = main.extract_article_data(url, html, retrieval_backend="fixture")

        self.assertEqual(data["title"], full_title)
        self.assertEqual(data["doi"], "10.1097/ms9.0000000000005494")
        self.assertEqual(data["reference_url"], "https://doi.org/10.1097/ms9.0000000000005494")
        self.assertEqual(data["authors"], "Juan Xiao, Hongyu Zhu")
        self.assertEqual(data["journal"], "Annals of Medicine & Surgery")
        self.assertEqual(data["pub_date"], "2026-08-06")
        self.assertEqual(data["abstract"], full_abstract)
        self.assertEqual(data["citation_metadata_issues"], [])
        self.assertIn("authors:recovered_from_crossref", data["metadata_repairs"])

    def test_extract_pmid_from_pubmed_url(self) -> None:
        self.assertEqual(
            main.extract_pmid("https://pubmed.ncbi.nlm.nih.gov/40878114/"),
            "40878114",
        )

    def test_canonicalize_article_url_publisher_pdf_gates(self) -> None:
        cases = [
            (
                "https://journals.sagepub.com/doi/pdf/10.1177/09603271251323134",
                "https://journals.sagepub.com/doi/full/10.1177/09603271251323134",
                "sage",
            ),
            (
                "https://acsess.onlinelibrary.wiley.com/doi/pdfdirect/10.1002/agg2.70313",
                "https://acsess.onlinelibrary.wiley.com/doi/abs/10.1002/agg2.70313",
                "wiley",
            ),
            (
                "https://academic.oup.com/pnasnexus/advance-article-pdf/doi/10.1093/pnasnexus/pgaf078/62307851/pgaf078.pdf",
                "https://doi.org/10.1093/pnasnexus/pgaf078",
                "oup",
            ),
            (
                "https://link.springer.com/content/pdf/10.1007/s11694-025-03155-3.pdf",
                "https://link.springer.com/article/10.1007/s11694-025-03155-3",
                "springer",
            ),
            (
                "https://www.mdpi.com/2227-9717/13/3/767/pdf",
                "https://www.mdpi.com/2227-9717/13/3/767",
                "mdpi",
            ),
            (
                "https://www.tandfonline.com/doi/pdf/10.1080/10412905.2025.2470781",
                "https://www.tandfonline.com/doi/full/10.1080/10412905.2025.2470781",
                "tandf",
            ),
        ]
        for src, expected, label in cases:
            got, notes = main.canonicalize_article_url(src)
            self.assertEqual(got, expected, msg=label)
            self.assertTrue(notes, msg=f"{label} should record rewrite notes")

        # Local paths and normal HTML URLs are unchanged
        local = "/tmp/paper.pdf"
        self.assertEqual(main.canonicalize_article_url(local), (local, []))
        html = "https://www.nature.com/articles/s41598-025-87298-9"
        self.assertEqual(main.canonicalize_article_url(html)[0], html)

    def test_extract_article_data_from_html_fixture(self) -> None:
        html = (FIXTURES_DIR / "article.html").read_text(encoding="utf-8")

        with patch.object(main, "fetch_json", side_effect=RuntimeError("no network")):
            data = main.extract_article_data(
                "https://example.org/article",
                html,
                retrieval_backend="fixture",
            )

        self.assertEqual(data["title"], "Example Trial on Nutrition")
        self.assertEqual(data["doi"], "10.1000/example-doi")
        self.assertEqual(data["journal"], "Nutrition Journal")
        self.assertEqual(data["source_kind"], "html")
        self.assertIn("randomized controlled trial", data["abstract"].lower())

    def test_scrape_pdf_source_uses_fixture_preview_text(self) -> None:
        preview_text = (FIXTURES_DIR / "pdf_preview.txt").read_text(encoding="utf-8")

        class FakePage:
            def extract_text(self) -> str:
                return preview_text

        class FakeReader:
            metadata = {"/Title": "Fixture PDF Study", "/Author": "Jane Doe"}
            pages = [FakePage()]

            def __init__(self, _path: str) -> None:
                pass

        with tempfile.TemporaryDirectory() as tmpdir, patch.object(main, "PdfReader", FakeReader):
            pdf_path = Path(tmpdir) / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-fixture")
            data = main.scrape_pdf_source(str(pdf_path))

        self.assertEqual(data["title"], "Fixture PDF Study")
        self.assertEqual(data["authors"], "Jane Doe")
        self.assertEqual(data["source_kind"], "pdf")
        self.assertIn("nutrition intervention", data["abstract"].lower())


if __name__ == "__main__":
    unittest.main()
