"""Page views for the catalog web UI: a page as named live regions plus what it watches.

A page builder returns a ``PageView``: the page body (with its live parts
wrapped by ``LiveRegions.region``), the HTML of each live region, and the
catalog entities the page depends on (a ``catalog_changes.Watch``). The same
view renders either as a full HTML page or, for the live updater, as a JSON
map of region key to region HTML, so a region swapped in place is byte for byte
what a reload would have shown.

Static parts of a page (search and filter forms, notices from the query
string, the Anki sync panel, settings forms) are simply not wrapped: the live
updater never touches them, so typing in them is never interrupted.
"""

from __future__ import annotations

import hashlib
import html
from collections.abc import Mapping
from dataclasses import dataclass, field

from catalog_changes import Watch

LIVE_REGION_CLASS = "live-region"
# The language bar sits in the page header (``layout``), not in the body, but
# its per-language counts change with the catalog like any region.
LANGUAGE_BAR_REGION = "language-bar"


def region_hash(markup: str) -> str:
    return hashlib.sha1(markup.encode("utf-8")).hexdigest()[:16]


def wrap_region(key: str, markup: str) -> str:
    return (
        f'<div class="{LIVE_REGION_CLASS}" data-live-region="{html.escape(key, quote=True)}"'
        f' data-live-hash="{region_hash(markup)}">{markup}</div>'
    )


@dataclass
class LiveRegions:
    """Collects a page's live regions while its body is composed.

    ``region`` returns the wrapped markup to embed in the body and records the
    inner markup under ``key``. The wrapper is ``display: contents``, so it does
    not change the page layout.
    """

    regions: dict[str, str] = field(default_factory=dict)

    def region(self, key: str, markup: str) -> str:
        if key in self.regions or key == LANGUAGE_BAR_REGION:
            raise ValueError(f"Duplicate live region key: {key}")
        self.regions[key] = markup
        return wrap_region(key, markup)


@dataclass(frozen=True)
class PageView:
    title: str
    active_path: str
    body: str
    lang: str
    watch: Watch = field(default_factory=Watch)
    regions: Mapping[str, str] = field(default_factory=dict)
    language_bar: str = ""


    def live_regions(self) -> dict[str, str]:
        """Every region the live updater may swap: the body's plus the language bar."""
        if not self.language_bar:
            return dict(self.regions)
        return {**self.regions, LANGUAGE_BAR_REGION: self.language_bar}

    def header_language_bar(self) -> str:
        """The language bar as ``layout`` should embed it."""
        return wrap_region(LANGUAGE_BAR_REGION, self.language_bar) if self.language_bar else ""


def fragments_payload(view: PageView, revision: int) -> dict[str, object]:
    """What the live updater fetches: every live region with its hash."""
    return {
        "revision": revision,
        "watch": view.watch.encode(),
        "regions": {
            key: {"hash": region_hash(markup), "html": markup}
            for key, markup in view.live_regions().items()
        },
    }
