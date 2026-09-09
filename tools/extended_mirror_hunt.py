#!/usr/bin/env python3
"""Run mirror_hunt with historical layouts and full capped mirror indexes."""

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

    for label, url in _original_candidate_urls(major):
        item = emit(label, url)
        if item:
            yield item

    abi = f"FreeBSD:{major}:amd64"
    for branch in (".latest", "latest", ".quarterly", "quarterly"):
        item = emit(
            f"Nepustil current {branch}",
            f"https://repo.nepustil.net/{abi}/{branch}/",
        )
        if item:
            yield item

    for snap in hunt.NEPUSTIL_SNAPSHOTS[major]:
        for branch in (".latest", "latest", ".quarterly", "quarterly"):
            item = emit(
                f"Nepustil snapshot {snap} {branch}",
                f"https://repo.nepustil.net/{snap}/{abi}/{branch}/",
            )
            if item:
                yield item


def source_entries(base_url: str):
    """Use packagesite only when the browsable listing is absent or capped.

    SGGS historical All/ views commonly stop at exactly 10,000 entries.  The
    packagesite catalogue is complete, so expand capped views from metadata.
    For ordinary mirrors keep the faster HTML path to avoid downloading dozens
    of multi-megabyte catalogues unnecessarily.
    """
    listing = hunt.html_index(base_url)
    if not listing:
        return hunt.metadata_index(base_url)
    if len(listing) >= 9990:
        metadata = hunt.metadata_index(base_url)
        if metadata:
            metadata.update({name: url for name, url in listing.items() if name not in metadata})
            return metadata
    return listing


hunt.candidate_urls = candidate_urls
hunt.source_entries = source_entries
raise SystemExit(hunt.main())
