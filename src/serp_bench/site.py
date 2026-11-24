"""Build the results site: static pages, ready for any static host.

    dist/
      index.html               results
      pricing/index.html       published prices, by volume
      methodology/index.html   what is measured and how
      summary.json             the data behind all three
      llms.txt, robots.txt, sitemap.xml, CNAME, .nojekyll, 404.html, assets/

The three pages are one template (site/template.html) cut three ways: each carries the
shared chrome, its own section, its own title and description, and the summary. The main
table of each page is written into the HTML, so it reads without running a script.

Nothing here computes a figure: that is `summary.py`. This module only writes what it is
given.
"""

from __future__ import annotations

import html
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SUMMARY_MARK = "/*__SUMMARY__*/null"
HEAD_MARK = "<!--__HEAD__-->"
PANEL = re.compile(r"  <!-- =+ (?P<name>\w+) =+ -->\n(?P<body>.*?)  <!-- /panel -->\n", re.DOTALL)

OPERATOR = "ScrapeBeaver"
OPERATOR_URL = "https://scrapebeaver.com"
# The licence of the records and the figures (LICENSE-DATA.md). The code is under MIT.
DATA_LICENCE = "https://creativecommons.org/licenses/by/4.0/"
# The same organisation the main site declares, so that both sites describe one entity.
ORGANIZATION = {
    "@type": "Organization",
    "@id": f"{OPERATOR_URL}/#organization",
    "name": OPERATOR,
    "url": OPERATOR_URL,
}

# Crawlers of answer engines and assistants, welcomed by name as on the main site: the
# benchmark is published to be read and cited.
AI_CRAWLERS = (
    "GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "Claude-Web", "anthropic-ai",
    "PerplexityBot", "Perplexity-User", "Google-Extended", "Applebot-Extended", "Amazonbot",
    "Bytespider", "CCBot", "cohere-ai", "Meta-ExternalAgent",
)  # fmt: skip


@dataclass(frozen=True)
class Page:
    key: str  # the section of the template this page carries
    path: str  # "" for the root, otherwise "name/"
    title: str  # the <title>, without the site name
    h1: str
    crumb: str | None  # its name in the breadcrumb; None for the root


PAGES: tuple[Page, ...] = (
    Page(
        "performance",
        "",
        "Google SERP API Benchmark: Success Rate, Speed and Price",
        "Google Search API benchmark",
        None,
    ),
    Page(
        "pricing",
        "pricing/",
        "SERP API Pricing Compared: Cost per 1,000 Searches by Volume",
        "Google SERP API pricing, compared by volume",
        "Pricing",
    ),
    Page(
        "methodology",
        "methodology/",
        "SERP API Benchmark Methodology: What Is Measured and How",
        "How the Google SERP API benchmark works",
        "Methodology",
    ),
)


# --- words ---------------------------------------------------------------------------------


def _names(summary: dict[str, Any]) -> str:
    return ", ".join(provider["name"] for provider in summary["providers"])


def description(page: Page, summary: dict[str, Any]) -> str:
    """One or two sentences that say what the page holds. Written from the data, so that it
    never names a provider that is not on the page."""
    names = f" ({_names(summary)})" if summary["providers"] else ""
    if page.key == "pricing":
        return (
            f"What Google search APIs{names} cost per 1,000 searches at every monthly volume "
            "from 10,000 to 100 million, by their published prices and their own terms: the "
            "plan, the overage and the month's bill behind each figure."
        )
    if page.key == "methodology":
        return (
            "How the benchmark measures Google search APIs: the same searches sent to every "
            "provider in the same second, then three checks. Did it succeed, are the result "
            "URLs resolved, is the page poisoned. With examples and the formula of each number."
        )
    updated = f" Updated {summary['as_of']}." if summary["providers"] else ""
    return (
        f"An open benchmark of Google search APIs{names}: the same searches sent to every "
        "provider in the same second. Success rate, resolved URLs, response time and price "
        f"per 1,000 searches.{updated}"
    )


def _percent(figure: list[float] | None) -> str:
    return "collecting data" if figure is None else f"{figure[0]:.1f}%"


def _seconds(figure: list[float] | None) -> str:
    return "collecting data" if figure is None else f"{figure[0]:.1f} s"


