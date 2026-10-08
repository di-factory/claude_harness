"""Pagination helpers for listing records."""


def page(items: list, number: int, size: int = 10) -> list:
    """The items on page ``number`` (the first page is 1) with ``size`` items per page."""
    if number < 1 or size < 1:
        raise ValueError("page number and size start at 1")
    start = number * size
    return items[start : start + size]


def page_count(items: list, size: int = 10) -> int:
    """How many pages ``items`` fill (a partly filled last page counts)."""
    if size < 1:
        raise ValueError("size starts at 1")
    return -(-len(items) // size)
