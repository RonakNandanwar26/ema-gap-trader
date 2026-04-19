"""Unit tests for trader_daemon.wanted_store."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from trader_daemon.wanted_store import WantedStore


def test_load_missing_file_returns_empty_dict(tmp_path: Path) -> None:
    store = WantedStore(tmp_path / "wanted.json")
    assert store.load() == {}


def test_add_and_load_roundtrip(tmp_path: Path) -> None:
    store = WantedStore(tmp_path / "wanted.json")
    payload = {"mode": "paper", "instrument": "NIFTY", "variant": "", "capital": 100000,
               "strategy": {"ema_short": 9}, "instrument_config": {}}
    store.add("paper:NIFTY", payload)
    loaded = store.load()
    assert loaded == {"paper:NIFTY": payload}


def test_remove_existing_key(tmp_path: Path) -> None:
    store = WantedStore(tmp_path / "wanted.json")
    store.add("a", {"x": 1})
    store.add("b", {"x": 2})
    store.remove("a")
    assert store.load() == {"b": {"x": 2}}


def test_remove_nonexistent_key_is_noop(tmp_path: Path) -> None:
    store = WantedStore(tmp_path / "wanted.json")
    store.add("a", {"x": 1})
    store.remove("nonexistent")
    assert store.load() == {"a": {"x": 1}}


def test_clear(tmp_path: Path) -> None:
    store = WantedStore(tmp_path / "wanted.json")
    store.add("a", {"x": 1})
    store.clear()
    assert store.load() == {}


def test_atomic_write_survives_corrupted_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "wanted.json"
    path.write_text("{ this is not valid json")
    store = WantedStore(path)
    # load() on corrupt file should return empty without raising
    assert store.load() == {}
    # a subsequent add should overwrite the corrupt content cleanly
    store.add("a", {"x": 1})
    assert json.loads(path.read_text()) == {"a": {"x": 1}}


def test_load_non_object_json_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "wanted.json"
    path.write_text('["not", "an", "object"]')
    store = WantedStore(path)
    assert store.load() == {}


def test_concurrent_adds_do_not_corrupt_file(tmp_path: Path) -> None:
    """Hammer add() from multiple threads; final state must contain all entries."""
    store = WantedStore(tmp_path / "wanted.json")
    keys = [f"session-{i}" for i in range(50)]

    def worker(key: str) -> None:
        store.add(key, {"n": int(key.split("-")[1])})

    threads = [threading.Thread(target=worker, args=(k,)) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    loaded = store.load()
    assert set(loaded.keys()) == set(keys)
    for k, v in loaded.items():
        assert v == {"n": int(k.split("-")[1])}


def test_parent_directory_is_created(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "wanted.json"
    store = WantedStore(path)
    store.add("a", {"x": 1})
    assert path.exists()
    assert json.loads(path.read_text()) == {"a": {"x": 1}}