def _price_at(summary: dict[str, Any], provider_id: str, volume: int) -> float | None:
    data = summary["pricing"]["providers"].get(provider_id) or {}
    point = next((p for p in data.get("points", []) if p[0] == volume), None)
    return None if not point else point[1]


def _price(summary: dict[str, Any], provider_id: str, volume: int) -> str:
    price = _price_at(summary, provider_id, volume)
    return "not published" if price is None else f"${price:.2f}"


# --- what a search engine and a link preview read -----------------------------------------


def _ld(*things: dict[str, Any]) -> str:
    graph = {"@context": "https://schema.org", "@graph": list(things)}
    return (
        '<script type="application/ld+json">'
        + json.dumps(graph, ensure_ascii=False).replace("</", "<\\/")
        + "</script>"
    )


def _structured(page: Page, summary: dict[str, Any], url: str) -> str:
    """Structured data: who publishes this, where the page sits, and what it is."""
    here = url + page.path
    site = {
        "@type": "WebSite",
        "@id": url + "#website",
        "url": url,
        "name": f"{OPERATOR} SERP API Benchmark",
        "publisher": {"@id": ORGANIZATION["@id"]},
        "inLanguage": "en",
    }
    things: list[dict[str, Any]] = [ORGANIZATION, site]
    if page.crumb:
        crumbs = [("Benchmark", url), (page.crumb, here)]
        things.append(
            {
                "@type": "BreadcrumbList",
                "itemListElement": [
                    {"@type": "ListItem", "position": i, "name": name, "item": item}
                    for i, (name, item) in enumerate(crumbs, start=1)
                ],
            }
        )
    if page.key == "methodology":
        things.append(
            {
                "@type": "TechArticle",
                "headline": page.h1,
                "description": description(page, summary),
                "url": here,
                "dateModified": summary["generated_at"],
                "author": {"@id": ORGANIZATION["@id"]},
                "publisher": {"@id": ORGANIZATION["@id"]},
                "isPartOf": {"@id": site["@id"]},
            }
        )
    else:
        dataset: dict[str, Any] = {
            "@type": "Dataset",
            "name": page.h1,
            "description": description(page, summary),
            "url": here,
            "creator": {"@id": ORGANIZATION["@id"]},
            "isAccessibleForFree": True,
            "license": DATA_LICENCE,
            "dateModified": summary["generated_at"],
            "variableMeasured": (
                ["Price per 1,000 searches", "Monthly bill", "Units charged per search"]
                if page.key == "pricing"
                else ["Success rate", "Resolved URLs", "Response time", "Price per 1,000"]
            ),
            "distribution": [
                {
                    "@type": "DataDownload",
                    "encodingFormat": "application/json",
                    "contentUrl": url + "summary.json",
                }
            ],
            "isPartOf": {"@id": site["@id"]},
        }
        if summary.get("first_day"):
            dataset["temporalCoverage"] = f"{summary['first_day']}/{summary['as_of']}"
        if summary.get("repository"):
            dataset["isBasedOn"] = summary["repository"]
        things.append(dataset)
    return _ld(*things)


def head(page: Page, summary: dict[str, Any], url: str) -> str:
    here = url + page.path
    root = "../" if page.path else "./"
    title = html.escape(f"{page.title} | {OPERATOR}", quote=True)
    text = html.escape(description(page, summary), quote=True)
    robots = "noindex, nofollow" if summary["preview"] else "index, follow, max-image-preview:large"
    image = url + "assets/og.png"
    return "\n".join(
        [
            f"<title>{title}</title>",
            f'<meta name="description" content="{text}">',
            f'<meta name="robots" content="{robots}">',
            f'<link rel="canonical" href="{here}">',
            f'<link rel="icon" href="{root}assets/icon.svg" type="image/svg+xml">',
            '<meta property="og:type" content="website">',
            f'<meta property="og:site_name" content="{OPERATOR}">',
            '<meta property="og:locale" content="en_US">',
            f'<meta property="og:title" content="{title}">',
            f'<meta property="og:description" content="{text}">',
            f'<meta property="og:url" content="{here}">',
            f'<meta property="og:image" content="{image}">',
            '<meta property="og:image:width" content="1200">',
            '<meta property="og:image:height" content="630">',
            '<meta name="twitter:card" content="summary_large_image">',
            f'<meta name="twitter:title" content="{title}">',
            f'<meta name="twitter:description" content="{text}">',
            f'<meta name="twitter:image" content="{image}">',
            _structured(page, summary, url),
        ]
    )


