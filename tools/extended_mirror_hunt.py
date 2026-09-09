#!/usr/bin/env python3
"""Run mirror_hunt with the historical Nepustil tree layouts included.

Nepustil keeps many archived package trees below hidden `.latest` / `.quarterly`
directories.  The original hunter checked snapshot roots but not all of those
layouts, which left valid same-ABI database packages undiscovered.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mirror_hunt as hunt  # noqa: E402

_original_candidate_urls = hunt.candidate_urls


def candidate_urls(major: int):
    seen = set()

    def emit(label: str, url: str):
        if url in seen:
            return None
        seen.add(url)
        return label, url

    # Keep every source already known by mirror_hunt.
    for label, url in _original_candidate_urls(major):
        item = emit(label, url)
        if item:
            yield item

    abi = f"FreeBSD:{major}:amd64"

    # Current Nepustil trees can also be exposed through hidden branch aliases.
    for branch in (".latest", "latest", ".quarterly", "quarterly"):
        item = emit(
            f"Nepustil current {branch}",
            f"https://repo.nepustil.net/{abi}/{branch}/",
        )
        if item:
            yield item

    # Historical snapshots.  This is the important missing layout: for example
    # FreeBSD 11.2 packages live under /112/FreeBSD:11:amd64/.latest/All/.
    for snap in hunt.NEPUSTIL_SNAPSHOTS[major]:
        for branch in (".latest", "latest", ".quarterly", "quarterly"):
            item = emit(
                f"Nepustil snapshot {snap} {branch}",
                f"https://repo.nepustil.net/{snap}/{abi}/{branch}/",
            )
            if item:
                yield item


hunt.candidate_urls = candidate_urls
raise SystemExit(hunt.main())
