import unittest

from wiki_automation.local_publish import study_type_contract, study_type_matches_packet


class StudyTypeContractTests(unittest.TestCase):
    def test_specific_recorded_type_is_copied_verbatim(self):
        packet = {"study_type": "Systematic Review and Meta-Analysis"}
        text = study_type_contract(packet, "fallback")
        self.assertIn("Systematic Review and Meta-Analysis", text)
        self.assertIn("verbatim", text)
        # The value the model is told to echo passes the unchanged gate.
        self.assertTrue(study_type_matches_packet(packet["study_type"], packet))

    def test_generic_or_missing_type_keeps_refinement_fallback(self):
        for recorded in ("", "Article", "Research Article", "original article"):
            self.assertEqual(
                study_type_contract({"study_type": recorded}, "fallback"), "fallback"
            )


if __name__ == "__main__":
    unittest.main()
