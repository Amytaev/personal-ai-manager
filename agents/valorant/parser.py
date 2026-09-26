"""Parse Stack B's /riot/storefront Livewire response into skin rows (Phase 6).

Written against REAL captured data, not a guessed shape -
scripts/valorant_capture_store.py captured a real logged-in session's
network traffic (valorant_capture/responses.jsonl) before this file was
written. Two real findings that shaped this module, both confirmed
against that capture:

1. stackb.net has no JSON API for the store. A plain GET
   /riot/storefront returns only the page shell (empty Livewire
   component). The actual daily items arrive in a SEPARATE POST
   /livewire/update response, fired automatically on page load by the
   "storefront" Livewire component's own wire:init="getDailyItems()" -
   and that response's `effects.html` is a fully server-rendered HTML
   fragment, not structured JSON (its `snapshot.data.dailyItems` field
   is just internal DB row IDs, not the item data itself - agents/
   valorant/agent.py is the module that captures this specific
   response; this module only parses the HTML it hands over).
2. Each daily item is one `<div class="skin-card" ...>` card with a
   real UUID (used elsewhere as /valorant/item/<uuid>), an <img alt=...>
   for the display name, and a price directly followed by a VP icon
   <img alt="VP">. Confirmed against 4 real items from one real capture
   (see the project's Phase 6.2 chat history for the exact values).

HONEST LIMITATION: this is an undocumented, unversioned internal
backend for Stack B's own web client, not a public API - it can change
shape at any time without notice. Parsing here is intentionally
defensive for exactly that reason: one malformed/unexpected card is
just skipped rather than crashing the whole run, but
agents/valorant/agent.py treats "zero items parsed at all" as a hard
VALORANT_STORE_PARSE_ERROR (not a silently empty store) - that's the
real signal Stack B's markup changed, as opposed to normal store
variation.
"""
from __future__ import annotations

import re
from html import unescape

# Anchored on the real markup confirmed in Phase 6.2 (see this module's
# docstring): each card's `onclick` carries the item's UUID, the card
# div itself carries class="skin-card", the display image's `alt` is
# the skin's display name, and the price is the first number before the
# VP icon's <img loading="lazy"> tag that follows it, directly inside a
# <p class="mt-3 ..."> wrapper. `re.S` so `.` also matches the newlines
# Stack B's server-rendered HTML is full of.
_ITEM_CARD_RE = re.compile(
    r"onclick=\"window\.openValorantItem\('(?P<uuid>[0-9a-fA-F-]{36})'\)\""
    r".*?class=\"skin-card[^\"]*\""
    r".*?src=\"(?P<image>[^\"]+)\"\s*"
    r"alt=\"(?P<name>[^\"]*)\"\s*"
    r"loading=\"lazy\""
    r".*?<p class=\"mt-3[^\"]*\">"
    r"\s*(?:<!--\[if BLOCK\]><!\[endif\]-->)?\s*"
    r"(?P<price>\d+)",
    re.S,
)


def parse_storefront_items(html: str) -> list[dict]:
    """Extract the daily store's skin cards from a Stack B
    /riot/storefront Livewire "storefront" component response's
    rendered HTML fragment (agents/valorant/agent.py captures and hands
    over that fragment; this function does no network I/O of its own).

    Returns [] if nothing matched at all - agents/valorant/agent.py is
    the layer that decides an all-empty result is a hard parse error
    (VALORANT_STORE_PARSE_ERROR) rather than "no items today", since a
    real VALORANT daily store is never actually empty.
    """
    if not html:
        return []

    items: list[dict] = []
    seen_uuids: set[str] = set()
    for match in _ITEM_CARD_RE.finditer(html):
        uuid = match.group("uuid")
        # Each real card references its own UUID twice in the markup
        # (onclick + onkeydown, confirmed in Phase 6.2's capture) - this
        # regex only ever anchors on the first (onclick) occurrence per
        # card, so a duplicate here would mean something odd rather than
        # the expected shape. Skipped defensively either way, per this
        # module's docstring.
        if uuid in seen_uuids:
            continue
        seen_uuids.add(uuid)

        try:
            price_vp = int(match.group("price"))
        except (TypeError, ValueError):
            price_vp = None

        name = unescape(match.group("name") or "").strip()
        items.append(
            {
                "uuid": uuid,
                "name": name or "(без названия)",
                "price_vp": price_vp,
                "image_url": match.group("image"),
            }
        )
    return items


def parse_reset_time_left(snapshot_data: dict) -> str | None:
    """Pulls the human-readable reset countdown Stack B's own Livewire
    snapshot already computes server-side (e.g. "7 часов и 49 минут" -
    a real value seen in Phase 6.2's capture), instead of trying to
    compute a reset time ourselves from nothing. Returns None if the
    field is missing/not a string - a missing countdown shouldn't be
    fatal, callers just won't have one to show."""
    value = snapshot_data.get("timeLeft") if isinstance(snapshot_data, dict) else None
    return value if isinstance(value, str) and value.strip() else None
