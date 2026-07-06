"""Regression tests for partial-batch classification caching (audit fix #3).

The OpenAI backend used to pre-seed every item's result to [] and only fill in
the indices the model returned. Items the model omitted kept [] but still got
stamped roles_v=version in main() — cached as "definitively no role" forever,
never re-queued without --backfill. The fix: unreturned items come back as
None and are left UNSTAMPED so they re-queue next run. No network — httpx is
stubbed with a fake client that returns a partial classification.
"""
from __future__ import annotations

import json

import classify_items


ROLES = [
    {"id": "litigator", "description": "handles court cases"},
    {"id": "transactional", "description": "handles contracts"},
]


class _FakeResponse:
    def __init__(self, content_obj):
        self._payload = {
            "choices": [{"message": {"content": json.dumps(content_obj)}}]
        }

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """Stand-in for httpx.Client that returns a canned classification and
    records nothing else. `returned_indices` controls which item indices the
    'model' includes in its response (the rest are omitted → partial batch)."""

    def __init__(self, returned_indices, roles_for):
        self._returned = returned_indices
        self._roles_for = roles_for

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, headers=None, json=None):
        import json as _json

        # The item list is the user message content (a JSON string), per
        # classify_openai's request shape.
        user_content = json["messages"][1]["content"]
        payload = _json.loads(user_content)
        classifications = []
        for entry in payload:
            i = entry["i"]
            if i in self._returned:
                classifications.append({"i": i, "roles": self._roles_for.get(i, [])})
        return _FakeResponse({"classifications": classifications})


def _install_fake_client(monkeypatch, returned_indices, roles_for):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def _factory(*args, **kwargs):
        return _FakeClient(returned_indices, roles_for)

    monkeypatch.setattr(classify_items.httpx, "Client", _factory)


def test_omitted_items_come_back_as_none(monkeypatch):
    items = [{"title": f"item {n}", "summary": ""} for n in range(3)]
    # Model returns items 0 and 2, omits item 1.
    _install_fake_client(
        monkeypatch,
        returned_indices={0, 2},
        roles_for={0: ["litigator"], 2: []},
    )
    result = classify_items.classify_openai(items, ROLES)
    assert result is not None
    assert result[0] == ["litigator"]
    assert result[1] is None  # omitted → unstamped sentinel, NOT []
    assert result[2] == []  # explicitly returned as no-role


def test_returned_empty_differs_from_omitted(monkeypatch):
    """An item the model explicitly returns with no roles ([]) must be cached;
    only omitted items (None) stay unstamped. This is the crux of the fix."""
    items = [{"title": "a", "summary": ""}, {"title": "b", "summary": ""}]
    _install_fake_client(monkeypatch, returned_indices={0}, roles_for={0: []})
    result = classify_items.classify_openai(items, ROLES)
    assert result[0] == []  # returned, empty → will be cached
    assert result[1] is None  # omitted → will re-queue


def test_invalid_role_ids_are_filtered(monkeypatch):
    items = [{"title": "a", "summary": ""}]
    _install_fake_client(
        monkeypatch,
        returned_indices={0},
        roles_for={0: ["litigator", "not-a-real-role"]},
    )
    result = classify_items.classify_openai(items, ROLES)
    assert result[0] == ["litigator"]


def test_main_leaves_omitted_items_unstamped(monkeypatch, tmp_path):
    """End-to-end through main(): an item the model omits must NOT get
    roles_v stamped, so it re-queues; a returned item must get stamped."""
    import common

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(common, "STATE_DIR", state_dir)
    monkeypatch.setattr(classify_items, "STATE_DIR", state_dir)

    state = {
        "id": "src",
        "items": [
            {"id": "1", "link": "https://x/1", "title": "returned item", "summary": ""},
            {"id": "2", "link": "https://x/2", "title": "omitted item", "summary": ""},
        ],
    }
    (state_dir / "src.json").write_text(json.dumps(state), encoding="utf-8")

    # roles.yml taxonomy_version + a real role id are read from disk (the
    # backend filters out ids not in the taxonomy).
    version, real_roles = classify_items.load_taxonomy()
    a_real_role = real_roles[0]["id"]

    # Model returns index 0 only (item "1"); omits index 1 (item "2").
    _install_fake_client(monkeypatch, returned_indices={0}, roles_for={0: [a_real_role]})
    monkeypatch.setattr("sys.argv", ["classify_items.py", "--backend", "openai"])

    rc = classify_items.main()
    assert rc == 0

    written = json.loads((state_dir / "src.json").read_text())
    by_id = {it["id"]: it for it in written["items"]}
    # Returned item: stamped and tagged.
    assert by_id["1"].get("roles_v") == version
    assert by_id["1"].get("roles") == [a_real_role]
    # Omitted item: left UNSTAMPED so it re-queues next run.
    assert "roles_v" not in by_id["2"]
    assert "roles" not in by_id["2"]


def test_heuristic_backend_returns_every_item(monkeypatch):
    """The heuristic backend classifies in-process and must return a concrete
    (never None) result for every item — the None sentinel is openai-only."""
    items = [{"title": "consumer scam alert", "summary": "", "_category": ""}]
    roles = [{"id": "consumer", "keywords": ["scam", "consumer"], "categories": []}]
    result = classify_items.classify_heuristic(items, roles)
    assert result[0] is not None
