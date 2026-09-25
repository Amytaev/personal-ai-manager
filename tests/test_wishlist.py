from __future__ import annotations

from storage.wishlist import WishlistStore


def test_new_wishlist_is_empty(tmp_path):
    store = WishlistStore(tmp_path / "wishlist.json")
    assert store.list() == []


def test_add_returns_true_and_persists(tmp_path):
    path = tmp_path / "wishlist.json"
    store = WishlistStore(path)

    assert store.add("Reaver Vandal") is True
    assert store.list() == ["Reaver Vandal"]

    # A fresh instance reading the same file sees the same data.
    reloaded = WishlistStore(path)
    assert reloaded.list() == ["Reaver Vandal"]


def test_add_is_case_insensitive_dedup(tmp_path):
    store = WishlistStore(tmp_path / "wishlist.json")
    store.add("Reaver Vandal")

    assert store.add("reaver vandal") is False
    assert store.list() == ["Reaver Vandal"]


def test_add_rejects_blank_input(tmp_path):
    store = WishlistStore(tmp_path / "wishlist.json")
    assert store.add("   ") is False
    assert store.list() == []


def test_remove_returns_true_when_present(tmp_path):
    store = WishlistStore(tmp_path / "wishlist.json")
    store.add("Prime Vandal")

    assert store.remove("prime vandal") is True  # case-insensitive removal
    assert store.list() == []


def test_remove_returns_false_when_absent(tmp_path):
    store = WishlistStore(tmp_path / "wishlist.json")
    assert store.remove("Ion Sheriff") is False


def test_corrupt_file_is_treated_as_empty_not_a_crash(tmp_path):
    path = tmp_path / "wishlist.json"
    path.write_text("{not valid json", encoding="utf-8")

    store = WishlistStore(path)
    assert store.list() == []
