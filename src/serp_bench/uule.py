"""Google's UULE string for a place below country level.

A helper for building requests, shared by the adapters whose providers take `uule`. It
measures nothing.
"""

from __future__ import annotations

import base64

_LENGTH_KEY = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def uule(canonical_name: str) -> str:
    """The UULE string for a canonical place name such as "Austin,Texas,United States":
    a fixed prefix, one character that encodes the name's length, and the name in base64."""
    raw = canonical_name.encode("utf-8")
    encoded = base64.b64encode(raw).decode("ascii").rstrip("=")
    return f"w+CAIQICI{_LENGTH_KEY[len(raw) % len(_LENGTH_KEY)]}{encoded}"
