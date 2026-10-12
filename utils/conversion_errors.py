from __future__ import annotations


class ConversionError(Exception):
    """
    A conversion cannot go on: a file, a tool or a reply was not usable.
    """