# --- tables written into the HTML -----------------------------------------------------------


def step3_name(summary: dict[str, Any]) -> str:
    """What the step-3 figure is called, whichever signals the verdicts rest on. How a
    page is judged at each stage is told on the methodology page."""
    return "Poisoned results"


def static_results(summary: dict[str, Any]) -> str:
    """The results table as plain HTML. The page's script replaces it with the interactive
    one; without a script, and for a crawler, this is what is read."""
    headers = [
        "Success rate",
        "Resolved URLs",
        step3_name(summary),
        "Median time",
        "Price / 1,000 at 100,000 a month",
    ]
    rows = []
    for provider in summary["providers"]:
        window = provider["windows"]["30"]
        cells = [
            _percent(window["success"]),
            _percent(window["resolved"]),
            _percent(window["poisoned"]),
            _seconds(window["p50"]),
            _price(summary, provider["id"], 100_000),
        ]
        rows.append(
            f'<tr><th scope="row">{html.escape(provider["name"])}</th>'
            + "".join(f'<td class="r">{cell}</td>' for cell in cells)
            + "</tr>"
        )
    return (
        f"<caption>Last 30 days, as of {summary['as_of']}</caption>"
        '<thead><tr><th scope="col">Provider</th>'
        + "".join(f'<th scope="col" class="r">{name}</th>' for name in headers)
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody>"
    )


def static_prices(summary: dict[str, Any]) -> str:
    """Price per 1,000 searches at every step of the volume ladder, as plain HTML."""
    providers = summary["providers"]
    rows = []
    for volume in summary["pricing"]["ladder"]:
        prices = [_price_at(summary, provider["id"], volume) for provider in providers]
        if all(price is None for price in prices):
            continue  # nobody on the page publishes a price this high
        rows.append(
            f'<tr><th scope="row">{volume:,}</th>'
            + "".join(
                f"<td>{'not published' if price is None else f'${price:.2f}'}</td>"
                for price in prices
            )
            + "</tr>"
        )
    return (
        "<caption>Price per 1,000 searches, by searches a month</caption>"
        '<thead><tr><th scope="col">Searches a month</th>'
        + "".join(f'<th scope="col">{html.escape(p["name"])}</th>' for p in providers)
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody>"
    )


# --- pages -----------------------------------------------------------------------------------


