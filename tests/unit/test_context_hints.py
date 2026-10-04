from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

import pytest
from conftest import AUTH, FakeSupervisor, build_client
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from tea_asr.config import AppPaths
from tea_asr.context import (
    MAX_CONTEXT_DICTIONARY_BYTES,
    ContextDictionaryStore,
    InvalidDictionary,
    ReplacementRule,
    UnknownDictionaryProfile,
    apply_replacements,
    resolve_context,
)
from tea_asr.wire import ContextEcho, ContextOptions, ContextReplacement


def _dictionary_text(
    domain: str,
    hotwords: list[str],
    replacements: list[tuple[str, str]] = (),
) -> str:
    text = f"domain = {json.dumps(domain, ensure_ascii=False)}\n"
    text += f"hotwords = {json.dumps(hotwords, ensure_ascii=False)}\n"
    for source, target in replacements:
        text += "\n[[replacements]]\n"
        text += f"from = {json.dumps(source, ensure_ascii=False)}\n"
        text += f"to = {json.dumps(target, ensure_ascii=False)}\n"
    return text


def _session_start(context: dict[str, object]) -> dict[str, object]:
    return {
        "type": "session.start",
        "request_id": "context-test",
        "profile": "utterance",
        "audio": {"sample_rate": 16_000, "channels": 1, "format": "pcm_s16le"},
        "context": context,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("domain", "x" * 301),
        ("hotwords", ["x"] * 201),
        ("hotwords", [""]),
        ("hotwords", ["x" * 33]),
        ("replacements", [{"from": "x", "to": "y"}] * 501),
        ("replacements", [{"from": "", "to": ""}]),
        ("replacements", [{"from": "x" * 33, "to": ""}]),
        ("replacements", [{"from": "x", "to": "y" * 33}]),
    ],
)
def test_context_rejects_every_over_limit_value(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        ContextOptions.model_validate({field: value})


def test_context_accepts_all_exact_limits_and_empty_replacement_target() -> None:
    context = ContextOptions.model_validate(
        {
            "domain": "x" * 300,
            "hotwords": ["w" * 32] * 200,
            "replacements": [{"from": "f" * 32, "to": "t" * 32}] * 500,
        }
    )
    assert len(context.domain or "") == 300
    assert len(context.hotwords or ()) == 200
    assert len(context.replacements or ()) == 500
    assert ContextReplacement.model_validate({"from": "delete", "to": ""}).to == ""


@pytest.mark.parametrize(
    "payload",
    [
        {"unknown": "value"},
        {"replacements": [{"from": "x", "to": "y", "extra": True}]},
    ],
)
def test_context_and_replacement_reject_unknown_keys(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ContextOptions.model_validate(payload)


def test_merge_is_profile_first_with_inline_overrides_and_ordered_hotwords(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "dictionaries"
    directory.mkdir()
    (directory / "church.example.toml").write_text(
        _dictionary_text(
            "profile domain",
            ["聖經", "禱告", "聖經"],
            [("盛家", "聖經"), ("神話語", "神的話語")],
        )
    )
    request = ContextOptions.model_validate(
        {
            "profile": "church.example",
            "domain": "inline domain",
            "hotwords": ["禱告", "福音"],
            "replacements": [{"from": "盛家", "to": "Bible"}, {"from": "聖經", "to": "聖經書卷"}],
        }
    )

    plan = resolve_context(request, ContextDictionaryStore(directory))

    assert plan.profile == "church.example"
    assert plan.domain == "inline domain"
    assert plan.hotwords == ("聖經", "禱告", "福音")
    assert [(rule.source, rule.target) for rule in plan.replacements] == [
        ("盛家", "Bible"),
        ("神話語", "神的話語"),
        ("聖經", "聖經書卷"),
    ]
    assert "inline domain" in (plan.system_prompt or "")


def test_hotword_merge_truncates_and_reports_the_count(tmp_path: Path) -> None:
    directory = tmp_path / "dictionaries"
    directory.mkdir()
    profile_words = [f"p{index:03}" for index in range(200)]
    (directory / "many.toml").write_text(_dictionary_text("domain", profile_words))
    request = ContextOptions.model_validate({"profile": "many", "hotwords": ["inline-a", "inline-b"]})

    plan = resolve_context(request, ContextDictionaryStore(directory))

    assert len(plan.hotwords) == 200
    assert plan.hotwords[:2] == ("p000", "p001")
    assert plan.hotwords_truncated == 2


def test_profile_cache_reloads_after_mtime_changes(tmp_path: Path) -> None:
    directory = tmp_path / "dictionaries"
    directory.mkdir()
    path = directory / "church.toml"
    path.write_text(_dictionary_text("first", ["耶穌"]))
    store = ContextDictionaryStore(directory)
    first = store.load("church")

    path.write_text(_dictionary_text("second", ["基督"]))
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    second = store.load("church")

    assert first.domain == "first"
    assert second.domain == "second"
    assert second is not first


def test_unknown_and_invalid_profiles_are_rejected(tmp_path: Path) -> None:
    store = ContextDictionaryStore(tmp_path / "dictionaries")
    with pytest.raises(UnknownDictionaryProfile):
        resolve_context(ContextOptions.model_validate({"profile": "missing"}), store)

    store.directory.mkdir()
    (store.directory / "broken.toml").write_text("not = [toml")
    with pytest.raises(InvalidDictionary):
        store.load("broken")


def test_dictionary_summaries_report_invalid_files_without_hiding_valid_ones(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    paths = _paths(tmp_path)
    directory = paths.dictionaries_dir
    directory.mkdir(parents=True)
    (directory / "valid.toml").write_text(_dictionary_text("valid domain", ["word"]))
    (directory / "invalid.toml").write_text("secret-content = [\n")
    (directory / "oversized.toml").write_text(
        "secret-content = \"" + "x" * MAX_CONTEXT_DICTIONARY_BYTES + "\"\n"
    )
    outside = tmp_path / "outside.toml"
    outside.write_text(_dictionary_text("outside domain", ["private"]))
    (directory / "escaping.toml").symlink_to(outside)

    with (
        build_client(FakeSupervisor(), context_hints_enabled=True, paths=paths) as client,
        caplog.at_level(logging.WARNING, logger="tea_asr.context"),
    ):
        response = client.get("/v1/dictionaries", headers=AUTH)

    assert response.status_code == 200
    summaries = {item["name"]: item for item in response.json()}
    assert {
        key: summaries["valid"][key]
        for key in ("name", "domain", "hotwords_count", "replacements_count")
    } == {
        "name": "valid",
        "domain": "valid domain",
        "hotwords_count": 1,
        "replacements_count": 0,
    }
    assert len(summaries["valid"]["revision"]) == 64
    assert summaries["valid"]["updated_at"].endswith("Z")
    assert set(summaries) == {"escaping", "invalid", "oversized", "valid"}
    assert set(summaries["invalid"]) == {"name", "error", "revision", "updated_at"}
    assert set(summaries["oversized"]) == {"name", "error", "revision", "updated_at"}
    assert set(summaries["escaping"]) == {"name", "error", "revision", "updated_at"}
    assert len(summaries["invalid"]["revision"]) == 64
    assert summaries["escaping"]["revision"] is None
    assert "invalid TOML" in summaries["invalid"]["error"]
    assert "1 MiB" in summaries["oversized"]["error"]
    assert "escapes dictionaries directory" in summaries["escaping"]["error"]

    invalid_events = [
        record
        for record in caplog.records
        if record.message == "context.dictionary_invalid"
    ]
    assert len(invalid_events) == 3
    assert {record.fields["name"] for record in invalid_events} == {
        "escaping",
        "invalid",
        "oversized",
    }
    assert all(record.levelno == logging.WARNING for record in invalid_events)
    assert all(set(record.fields) == {"name", "reason"} for record in invalid_events)
    assert {record.fields["name"]: record.fields["reason"] for record in invalid_events} == {
        name: summaries[name]["error"] for name in ("escaping", "invalid", "oversized")
    }
    assert "secret-content" not in caplog.text
    assert "private" not in caplog.text


def test_dictionary_summaries_reflect_file_mutations(tmp_path: Path) -> None:
    store = ContextDictionaryStore(tmp_path / "dictionaries")
    store.directory.mkdir()
    path = store.directory / "changing.toml"
    path.write_text(_dictionary_text("before mutation", ["word"]))

    before = store.summaries()[0]
    assert before["name"] == "changing"
    assert before["domain"] == "before mutation"
    assert before["hotwords_count"] == 1
    assert before["replacements_count"] == 0
    assert before["revision"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert str(before["updated_at"]).endswith("Z")

    path.write_text("secret-after-mutation = [\n")
    invalid = store.summaries()
    assert invalid[0]["name"] == "changing"
    assert invalid[0]["error"] == "invalid TOML or dictionary fields"
    assert invalid[0]["revision"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert str(invalid[0]["updated_at"]).endswith("Z")


def test_invalid_profile_is_rejected_at_session_start(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.dictionaries_dir.mkdir(parents=True)
    (paths.dictionaries_dir / "broken.toml").write_text("not = [toml")

    with build_client(FakeSupervisor(), context_hints_enabled=True, paths=paths) as client:
        messages, close_code = _websocket_start(
            client, _session_start({"profile": "broken"})
        )

    assert close_code == 1008
    assert any(message.get("code") == "unsupported_option" for message in messages)


def test_replacements_are_longest_first_left_to_right_and_non_recursive() -> None:
    rules = (
        ReplacementRule("ab", "short"),
        ReplacementRule("abc", "long"),
        ReplacementRule("bc", "other"),
        ReplacementRule("a", "bc"),
        ReplacementRule("aa", "X"),
    )

    assert apply_replacements("abcab", rules) == ("longshort", 2)
    assert apply_replacements("a", rules) == ("bc", 1)
    assert apply_replacements("aaa", rules) == ("Xbc", 2)
    assert apply_replacements("AB", rules) == ("AB", 0)
    assert apply_replacements("delete", (ReplacementRule("delete", ""),)) == ("", 1)


def _websocket_start(client: TestClient, start: dict[str, object]) -> tuple[list[dict], int]:
    messages: list[dict] = []
    close_code = 1000
    with client.websocket_connect("/v1/stream", headers=AUTH) as websocket:
        messages.append(websocket.receive_json())
        websocket.send_json(start)
        try:
            while True:
                message = websocket.receive_json()
                messages.append(message)
                if message.get("type") == "session.started":
                    break
        except WebSocketDisconnect as exc:
            close_code = exc.code
    return messages, close_code


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(support=tmp_path / "support", logs=tmp_path / "logs")


def test_context_is_rejected_when_capability_is_off_and_close_is_1008() -> None:
    with build_client(FakeSupervisor()) as client:
        capabilities = client.get("/v1/capabilities", headers=AUTH).json()
        assert capabilities["features"]["context_biasing"] is False
        assert "context_limits" not in capabilities["features"]

        messages, close_code = _websocket_start(client, _session_start({"domain": "講道"}))

    assert close_code == 1008
    assert any(message.get("code") == "unsupported_option" for message in messages)


def test_unknown_context_key_uses_protocol_error_close_convention() -> None:
    with build_client(FakeSupervisor(), context_hints_enabled=True) as client:
        messages, close_code = _websocket_start(
            client, _session_start({"domain": "講道", "not_allowed": True})
        )

    assert close_code == 1008
    assert any(message.get("code") == "protocol_error" for message in messages)


def test_unknown_profile_is_rejected_at_session_start(tmp_path: Path) -> None:
    with build_client(
        FakeSupervisor(), context_hints_enabled=True, paths=_paths(tmp_path)
    ) as client:
        messages, close_code = _websocket_start(
            client, _session_start({"profile": "missing"})
        )

    assert close_code == 1008
    assert any(message.get("code") == "unsupported_option" for message in messages)


def test_enabled_context_echo_and_dictionary_endpoint_auth(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    directory = paths.dictionaries_dir
    directory.mkdir(parents=True)
    (directory / "church_example.toml").write_text(
        _dictionary_text("profile domain", ["聖經", "禱告"], [("盛家", "聖經")])
    )
    with build_client(
        FakeSupervisor(), context_hints_enabled=True, paths=paths
    ) as client:
        unauthenticated = client.get("/v1/dictionaries")
        assert unauthenticated.status_code == 401
        response = client.get("/v1/dictionaries", headers=AUTH)
        assert response.status_code == 200
        summary = response.json()[0]
        assert summary["name"] == "church_example"
        assert summary["domain"] == "profile domain"
        assert summary["hotwords_count"] == 2
        assert summary["replacements_count"] == 1
        assert len(summary["revision"]) == 64
        assert summary["updated_at"].endswith("Z")
        messages, close_code = _websocket_start(
            client,
            _session_start(
                {
                    "profile": "church_example",
                    "domain": "inline domain",
                    "hotwords": ["福音"],
                }
            ),
        )

    assert close_code == 1000
    started = next(message for message in messages if message.get("type") == "session.started")
    assert started["context"] == {
        "profile": "church_example",
        "domain_chars": len("inline domain"),
        "hotwords_count": 3,
        "replacements_count": 1,
        "prompt_applied": False,
    }
    assert "prompt_tokens" not in started["context"]
    assert "inline domain" not in json.dumps(started)


def test_context_echo_schema_never_includes_prompt_tokens() -> None:
    fields = {
        "profile": None,
        "domain_chars": 4,
        "hotwords_count": 1,
        "replacements_count": 0,
        "prompt_applied": True,
    }
    echo = ContextEcho(**fields).model_dump(mode="json")

    assert "prompt_tokens" not in echo
    assert "prompt_tokens" not in ContextEcho.model_json_schema()["properties"]


def test_session_started_echoes_prompt_applied_when_prompt_is_on() -> None:
    with build_client(
        FakeSupervisor(),
        context_hints_enabled=True,
        context_prompt_enabled=True,
    ) as client:
        messages, close_code = _websocket_start(
            client, _session_start({"domain": "講道", "hotwords": ["聖經"]})
        )

    assert close_code == 1000
    started = next(message for message in messages if message.get("type") == "session.started")
    assert started["context"]["prompt_applied"] is True
    assert "prompt_tokens" not in started["context"]
