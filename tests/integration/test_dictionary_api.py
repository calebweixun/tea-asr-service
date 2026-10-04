from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest
from conftest import AUTH, FakeSupervisor
from fastapi.testclient import TestClient

import tea_asr.context as context_module
from tea_asr.api.app import create_app
from tea_asr.config import AppPaths, ServiceConfig
from tea_asr.context import ContextDictionaryStore, ReplacementRule
from tea_asr.logs import read_recent_events, setup_logging


def _paths(root: Path) -> AppPaths:
    return AppPaths(support=root / "support", logs=root / "logs")


def _client(
    root: Path,
    *,
    remote_edit: bool = False,
    client_address: tuple[str, int] = ("127.0.0.1", 50000),
    raise_server_exceptions: bool = True,
) -> TestClient:
    paths = _paths(root)
    app = create_app(
        Path("unused"),
        token="test-token",
        supervisor=FakeSupervisor(),
        config=ServiceConfig(dictionary_remote_edit=remote_edit),
        paths=paths,
        vad_model=None,
        translation_provider=None,
        singing_runtime=None,
    )
    return TestClient(
        app,
        base_url="http://127.0.0.1",
        client=client_address,
        raise_server_exceptions=raise_server_exceptions,
    )


def _rules(domain: str = "Church") -> dict[str, Any]:
    return {
        "domain": domain,
        "hotwords": ["Bible", "prayer"],
        "replacements": [{"from": "foo", "to": "bar"}],
    }


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/v1/dictionaries", None),
        ("get", "/v1/dictionaries/church", None),
        ("put", "/v1/dictionaries/church", _rules()),
        ("delete", "/v1/dictionaries/church", None),
        ("post", "/v1/dictionaries/church/preview", {"text": "foo"}),
    ],
)
def test_every_dictionary_route_requires_bearer_auth(
    tmp_path: Path, method: str, path: str, body: dict[str, Any] | None
) -> None:
    with _client(tmp_path) as client:
        response = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


