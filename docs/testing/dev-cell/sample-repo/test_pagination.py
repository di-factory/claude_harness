import unittest

from pagination import page, page_count


class PageCountTest(unittest.TestCase):
    def test_counts_a_partly_filled_last_page(self) -> None:
        self.assertEqual(page_count(list(range(25)), 10), 3)

    def test_empty_list_has_no_pages(self) -> None:
        self.assertEqual(page_count([], 10), 0)


class PageTest(unittest.TestCase):
    def test_rejects_page_zero(self) -> None:
        with self.assertRaises(ValueError):
            page([1, 2, 3], 0)


if __name__ == "__main__":
    unittest.main()