def render(template: str, page: Page, summary: dict[str, Any], url: str) -> str:
    """One page: the template with only this page's section, its head and the data."""
    url = url.rstrip("/") + "/"
    for mark in (SUMMARY_MARK, HEAD_MARK, "{{PAGE}}", "{{H1}}"):
        if template.count(mark) != 1:
            raise ValueError(f"the template must contain `{mark}` exactly once")
    sections = {match["name"].lower() for match in PANEL.finditer(template)}
    if sections != {p.key for p in PAGES}:
        raise ValueError(f"the template's sections are {sorted(sections)}")

    def keep(match: re.Match[str]) -> str:
        if match["name"].lower() != page.key:
            return ""
        return match["body"].replace(f'data-panel="{page.key}" hidden', f'data-panel="{page.key}"')

    text = PANEL.sub(keep, template)
    if page.key == "performance":
        table = '<table id="results"></table>'
        text = text.replace(table, f'<table id="results">{static_results(summary)}</table>')
    if page.key == "pricing":
        table = '<table class="mini" id="volumes"></table>'
        if text.count(table) != 1:
            raise ValueError("the pricing section must contain the volumes table once")
        static = f'<table class="mini" id="volumes">{static_prices(summary)}</table>'
        text = text.replace(table, static)
    text = text.replace(f'data-tab="{page.key}"', f'data-tab="{page.key}" aria-current="page"')
    # `</` inside the data must not end the script element it is written into.
    data = json.dumps(summary, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    text = text.replace(SUMMARY_MARK, data)
    text = text.replace(HEAD_MARK, head(page, summary, url))
    text = text.replace("{{PAGE}}", page.key).replace("{{H1}}", html.escape(page.h1))
    return text.replace("{{ROOT}}", "../" if page.path else "./")


def llms_txt(summary: dict[str, Any], url: str) -> str:
    """A plain-text account of the site for assistants and answer engines: what it is, where
    things are, and the current figures, so that a quoted number can be the right one."""
    lines = [
        f"# {OPERATOR} SERP API Benchmark",
        "",
        "> An open, reproducible benchmark of Google search APIs. The same searches are sent "
        "to every provider within the same three minutes, three times a day; each answer is "
        "checked for delivery, for resolved result URLs and for results that are not the ones "
        "Google really serves (poisoned results). Run by "
        f"{OPERATOR}, which is one of the providers measured. Code and records are public.",
        "",
        "## Pages",
        "",
    ]
    for page in PAGES:
        lines.append(f"- [{page.h1}]({url}{page.path}): {description(page, summary)}")
    lines.append(f"- [The data behind every page (JSON)]({url}summary.json)")
    if summary.get("repository"):
        lines.append(f"- [Source code, methodology and records]({summary['repository']})")
    lines += [
        "",
        f"The figures and the records are under CC BY 4.0 ({DATA_LICENCE}): quote them with "
        f"a link to {url} and the date they were read. The code is under the MIT licence.",
    ]
    lines += ["", f"## Figures, last 30 days, as of {summary['as_of']}", ""]
    if not summary["providers"]:
        lines.append("No results are published yet.")
    for provider in summary["providers"]:
        window = provider["windows"]["30"]
        lines.append(
            f"- {provider['name']}: success rate {_percent(window['success'])}, resolved URLs "
            f"{_percent(window['resolved'])}, {step3_name(summary).lower()} "
            f"{_percent(window['poisoned'])}, median response time {_seconds(window['p50'])}, "
            f"published price {_price(summary, provider['id'], 100_000)} per 1,000 searches at "
            f"100,000 a month. Based on {window['tasks']} searches."
        )
    if summary["not_measured_yet"]:
        words = {"poisoned": "poisoned results", "usable": "usable answers"}
        missing = ", ".join(words.get(key, key) for key in summary["not_measured_yet"])
        lines += ["", f"Not measured yet: {missing}."]
    if summary["preview"]:
        lines += ["", "This is a preview before launch: the sample is small."]
    return "\n".join(lines) + "\n"


def robots_txt(summary: dict[str, Any], url: str) -> str:
    if summary["preview"]:  # a page that is not launched asks not to be crawled at all
        return "User-agent: *\nDisallow: /\n"
    groups = ["User-agent: *\nAllow: /\n"]
    groups += [f"User-agent: {agent}\nAllow: /\n" for agent in AI_CRAWLERS]
    return "\n".join(groups) + f"\nSitemap: {url}sitemap.xml\n"


def sitemap_xml(summary: dict[str, Any], url: str) -> str:
    day = summary["generated_at"][:10]
    entries = "".join(
        f"  <url><loc>{url}{page.path}</loc><lastmod>{day}</lastmod></url>\n" for page in PAGES
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + entries + "</urlset>\n"
    )


def build_site(summary: dict[str, Any], template: Path, out: Path, url: str) -> list[Path]:
    """Write the site into `out` and return the files written."""
    url = url.rstrip("/") + "/"
    source = template.read_text(encoding="utf-8")
    files = {f"{page.path}index.html": render(source, page, summary, url) for page in PAGES}
    files |= {
        "summary.json": json.dumps(summary, ensure_ascii=False, indent=1) + "\n",
        "llms.txt": llms_txt(summary, url),
        "robots.txt": robots_txt(summary, url),
        "sitemap.xml": sitemap_xml(summary, url),
        "404.html": (
            '<!doctype html><meta charset="utf-8"><meta name="robots" content="noindex">'
            f'<title>Not found</title><p>Nothing here. <a href="{url}">The benchmark</a>.</p>\n'
        ),
        ".nojekyll": "",  # GitHub Pages: serve the files as they are
    }
    host = urlparse(url).netloc
    if host and not host.endswith("github.io"):
        files["CNAME"] = host + "\n"  # GitHub Pages: the custom domain of the site
    written = []
    for name, content in files.items():
        path = out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)
    assets = template.parent / "assets"
    if assets.is_dir():
        shutil.copytree(assets, out / "assets", dirs_exist_ok=True)
        written += sorted(p for p in (out / "assets").iterdir() if p.is_file())
    return written
