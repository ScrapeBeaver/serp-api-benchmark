"""Google's AI Overview: whether a provider's answer to an ordinary search carries it.

Every provider puts the block in a different place and gives it a different shape, so an
adapter only points at it (`read(data.get("ai_overview"))`). What is counted is decided
here, the same way for everyone: the characters of the overview's own text. Addresses,
tokens, ids and the list of sources are left out, so a block is not longer because it
names more references.

A block with too little text is not an overview a reader could use: some answers carry
only its first line, or a token for a second request. The second request is another
product at another price, and it is not sent.

Whether a search had an AI Overview at all is not known from one answer: Google shows it
for some searches and not for others, and not always. A search is taken to have had one
when at least `MIN_PROVIDERS` providers returned its text for the same task of the same
run. A provider is then counted on those searches only.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from serp_bench.models import OUTCOME_DELIVERED, AiOverview, Record

MIN_CHARS = 200  # less text than this is a first line or a label, not the overview
MIN_PROVIDERS = 2  # providers that must return the text for a search to count as having one

# Keys whose values are not the overview's own words.
_NOT_TEXT = re.compile(
    r"link|url|href|token|thumbnail|image|favicon|icon|source|reference|citation|domain|date"
    r"|(^|_)(id|type)$",
    re.IGNORECASE,
)
# Keys that carry what a second request needs.
_POINTER = re.compile(r"token|serpapi_link", re.IGNORECASE)


def _chars(node: Any) -> int:
    """Characters of text under a node. Everything under a key that is not text (the
    list of sources with their titles, for one) is left out whole."""
    if isinstance(node, str):
        return 0 if node.startswith(("http://", "https://", "data:")) else len(node.strip())
    if isinstance(node, dict):
        return sum(_chars(value) for name, value in node.items() if not _NOT_TEXT.search(str(name)))
    if isinstance(node, list):
        return sum(_chars(value) for value in node)
    return 0


def _points_elsewhere(node: Any) -> bool:
    if isinstance(node, dict):
        return any(
            (_POINTER.search(str(name)) and isinstance(value, str) and bool(value))
            or _points_elsewhere(value)
            for name, value in node.items()
        )
    if isinstance(node, list):
        return any(_points_elsewhere(value) for value in node)
    return False


def read(block: Any) -> AiOverview | None:
    """The block of a provider's answer in the common shape. None when the answer has no
    such block (the key is absent or null)."""
    if block is None:
        return None
    chars = _chars(block)
    return AiOverview(chars=chars, deferred=chars < MIN_CHARS and _points_elsewhere(block))


def has_text(overview: AiOverview | None) -> bool:
    """The answer carried the overview's text, not only its first line or a token."""
    return overview is not None and overview.chars >= MIN_CHARS


def searches_with_one(records: Iterable[Record]) -> set[tuple[str, str]]:
    """The (run, task) pairs taken to have had an AI Overview: at least `MIN_PROVIDERS`
    providers returned its text. Warm-ups and control repeats are not looked at."""
    seen: dict[tuple[str, str], set[str]] = defaultdict(set)
    for record in records:
        if record.counts and record.outcome == OUTCOME_DELIVERED and has_text(record.ai_overview):
            seen[(record.run_id, record.task_id)].add(record.provider)
    return {key for key, providers in seen.items() if len(providers) >= MIN_PROVIDERS}