@pytest.mark.parametrize("method", ["put", "delete"])
def test_non_loopback_dictionary_writes_are_forbidden_by_default(
    tmp_path: Path, method: str
) -> None:
    paths = _paths(tmp_path)
    paths.dictionaries_dir.mkdir(parents=True)
    if method == "delete":
        (paths.dictionaries_dir / "church.toml").write_text(
            'domain = "Church"\nhotwords = []\n'
        )
    with _client(
        tmp_path, client_address=("192.0.2.10", 45000)
    ) as client:
        response = (
            client.put("/v1/dictionaries/church", json=_rules(), headers=AUTH)
            if method == "put"
            else client.delete("/v1/dictionaries/church", headers=AUTH)
        )
        malformed = (
            client.put(
                "/v1/dictionaries/church", json={"domain": "missing required rules"}, headers=AUTH
            )
            if method == "put"
            else None
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    if malformed is not None:
        assert malformed.status_code == 403
        assert malformed.json()["error"]["code"] == "forbidden"
    assert not (paths.dictionaries_dir / "church.toml").is_symlink()
    assert (paths.dictionaries_dir / "church.toml").exists() is (method == "delete")


def test_remote_edit_flag_allows_non_loopback_put_and_delete(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    with _client(
        tmp_path,
        remote_edit=True,
        client_address=("192.0.2.11", 45001),
    ) as client:
        created = client.put("/v1/dictionaries/church", json=_rules(), headers=AUTH)
        deleted = client.delete("/v1/dictionaries/church", headers=AUTH)

    assert created.status_code == 200
    assert deleted.status_code == 204
    assert list((paths.dictionaries_dir / ".history").glob("church-*.toml"))


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/v1/dictionaries/missing", None),
        ("delete", "/v1/dictionaries/missing", None),
        ("post", "/v1/dictionaries/missing/preview", {"text": "hello"}),
    ],
)
def test_missing_dictionary_routes_return_404(
    tmp_path: Path, method: str, path: str, body: dict[str, Any] | None
) -> None:
    with _client(tmp_path) as client:
        response = (
            getattr(client, method)(path, json=body, headers=AUTH)
            if body is not None
            else getattr(client, method)(path, headers=AUTH)
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_stale_revision_conflicts_with_current_revision_and_does_not_write(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        created = client.put("/v1/dictionaries/church", json=_rules(), headers=AUTH)
        revision = created.json()["revision"]
        stale = client.put(
            "/v1/dictionaries/church",
            json={**_rules("Updated"), "base_revision": "0" * 64},
            headers=AUTH,
        )
        current = client.get("/v1/dictionaries/church", headers=AUTH)

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "conflict"
    assert stale.json()["error"]["current_revision"] == revision
    assert current.json()["revision"] == revision


def test_revision_on_missing_file_conflicts_with_null_current_revision(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        response = client.put(
            "/v1/dictionaries/church",
            json={**_rules(), "base_revision": "stale"},
            headers=AUTH,
        )

    assert response.status_code == 409
    assert response.json()["error"]["current_revision"] is None


@pytest.mark.parametrize(
    ("name", "payload", "expected_field"),
    [
        (
            "church",
            {
                **_rules(),
                "replacements": [
                    {"from": "same", "to": "one"},
                    {"from": "same", "to": "two"},
                ],
            },
            "replacements.from",
        ),
        (
            "church",
            {**_rules(), "replacements": [{"from": "", "to": "target"}]},
            "replacements.from",
        ),
        ("church", {**_rules(), "domain": "d" * 301}, "domain"),
        ("church", {**_rules(), "hotwords": ["h"] * 201}, "hotwords"),
        (
            "church",
            {
                **_rules(),
                "replacements": [{"from": f"w{i}", "to": "x"} for i in range(501)],
            },
            "replacements",
        ),
        ("bad.name", _rules(), "name"),
        ("bad/name", _rules(), "name"),
    ],
)
def test_put_reports_validation_issues(
    tmp_path: Path, name: str, payload: dict[str, Any], expected_field: str
) -> None:
    with _client(tmp_path) as client:
        response = client.put(f"/v1/dictionaries/{name}", json=payload, headers=AUTH)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid"
    assert expected_field in {item["field"] for item in error["details"]}
    if expected_field == "replacements.from" and payload["replacements"][0]["from"] == "same":
        assert error["details"] == [
            {
                "field": "replacements.from",
                "index": 1,
                "message": "duplicate source; first used at index 0",
            }
        ]


def test_replacement_character_limits_and_preview_text_limit(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        bad_replacement = client.put(
            "/v1/dictionaries/church",
            json={
                **_rules(),
                "hotwords": ["h" * 33],
                "replacements": [{"from": "f" * 33, "to": "t" * 33}],
            },
            headers=AUTH,
        )
        text_too_long = client.post(
            "/v1/dictionaries/church/preview",
            json={"text": "x" * 2001, "dictionary": _rules()},
            headers=AUTH,
        )

    assert bad_replacement.status_code == 422
    assert {item["field"] for item in bad_replacement.json()["error"]["details"]} == {
        "hotwords",
        "replacements.from",
        "replacements.to",
    }
    assert text_too_long.status_code == 422
    assert text_too_long.json()["error"]["details"][0]["field"] == "text"


def test_invalid_file_stays_in_list_with_metadata_and_detail_returns_parse_error(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    paths.dictionaries_dir.mkdir(parents=True)
    raw = b"not = [toml"
    (paths.dictionaries_dir / "broken.toml").write_bytes(raw)
    with _client(tmp_path) as client:
        listing = client.get("/v1/dictionaries", headers=AUTH)
        detail = client.get("/v1/dictionaries/broken", headers=AUTH)

    summary = listing.json()[0]
    assert summary["name"] == "broken"
    assert summary["error"]
    assert summary["revision"] == hashlib.sha256(raw).hexdigest()
    assert summary["updated_at"].endswith("Z")
    assert detail.status_code == 422
    assert detail.json()["error"]["details"][0]["field"] == "file"


def test_write_is_atomic_when_replace_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _paths(tmp_path)
    paths.dictionaries_dir.mkdir(parents=True)
    target = paths.dictionaries_dir / "church.toml"
    target.write_text('domain = "Original"\nhotwords = []\n')
    original = target.read_bytes()
    real_replace = os.replace

    def fail_target_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        if Path(destination) == target:
            raise OSError("simulated rename failure")
        real_replace(source, destination)

    monkeypatch.setattr(context_module.os, "replace", fail_target_replace)
    with _client(tmp_path, raise_server_exceptions=False) as client:
        response = client.put("/v1/dictionaries/church", json=_rules(), headers=AUTH)

    assert response.status_code == 500
    assert target.read_bytes() == original
    assert not list(paths.dictionaries_dir.glob(".church-*.tmp"))
    archived = list((paths.dictionaries_dir / ".history").glob("church-*.toml"))
    assert len(archived) == 1
    assert archived[0].read_bytes() == original


def test_update_writes_canonical_toml_and_preserves_original_in_history(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    paths.dictionaries_dir.mkdir(parents=True)
    target = paths.dictionaries_dir / "church.toml"
    original = (
        b"# Hand edited comment\n"
        b'domain = "Old domain"\n'
        b'hotwords = ["Old word"]\n'
    )
    target.write_bytes(original)
    revision = hashlib.sha256(original).hexdigest()
    with _client(tmp_path) as client:
        response = client.put(
            "/v1/dictionaries/church",
            json={**_rules("New domain"), "base_revision": revision},
            headers=AUTH,
        )

    canonical = target.read_text()
    assert response.status_code == 200
    assert canonical.splitlines() == [
        "# TEA ASR dictionary, written by the server API.",
        'domain = "New domain"',
        'hotwords = ["Bible", "prayer"]',
        "",
        "[[replacements]]",
        'from = "foo"',
        'to = "bar"',
    ]
    archived = list((paths.dictionaries_dir / ".history").glob("church-*.toml"))
    assert len(archived) == 1
    assert archived[0].read_bytes() == original
    assert response.json()["revision"] == hashlib.sha256(target.read_bytes()).hexdigest()


def test_history_keeps_newest_thirty_versions_per_dictionary(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        current = client.put("/v1/dictionaries/church", json=_rules("0"), headers=AUTH).json()
        for index in range(1, 32):
            current = client.put(
                "/v1/dictionaries/church",
                json={
                    **_rules(str(index)),
                    "base_revision": current["revision"],
                },
                headers=AUTH,
            ).json()

    history = _paths(tmp_path).dictionaries_dir / ".history"
    archived = list(history.glob("church-*.toml"))
    assert len(archived) == 30
    archived_domains = {line for path in archived for line in path.read_text().splitlines() if line.startswith("domain = ")}
    assert 'domain = "0"' not in archived_domains
    assert 'domain = "30"' in archived_domains


def test_delete_soft_moves_file_to_history_and_returns_no_body(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    setup_logging(paths)
    with _client(tmp_path) as client:
        client.put("/v1/dictionaries/church", json=_rules(), headers=AUTH)
        response = client.delete("/v1/dictionaries/church", headers=AUTH)

    assert response.status_code == 204
    assert response.content == b""
    assert not (paths.dictionaries_dir / "church.toml").exists()
    archived = list((paths.dictionaries_dir / ".history").glob("church-*.toml"))
    assert len(archived) == 1
    assert 'domain = "Church"' in archived[0].read_text()
    log_text = paths.log_file.read_text()
    logs = read_recent_events(paths.log_file, limit=20, backup_count=3)
    saved_event = next(item for item in logs if item["message"] == "dictionary.saved")
    deleted_event = next(item for item in logs if item["message"] == "dictionary.deleted")
    assert saved_event["name"] == "church"
    assert saved_event["hotwords_count"] == 2
    assert saved_event["replacements_count"] == 1
    assert len(saved_event["revision"]) == 64
    assert deleted_event["name"] == "church"
    assert deleted_event["hotwords_count"] == 2
    assert deleted_event["replacements_count"] == 1
    assert len(deleted_event["revision"]) == 64
    assert "Church" not in log_text
    assert "Bible" not in log_text


def test_saved_and_draft_preview_use_server_replacement_rules(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        saved = client.put(
            "/v1/dictionaries/church",
            json={
                "domain": "",
                "hotwords": [],
                "replacements": [
                    {"from": "ab", "to": "X"},
                    {"from": "a", "to": "Y"},
                ],
            },
            headers=AUTH,
        )
        saved_preview = client.post(
            "/v1/dictionaries/church/preview",
            json={"text": "ab a"},
            headers=AUTH,
        )
        draft_preview = client.post(
            "/v1/dictionaries/church/preview",
            json={
                "text": "ab a",
                "dictionary": {
                    "domain": "draft",
                    "hotwords": [],
                    "replacements": [{"from": "a", "to": "Z"}],
                },
            },
            headers=AUTH,
        )

    assert saved.status_code == 200
    assert saved_preview.json() == {
        "text": "X Y",
        "applied": [
            {"from": "ab", "to": "X", "count": 1},
            {"from": "a", "to": "Y", "count": 1},
        ],
    }
    assert draft_preview.json() == {
        "text": "Zb Z",
        "applied": [{"from": "a", "to": "Z", "count": 2}],
    }


def test_put_get_round_trip_and_loaded_dictionaries_are_snapshots(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = ContextDictionaryStore(paths.dictionaries_dir)
    with _client(tmp_path) as client:
        created = client.put("/v1/dictionaries/church", json=_rules(), headers=AUTH)
        first_loaded = store.load("church")
        changed = client.put(
            "/v1/dictionaries/church",
            json={
                "domain": "Updated Church",
                "hotwords": ["sermon"],
                "replacements": [{"from": "foo", "to": "worship"}],
                "base_revision": created.json()["revision"],
            },
            headers=AUTH,
        )
        read_back = client.get("/v1/dictionaries/church", headers=AUTH)
        second_loaded = store.load("church")

    assert changed.status_code == 200
    assert read_back.json() == changed.json()
    assert first_loaded.domain == "Church"
    assert first_loaded.hotwords == ("Bible", "prayer")
    assert first_loaded.replacements == (ReplacementRule("foo", "bar"),)
    assert second_loaded.domain == "Updated Church"
    assert second_loaded.hotwords == ("sermon",)
    assert second_loaded.replacements == (ReplacementRule("foo", "worship"),)


def test_remote_edit_flag_loads_from_service_config(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.support.mkdir(parents=True)
    paths.config_file.write_text("[service]\ndictionary_remote_edit = true\n")

    assert ServiceConfig().dictionary_remote_edit is False
    assert ServiceConfig.load(paths, env={}).dictionary_remote_edit is True
    assert ServiceConfig.from_env({"TEA_ASR_DICTIONARY_REMOTE_EDIT": "1"}).dictionary_remote_edit
