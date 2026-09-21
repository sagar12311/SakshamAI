import unittest

from mac.screen_observer import ScreenObserver


class ScreenObserverTests(unittest.TestCase):
    def test_distinctive_product_label_needs_two_matching_tokens(self) -> None:
        self.assertTrue(ScreenObserver._has_label_evidence(
            "HP Z3700 Wireless Mouse", "HP Z3700 wireless mouse ₹475",
        ))
        self.assertFalse(ScreenObserver._has_label_evidence(
            "HP Z3700 Wireless Mouse", "wireless mouse options",
        ))

    def test_generic_page_terms_cannot_be_product_evidence(self) -> None:
        self.assertFalse(ScreenObserver._has_label_evidence(
            "The Sleep Company Stylux Premium Ergonomic Office Chair",
            "premium office chair Amazon results",
        ))

    def test_three_independent_visible_product_terms_are_sufficient(self) -> None:
        self.assertTrue(ScreenObserver._has_label_evidence(
            "Pack of 54 Motivational Inspirational Wall Collage Kit Poster",
            "54 motivational collage poster",
        ))


if __name__ == "__main__":
    unittest.main()
