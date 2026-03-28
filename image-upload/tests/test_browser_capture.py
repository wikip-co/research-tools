import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from image_upload import browser_capture


class BrowserCaptureTests(unittest.TestCase):
    def test_capture_page_screenshot_writes_expected_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)

            def fake_run(binary, session, args, **kwargs):  # type: ignore[no-untyped-def]
                if args[0] == "screenshot":
                    Path(args[1]).write_bytes(b"png")
                    return {}
                if args[:2] == ["eval", "document.title"]:
                    return {"result": "Example Page"}
                if args[:2] == ["eval", "location.href"]:
                    return {"result": "https://example.com/final"}
                return {"result": ""}

            with patch.object(browser_capture, "resolve_agent_browser_binary", return_value="/usr/bin/agent-browser"), patch.object(
                browser_capture,
                "run_agent_browser_json",
                side_effect=fake_run,
            ):
                capture = browser_capture.capture_page_screenshot(
                    "https://example.com",
                    output_path=output_dir,
                    basename="example-shot",
                    full_page=True,
                )

            self.assertTrue(Path(capture["local_path"]).is_file())
            self.assertEqual(capture["page_title"], "Example Page")
            self.assertEqual(capture["final_url"], "https://example.com/final")


if __name__ == "__main__":
    unittest.main()
