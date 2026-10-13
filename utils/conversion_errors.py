from __future__ import annotations


class ConversionError(Exception):
    """
    A conversion cannot go on: a file, a tool or a reply was not usable.
    """


class ConversionCancelled(Exception):
    """
    The conversion was cancelled between two steps.

    Not a ConversionError: nothing went wrong, so it is not reported as a failure.
    """
