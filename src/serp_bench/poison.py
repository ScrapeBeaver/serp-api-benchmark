"""Step 3 of the funnel: is the page a provider returned the page Google really serves?

Google answers traffic it distrusts with a page that looks normal and lists other sites.
Every check of step 1 and step 2 passes on such a page. Nobody can ask Google for the
"true" page, so the check is built on two properties of these pages instead:

* they are random: two of them, fetched independently, have almost nothing in common,
  while real pages for the same query fetched at the same minute largely coincide;
* they jump: a real ranking drifts by a site or so a day, a substituted page changes
  nearly everything at once.

Pages are compared as sets of sites (registrable domains) of their first ten results.
Order and exact addresses are not compared. Only resolved links count: an unresolved link
points at google.com and says nothing about where the result leads.

Signal A, the query's usual sites (history). The sites seen on at least 60% of the
reference pages of the same query over the last 14 days. A page that has 60% of them or
more passes, 40-59% is doubtful, less fails. Reference pages are those that were in the
agreeing group of their own run (signal B), from any provider. The rule is the one two
rank trackers publish (DemandSphere, Nozzle); it needs history, so it says nothing until a
query has reference pages from several days.

Signal B, agreement at the same minute. Every provider gets the same query at the same
moment. Two pages agree when at least half of the sites of the smaller one are on the
other. The agreeing group is the largest set of pages, from at least three providers,
that all agree with each other. This is not a majority vote: when three of seven
providers agree and the other four return four different pages, the three are the real
ones, because random pages do not coincide and real ones do. Three and not two: two
providers that share a quirk (the same other country, the same stale copy) agree with
each other as well, and with the honest pages missing they would be taken for the group.

Signal C, the control repeat. A page outside the group is asked for once more from the
same provider after the run. It does not change the verdict; it tells a one-off from a
provider that keeps returning a different page.

The verdict: `clean` when the page passes A or is in the group of B; `poisoned` when it
fails every signal that is available, and at least one is; `unjudged` otherwise. A page
that fails A but is in the group is clean: when every provider moved at once, Google
changed, not the provider. Unjudged pages are reported and never counted as clean.

A page with fewer than seven sites gets no verdict at all, clean or poisoned. Half of the
sites of a five-site page is three, and real pages of a weather or a loan search share two
of five often enough to be called poisoned by chance. The rule is the same in both
directions on purpose: dropping only the short poisoned pages would lower the poisoned
share of whoever returns short pages.

Nothing here knows a provider by name, and everything is recomputed from the records.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import combinations

from serp_bench.models import OUTCOME_DELIVERED, Record

TOP_RESULTS = 10  # results of a page that are compared
MIN_RESOLVED_LINKS = 5  # fewer resolved links than this: the page cannot be compared
MIN_SITES_TO_JUDGE = 7  # fewer sites than this: compared, but too short for a verdict
MIN_GROUP = 3  # providers whose pages all agree before they are taken for the real page
AGREE_SHARE = 0.5  # two pages agree when they share this part of the smaller one's sites
HISTORY_DAYS = 14  # how far back reference pages are taken from
MIN_REFERENCE_PAGES = 5  # history says nothing with fewer reference pages than this,
MIN_REFERENCE_DAYS = 3  # or with pages from fewer different days: one day is not a history
USUAL_SITE_SHARE = 0.6  # a site is usual when it is on this part of the reference pages
CLEAN_SHARE = 0.6  # a page with this part of the usual sites passes signal A
DOUBTFUL_SHARE = 0.4  # below CLEAN_SHARE and from here up: doubtful; below: fails

CLEAN, POISONED, UNJUDGED = "clean", "poisoned", "unjudged"
PASS, DOUBTFUL, FAIL = "pass", "doubtful", "fail"  # signal A
IN_GROUP, NEAR_GROUP, OUTSIDE_GROUP = "in", "near", "outside"  # signal B
ONE_OFF, PERSISTENT, DIFFERENT_AGAIN, NO_ANSWER = (  # signal C
    "one_off",
    "persistent",
    "different_again",
    "no_answer",
)

# Second-level labels under a country domain that are not a site's own name: "bbc.co.uk"
# is the site, not "co.uk". A short fixed list instead of the full public-suffix list: the
# same rule is applied to every provider's page, so a miss cannot favour anyone.
_GENERIC_SECOND_LEVEL = frozenset(
    ("ac", "co", "com", "edu", "gen", "go", "gob", "gov", "ind", "ltd")
    + ("mil", "ne", "net", "nhs", "nic", "or", "org", "plc", "sch")
)

Key = tuple[str, str, str]  # run id, task id, provider id


def site_of(host: str) -> str:
    """The site a host belongs to: "en.wikipedia.org" -> "wikipedia.org",
    "news.bbc.co.uk" -> "bbc.co.uk"."""
    labels = host.lower().strip(".").split(".")
    if len(labels) <= 2:
        return ".".join(labels)
    if len(labels[-1]) == 2 and labels[-2] in _GENERIC_SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def page_sites(record: Record) -> frozenset[str] | None:
    """The sites of the page's first ten results, taken from resolved links only.
    None when fewer than `MIN_RESOLVED_LINKS` of them are resolved."""
    top = sorted(record.results, key=lambda result: result.pos)[:TOP_RESULTS]
    hosts = [result.host for result in top if result.host and not result.wrapper]
    if len(hosts) < MIN_RESOLVED_LINKS:
        return None
    return frozenset(site_of(host) for host in hosts)


def shared(a: frozenset[str], b: frozenset[str]) -> tuple[int, int]:
    """(sites the two pages have in common, sites on the smaller page)."""
    return len(a & b), min(len(a), len(b))


def agree(a: frozenset[str], b: frozenset[str]) -> bool:
    common, smaller = shared(a, b)
    return smaller > 0 and common / smaller >= AGREE_SHARE


def clean_group(pages: Mapping[str, frozenset[str]]) -> frozenset[str]:
    """The providers whose pages form the agreeing group; empty when there is none.

    The group is the largest set of pages that all agree with each other. When several
    sets are equally large, only the pages that are in every one of them are certain, and
    they are the group if at least `MIN_GROUP` remain. Two equally large groups with
    nothing in common give no group at all: nothing says which of them is the real page.
    """
    ids = sorted(pages)
    for size in range(len(ids), MIN_GROUP - 1, -1):
        largest = [
            set(members)
            for members in combinations(ids, size)
            if all(agree(pages[x], pages[y]) for x, y in combinations(members, 2))
        ]
        if largest:
            certain = set.intersection(*largest)
            return frozenset(certain) if len(certain) >= MIN_GROUP else frozenset()
    return frozenset()


def usual_sites(reference: Sequence[frozenset[str]]) -> frozenset[str]:
    """The sites that are on at least `USUAL_SITE_SHARE` of the reference pages."""
    if not reference:
        return frozenset()
    seen = Counter(site for page in reference for site in page)
    return frozenset(s for s, n in seen.items() if n / len(reference) >= USUAL_SITE_SHARE)


def _when(record: Record) -> dt.datetime | None:
    stamp = record.scheduled_at or record.started_at
    if stamp is None:
        return None
    return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(dt.UTC)


def _query_key(record: Record) -> tuple[object, ...]:
    """What makes two pages "the same query": history is kept per query, place, device
    and results page."""
    return (
        " ".join(record.query.casefold().split()),
        record.country,
        record.locale,
        record.location,
        str(record.device),
        record.page,
    )


@dataclass(frozen=True)
class Verdict:
    """What step 3 says about one delivered page, and why."""

    verdict: str  # clean | poisoned | unjudged
    reason: str  # one sentence
    sites: tuple[str, ...] = ()  # the page's sites; empty when it could not be compared
    history: str | None = None  # signal A: pass | doubtful | fail; None: no history yet
    usual_share: float | None = None  # part of the query's usual sites that is on the page
    reference_pages: int = 0  # reference pages the history was taken from
    agreement: str | None = None  # signal B: in | near | outside; None: no group formed
    group: tuple[str, ...] = ()  # the providers in the agreeing group at that moment
    # With each other provider's page of the same moment: (sites in common, smaller page).
    shared_with: dict[str, tuple[int, int]] = field(default_factory=dict)
    control: str | None = None  # signal C, when a control repeat was made

    @property
    def signals(self) -> tuple[str, ...]:
        """Which signals had something to say: "history", "agreement"."""
        named = (("history", self.history), ("agreement", self.agreement))
        return tuple(name for name, value in named if value is not None)


def _history_signal(share: float) -> str:
    if share >= CLEAN_SHARE:
        return PASS
    return DOUBTFUL if share >= DOUBTFUL_SHARE else FAIL


def decide(history: str | None, agreement: str | None) -> tuple[str, str]:
    """THE verdict rule: (verdict, reason) from what the two signals say."""
    if history == PASS and agreement == IN_GROUP:
        return CLEAN, "has the query's usual sites and matches the other providers' pages"
    if agreement == IN_GROUP:
        if history is None:
            return CLEAN, "matches the other providers' pages of the same minute"
        return CLEAN, (
            "matches the other providers' pages although the usual sites are missing: "
            "the results changed for everyone"
        )
    if history == PASS:
        return CLEAN, "has the query's usual sites"
    if history is None and agreement is None:
        return UNJUDGED, "no history for this query yet, and no single group of pages agreed"
    if history in (FAIL, None) and agreement in (OUTSIDE_GROUP, None):
        if history is None:
            return POISONED, "shares too little with every page of the agreeing group"
        if agreement is None:
            return POISONED, "has too few of the query's usual sites"
        return POISONED, "has too few of the usual sites and matches no other provider's page"
    return UNJUDGED, "doubtful: close to the usual sites or to the group, not close enough"


@dataclass(frozen=True)
class _Reference:
    when: dt.datetime
    run_id: str
    sites: frozenset[str]


def judge(records: Iterable[Record]) -> dict[Key, Verdict]:
    """A verdict for every delivered, measured page among `records`, keyed by
    (run id, task id, provider). Pass the records of the judged runs together with those
    of the two weeks before them: the earlier ones are the history."""
    records = list(records)
    measured = [r for r in records if r.counts and r.outcome == OUTCOME_DELIVERED]
    controls = {(r.run_id, r.task_id, r.provider): r for r in records if r.control and not r.warmup}

    moments: dict[tuple[str, str], dict[str, Record]] = {}
    for record in measured:
        moments.setdefault((record.run_id, record.task_id), {})[record.provider] = record

    # Signal B for every moment; the pages of each group become the reference pages.
    sites: dict[Key, frozenset[str]] = {}
    groups: dict[tuple[str, str], frozenset[str]] = {}
    references: dict[tuple[object, ...], list[_Reference]] = {}
    for moment, pages in moments.items():
        comparable = {}
        for provider, record in pages.items():
            page = page_sites(record)
            if page is not None:
                comparable[provider] = sites[(*moment, provider)] = page
        group = groups[moment] = clean_group(comparable)
        for provider in group:
            when = _when(pages[provider])
            if when is not None:
                references.setdefault(_query_key(pages[provider]), []).append(
                    _Reference(when, moment[0], comparable[provider])
                )

    verdicts: dict[Key, Verdict] = {}
    for moment, pages in moments.items():
        group = groups[moment]
        for provider, record in pages.items():
            key = (*moment, provider)
            page = sites.get(key)
            if page is None:
                verdicts[key] = Verdict(
                    UNJUDGED,
                    f"fewer than {MIN_RESOLVED_LINKS} resolved links: nothing to compare",
                )
                continue

            others = {
                p: sites[(*moment, p)] for p in pages if p != provider and (*moment, p) in sites
            }
            agreement = None
            if group:
                members = [others[p] for p in group if p != provider]
                if provider in group:
                    agreement = IN_GROUP
                elif any(agree(page, member) for member in members):
                    agreement = NEAR_GROUP
                else:
                    agreement = OUTSIDE_GROUP

            usual, reference = _usual_for(record, references)
            history = share = None
            if usual:
                share = len(usual & page) / len(usual)
                history = _history_signal(share)

            verdict, reason = decide(history, agreement)
            if len(page) < MIN_SITES_TO_JUDGE:
                # Compared like any other page, and it can be in the group, but it is given
                # no verdict of its own in either direction.
                verdict = UNJUDGED
                reason = f"fewer than {MIN_SITES_TO_JUDGE} sites on the page: too short to judge"
            verdicts[key] = Verdict(
                verdict,
                reason,
                sites=tuple(sorted(page)),
                history=history,
                usual_share=None if share is None else round(share, 3),
                reference_pages=len(reference),
                agreement=agreement,
                group=tuple(sorted(group)),
                shared_with={p: shared(page, other) for p, other in sorted(others.items())},
                control=_control(
                    page, controls.get(key), [others[p] for p in group if p in others], usual
                ),
            )
    return verdicts


def _usual_for(
    record: Record, references: Mapping[tuple[object, ...], list[_Reference]]
) -> tuple[frozenset[str], list[_Reference]]:
    """The usual sites for this record's query as they stood before its run, and the
    reference pages they come from. Empty when there is not enough history."""
    when = _when(record)
    if when is None:
        return frozenset(), []
    since = when - dt.timedelta(days=HISTORY_DAYS)
    earlier = [
        ref
        for ref in references.get(_query_key(record), [])
        if ref.run_id != record.run_id and since <= ref.when < when
    ]
    days = {ref.when.date() for ref in earlier}
    if len(earlier) < MIN_REFERENCE_PAGES or len(days) < MIN_REFERENCE_DAYS:
        return frozenset(), []
    return usual_sites([ref.sites for ref in earlier]), earlier


def _control(
    page: frozenset[str],
    control: Record | None,
    group_pages: Sequence[frozenset[str]],
    usual: frozenset[str],
) -> str | None:
    """Signal C: what the control repeat of this page showed, when one was made."""
    if control is None:
        return None
    again = page_sites(control) if control.outcome == OUTCOME_DELIVERED else None
    if again is None:
        return NO_ANSWER
    with_group = bool(group_pages) and all(agree(again, member) for member in group_pages)
    with_usual = bool(usual) and len(usual & again) / len(usual) >= CLEAN_SHARE
    if with_group or with_usual:
        return ONE_OFF  # the provider can return the real page: the first one was a one-off
    if agree(again, page):
        return PERSISTENT  # the same different page twice: another country, an old cache
    return DIFFERENT_AGAIN  # a third page: the mark of random substitution


def control_candidates(records: Iterable[Record]) -> list[Record]:
    """The measured first pages worth a control repeat: those the agreement signal puts
    outside the group of their moment, short pages left out (they get no verdict). In the
    order they were sent."""
    records = list(records)
    verdicts = judge(records)
    return [
        record
        for record in records
        if record.counts
        and record.page == 1
        and (verdict := verdicts.get((record.run_id, record.task_id, record.provider)))
        and verdict.agreement == OUTSIDE_GROUP
        and len(verdict.sites) >= MIN_SITES_TO_JUDGE
    ]


def tally(verdicts: Iterable[Verdict]) -> dict[str, int]:
    """How many pages got each verdict."""
    counts = Counter(verdict.verdict for verdict in verdicts)
    return {name: counts.get(name, 0) for name in (CLEAN, POISONED, UNJUDGED)}
