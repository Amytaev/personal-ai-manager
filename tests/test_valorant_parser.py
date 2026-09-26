from __future__ import annotations

from agents.valorant.parser import parse_reset_time_left, parse_storefront_items


def _card(uuid: str, name: str, price: int, image: str = "https://cdn-kr.stackb.net/x.png") -> str:
    """A minimal HTML snippet shaped like one real skin-card, as
    confirmed against a real captured Stack B /riot/storefront Livewire
    response (Phase 6.2 investigation) - only the attributes
    agents/valorant/parser.py's regex actually anchors on, with the
    same surrounding noise (Blade's if-BLOCK comments, the tier <img>
    before the name) real cards contain."""
    return f"""
    <div
        role="button"
        tabindex="0"
        onclick="window.openValorantItem('{uuid}')"
        onkeydown="if (event.key === 'Enter' || event.key === ' ') {{ event.preventDefault(); window.openValorantItem('{uuid}') }}"
        style="--tier: 0 203 179"
        class="skin-card flex flex-col bg-green-50 dark:bg-green-900/20 rounded-3xl relative cursor-pointer"
    >
        <div class="storefront-image flex h-24 items-center justify-center p-3">
            <img
                    src="{image}"
                    alt="{name}"
                    loading="lazy"
                    decoding="async"
                    class="relative z-10 h-full w-full object-contain"
                >
        </div>
        <div class="relative z-10 p-4">
            <h3 class="storefront-title flex items-center gap-x-1">
                <!--[if BLOCK]><![endif]-->
                    <img class="w-6" src="/tier.png" alt="">
                <!--[if ENDBLOCK]><![endif]-->
                {name}
            </h3>
            <!--[if BLOCK]><![endif]-->
                <p class="mt-3 flex items-center gap-x-1 text-xs font-semibold uppercase">
                    <!--[if BLOCK]><![endif]-->
                        {price}
                        <img class="w-4 invert dark:invert-0" src="/vp.png" alt="VP">
                    <!--[if ENDBLOCK]><![endif]-->
                </p>
            <!--[if ENDBLOCK]><![endif]-->
        </div>
    </div>
    """


def test_parse_storefront_items_extracts_all_real_shaped_cards():
    html = "".join(
        [
            _card("88f1bcbd-4dfd-f2ef-8a2c-44b3baa26b3c", "Апертура", 1275),
            _card("72b3bacc-48ac-85f7-ec38-5ab629654486", "Куронами", 2375),
            _card("857ad950-486a-d199-7bf2-7092aaf88ae2", "Чаропанк", 1775),
            _card("4049dc82-495b-cecc-8e34-4dbf35753129", "Пустошь", 1275),
        ]
    )

    items = parse_storefront_items(html)

    assert len(items) == 4
    by_uuid = {item["uuid"]: item for item in items}
    assert by_uuid["88f1bcbd-4dfd-f2ef-8a2c-44b3baa26b3c"]["name"] == "Апертура"
    assert by_uuid["88f1bcbd-4dfd-f2ef-8a2c-44b3baa26b3c"]["price_vp"] == 1275
    assert by_uuid["72b3bacc-48ac-85f7-ec38-5ab629654486"]["price_vp"] == 2375


def test_parse_storefront_items_returns_empty_list_for_unrelated_html():
    # No skin-card markup at all - e.g. the bare page shell before the
    # Livewire "storefront" component has loaded any items. This is the
    # case agents/valorant/agent.py turns into VALORANT_STORE_PARSE_ERROR.
    assert parse_storefront_items("<html><body>Loading...</body></html>") == []


def test_parse_storefront_items_returns_empty_list_for_empty_string():
    assert parse_storefront_items("") == []


def test_parse_storefront_items_deduplicates_repeated_uuid_reference():
    # Every real card references its own uuid twice (onclick + onkeydown) -
    # the regex only ever matches the first (onclick) span per card, but
    # this proves a genuine duplicate uuid still doesn't produce two rows.
    html = _card("dad0000d-0000-0000-0000-000000000000", "Дубликат", 999)
    html += html  # simulate the same card appearing twice in the page

    items = parse_storefront_items(html)
    assert len(items) == 1


def test_parse_storefront_items_falls_back_to_placeholder_name_when_alt_is_empty():
    html = _card("00000000-0000-0000-0000-000000000000", "", 500)
    items = parse_storefront_items(html)
    assert items[0]["name"] == "(без названия)"


def test_parse_reset_time_left_returns_the_string_when_present():
    assert parse_reset_time_left({"timeLeft": "7 часов и 49 минут"}) == "7 часов и 49 минут"


def test_parse_reset_time_left_returns_none_when_missing_or_wrong_type():
    assert parse_reset_time_left({}) is None
    assert parse_reset_time_left({"timeLeft": None}) is None
    assert parse_reset_time_left({"timeLeft": 123}) is None
    assert parse_reset_time_left("not a dict") is None
