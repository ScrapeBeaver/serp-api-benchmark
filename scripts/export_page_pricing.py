"""Write the published-price curves into the benchmark page mock-up.

Reads `serp-bench curve --json` for each contract and replaces the block between
`/* pricing:start */` and `/* pricing:end */` in the page. The site itself will read the
same data from summary.json; this script exists so the mock-up shows real declared prices.

    uv run python scripts/export_page_pricing.py path/to/benchmark-page.html \
        providers/scrapebeaver/provider.yaml providers/_examples/scrapingdog.yaml ...
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from serp_bench.pricing import HEADLINE_VOLUMES, LADDER_VOLUMES

START, END = "/* pricing:start */", "/* pricing:end */"


def main(page: str, *contracts: str) -> int:
    command = ["serp-bench", "curve", "--json"]
    for contract in contracts:
        command += ["--file", contract]
    document = json.loads(
        subprocess.run(command, check=True, capture_output=True, text=True).stdout
    )

    providers = {}
    for provider_id, data in document["providers"].items():
        plans: list[str | None] = []
        points = []
        bills = {}  # for the table: the month's bill at each ladder volume, taken apart
        for point in data["points"]:
            if point["monthly_volume"] in LADDER_VOLUMES and point["price_per_1k"] is not None:
                bills[point["monthly_volume"]] = [
                    point["monthly_cost"],
                    point["plan_price"],
                    point["extra_cost"],
                    point["extra"],
                    point["formula"],
                ]
            plan = point.get("plan")
            if plan not in plans:
                plans.append(plan)
            price = point["price_per_1k"]
            points.append(
                [
                    point["monthly_volume"],
                    None if price is None else round(price, 4),
                    plans.index(plan),
                ]
            )
        providers[provider_id] = {
            "checked_at": data["checked_at"],
            "sources": data["sources"],
            "unit": data["unit"],
            "units_per_search": data["units_per_search"],
            "per_search": data["per_search"],
            "failed_request_charged": data["failed_request_charged"],
            "plans": plans,
            "points": points,
            "bills": bills,
        }
    block = {
        "date": document["date"],
        "volumes": list(HEADLINE_VOLUMES),
        "ladder": list(LADDER_VOLUMES),
        "providers": providers,
    }

    path = Path(page)
    text = path.read_text(encoding="utf-8")
    start, end = text.index(START), text.index(END)
    text = (
        text[: start + len(START)]
        + "\nconst PRICING = "
        + json.dumps(block, separators=(",", ":"))
        + ";\n"
        + text[end:]
    )
    path.write_text(text, encoding="utf-8")
    print(f"{path.name}: prices of {', '.join(providers)} as of {document['date']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
