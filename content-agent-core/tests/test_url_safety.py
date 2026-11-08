import socket
import unittest
from unittest.mock import patch
from urllib.request import Request

from content_agent_core.url_safety import PublicRedirectHandler, validate_public_url


class PublicURLTests(unittest.TestCase):
    def test_non_public_addresses_are_rejected(self):
        for address in [
            "127.0.0.1",
            "10.0.0.1",
            "169.254.169.254",
            "::1",
            "fc00::1",
            "0.0.0.0",
            "224.0.0.1",
        ]:
            with (
                self.subTest(address=address),
                patch(
                    "socket.getaddrinfo",
                    return_value=[(socket.AF_INET, 1, 6, "", (address, 443))],
                ),
                self.assertRaisesRegex(ValueError, "non-public"),
            ):
                validate_public_url("https://example.org/article")

    def test_credentials_and_schemes_rejected_without_dns(self):
        with patch("socket.getaddrinfo") as dns:
            for url in [
                "file:///etc/passwd",
                "http://user:pass@example.org",
                "http://user@example.org",
                "http:///missing",
            ]:
                with self.assertRaises(ValueError):
                    validate_public_url(url)
        dns.assert_not_called()

    def test_mixed_public_private_dns_is_rejected(self):
        with (
            patch(
                "socket.getaddrinfo",
                return_value=[
                    (2, 1, 6, "", (ip, 443)) for ip in ["8.8.8.8", "10.0.0.1"]
                ],
            ),
            self.assertRaises(ValueError),
        ):
            validate_public_url("https://example.org")

    def test_public_host_accepted(self):
        with patch(
            "socket.getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]
        ):
            self.assertEqual(
                validate_public_url("https://example.org"), "https://example.org"
            )

    def test_pdf_redirect_to_metadata_address_blocked(self):
        with (
            patch(
                "socket.getaddrinfo",
                return_value=[(2, 1, 6, "", ("169.254.169.254", 80))],
            ),
            self.assertRaises(ValueError),
        ):
            PublicRedirectHandler().redirect_request(
                Request("https://example.org/a.pdf"),
                None,
                302,
                "",
                {},
                "http://169.254.169.254/latest/meta-data",
            )
