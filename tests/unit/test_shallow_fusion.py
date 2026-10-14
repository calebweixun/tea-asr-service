from __future__ import annotations

import json
import sys
import wave
from argparse import Namespace
from types import SimpleNamespace

import numpy as np

from benchmarks import shallow_fusion
from benchmarks.gold_offline_eval import run_evaluation
from benchmarks.shallow_fusion import (
    TokenTrie,
    TrieLogitsProcessor,
    _append_item_cache,
    _read_item_cache,
    apply_vectorized_bias,
    item_has_false_change,
    tokenize_term_variants,
)


def _write_json(path, document) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


class FakeTokenizer:
    def encode(self, text: str, *, add_special_tokens: bool) -> list[int]:
        assert add_special_tokens is False
        return {"甲乙": [1, 2], " 甲乙": [3, 2]}[text]


def test_fake_tokenizer_keeps_standalone_and_leading_space_tokenizations() -> None:
    assert tokenize_term_variants(FakeTokenizer(), "甲乙") == [[1, 2], [3, 2]]


def test_trie_advance_breaks_prefix_and_restarts_at_root() -> None:
    trie = TokenTrie([[10, 11, 12], [20, 21]])
    state, credit = trie.advance(0, 0, 10)
    assert state != 0
    assert credit == 1
    state, credit = trie.advance(state, credit, 11)
    assert state != 0
    assert credit == 2
    state, credit = trie.advance(state, credit, 7)
    assert (state, credit) == (0, 0)
    restarted, credit = trie.advance(state, credit, 20)
    assert restarted != 0
    assert credit == 1


def test_bonus_applies_to_starts_and_valid_continuations_only() -> None:
    processor = TrieLogitsProcessor([[10, 11, 12], [20, 21]], beta=2)
    logits = np.zeros(32, dtype=np.float32)
    root = processor(np.array([99]), logits)
    assert root[10] == 2
    assert root[20] == 2
    assert root[9] == 0

    continuation = processor(np.array([99, 10]), logits)
    assert continuation[11] == 2
    assert continuation[9] == -2
    assert continuation[20] == 0


def test_processor_stops_boosting_old_prefix_after_it_breaks() -> None:
    processor = TrieLogitsProcessor([[10, 11, 12], [20, 21]], beta=2)
    logits = np.zeros(32, dtype=np.float32)
    processor(np.array([99]), logits)
    processor(np.array([99, 10]), logits)

    # The third token breaks the 10 -> 11 prefix; the following step is back at root.
    reset_step = processor(np.array([99, 10, 7]), logits)
    assert processor.state == 0
    assert reset_step[11] == 0
    assert reset_step[10] == 2
    assert reset_step[20] == 2


def test_break_transition_rewinds_all_accumulated_partial_bonus() -> None:
    processor = TrieLogitsProcessor([[10, 11, 12]], beta=1.5)
    logits = np.zeros(32, dtype=np.float32)
    processor(np.array([99]), logits)
    processor(np.array([99, 10]), logits)
    two_token_prefix = processor(np.array([99, 10, 11]), logits)
    assert processor.partial_credit == 2
    assert two_token_prefix[9] == -3


def test_beta_zero_is_a_no_op_and_does_not_advance_state() -> None:
    processor = TrieLogitsProcessor([[10, 11]], beta=0)
    logits = np.arange(32, dtype=np.float32)
    result = processor(np.array([99, 10]), logits)
    assert result is logits
    assert processor.state == 0
    np.testing.assert_array_equal(result, logits)


def test_vectorized_bias_matches_reference_vocabulary_loop() -> None:
    logits = np.linspace(-3, 4, 12, dtype=np.float32)
    continuation = np.array(
        [False, True, False, False, True, False, False, False, False, False, False, False]
    )
    restart = np.array(
        [False, False, True, False, False, False, True, False, False, False, False, False]
    )
    beta = 1.25
    credit = 2
    actual = apply_vectorized_bias(
        logits, continuation, restart, beta, credit, array_module=np
    )
    expected = np.array(
        [
            logits[index]
            + (beta if continuation[index] else 0)
            + (beta if restart[index] else 0)
            - (beta * credit if not continuation[index] else 0)
            for index in range(len(logits))
        ],
        dtype=np.float32,
    )
    np.testing.assert_allclose(actual, expected)


def test_false_change_counts_correct_character_regression_and_new_target() -> None:
    assert item_has_false_change("你好甲乙。", "你好甲乙。", "你坏甲乙。", ["甲乙"])
    assert item_has_false_change("你好。", "你好。", "你好甲乙。", ["甲乙"])
    assert not item_has_false_change("你好甲乙。", "你好。", "你好甲乙。", ["甲乙"])


def test_offline_evaluator_resumes_selected_seeded_items_without_loading_backend(
    tmp_path,
) -> None:
    answers = tmp_path / "answers.json"
    wav_path = tmp_path / "audio.wav"
    answers.write_text(
        json.dumps(
            {
                "items": [
                    {"id": "a", "start_s": 0.0, "end_s": 0.5},
                    {"id": "b", "start_s": 0.5, "end_s": 1.0},
                ]
            }
        ),
        encoding="utf-8",
    )
    with wave.open(str(wav_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(np.zeros(16_000, dtype="<i2").tobytes())

    args = Namespace(
        answers=answers,
        wav=wav_path,
        model_path=tmp_path / "unused-model",
        dictionary=None,
        prompt=False,
        mode="segment",
        pad_s=0.0,
        carry_s=None,
        carry_max_gap_s=None,
    )

    def forbidden_backend(_path):
        raise AssertionError("complete cache must not construct a model backend")

    output, timings = run_evaluation(
        args,
        backend_factory=forbidden_backend,
        selected_ids={"b"},
        seed_hypotheses={"a": "baseline-a", "b": "baseline-b"},
        cached_results={"b": {"text": "cached-b", "timing": {"decode_s": 0.2}}},
    )
    assert output == {"a": "baseline-a", "b": "cached-b"}
    assert timings == {"b": {"decode_s": 0.2}}


def test_offline_evaluator_checkpoints_each_new_item(tmp_path) -> None:
    answers = tmp_path / "answers.json"
    wav_path = tmp_path / "audio.wav"
    answers.write_text(
        json.dumps({"items": [{"id": "a", "start_s": 0.0, "end_s": 0.5}]}),
        encoding="utf-8",
    )
    with wave.open(str(wav_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(np.zeros(8_000, dtype="<i2").tobytes())
    args = Namespace(
        answers=answers,
        wav=wav_path,
        model_path=tmp_path / "unused-model",
        dictionary=None,
        prompt=False,
        mode="segment",
        pad_s=0.0,
        carry_s=None,
        carry_max_gap_s=None,
    )
    checkpoints = []

    class FakeBackend:
        def __init__(self, _path):
            pass

        def load(self):
            pass

        def transcribe(self, _audio, *, system_prompt=None):
            assert system_prompt is None
            return SimpleNamespace(text="decoded", total_time_s=0.1)

        def close(self):
            pass

    output, timings = run_evaluation(
        args,
        backend_factory=FakeBackend,
        item_callback=lambda item_id, text, timing: checkpoints.append(
            (item_id, text, timing)
        ),
    )
    assert output == {"a": "decoded"}
    assert list(timings) == ["a"]
    assert len(checkpoints) == 1
    assert checkpoints[0][:2] == ("a", "decoded")
    assert checkpoints[0][2]["decode_attempts"] == 1


def test_item_cache_truncates_a_torn_final_record_before_resume(tmp_path) -> None:
    cache = tmp_path / "cache.jsonl"
    metadata = {"kind": "metadata", "cache_version": 1}
    cache.write_text(
        json.dumps(metadata)
        + "\n"
        + json.dumps({"kind": "item", "id": "a", "text": "saved", "timing": {}})
        + "\n"
        + '{"kind":"item","id":"torn"',
        encoding="utf-8",
    )

    records = _read_item_cache(cache, metadata)
    assert records == {"a": {"text": "saved", "timing": {}}}
    assert cache.read_text(encoding="utf-8").endswith("\n")

    _append_item_cache(cache, "b", "resumed", {"decode_s": 0.2})
    assert _read_item_cache(cache, metadata)["b"] == {
        "text": "resumed",
        "timing": {"decode_s": 0.2},
    }

    cache.write_text(cache.read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8")
    assert _read_item_cache(cache, metadata)["b"]["text"] == "resumed"
    _append_item_cache(cache, "c", "resumed-with-newline", {})
    assert _read_item_cache(cache, metadata)["c"]["text"] == "resumed-with-newline"


def test_hypothesis_baseline_prefers_beta_zero_and_warns_on_legacy_fallback(
    tmp_path, capsys
) -> None:
    service = shallow_fusion.SERVICES[0]
    legacy_path = shallow_fusion._legacy_baseline_path(tmp_path, service)
    beta0_path = shallow_fusion._beta0_hypotheses_path(tmp_path, service)
    _write_json(legacy_path, {"item": "stale"})
    _write_json(beta0_path, {"item": "current\ufffddecode"})

    assert shallow_fusion._load_hypotheses(
        shallow_fusion._baseline_path(tmp_path, service)
    ) == {"item": "currentdecode"}
    assert capsys.readouterr().err == ""

    beta0_path.unlink()
    assert shallow_fusion._load_hypotheses(
        shallow_fusion._baseline_path(tmp_path, service)
    ) == {"item": "stale"}
    assert "WARNING" in capsys.readouterr().err


def test_tune_item_matching_uses_beta_zero_hypotheses(tmp_path, monkeypatch) -> None:
    service = shallow_fusion.SERVICES[0]
    fold = shallow_fusion.SERVICES[1]
    target_path = tmp_path / "shallow-fusion" / "targets" / f"{fold}.json"
    _write_json(
        target_path,
        {
            "base_terms": [{"text": "甲乙", "source": "dictionary"}],
            "name_terms": [],
        },
    )
    _write_json(
        shallow_fusion._answers_path(tmp_path, service),
        {
            "items": [
                {"id": "current", "reference": "普通文字"},
                {"id": "stale", "reference": "普通文字"},
            ]
        },
    )
    _write_json(
        shallow_fusion._legacy_baseline_path(tmp_path, service), {"stale": "甲乙"}
    )
    _write_json(
        shallow_fusion._beta0_hypotheses_path(tmp_path, service), {"current": "甲乙"}
    )
    monkeypatch.setattr(shallow_fusion, "_phonetic_terms", lambda _entries: [])
    monkeypatch.setitem(
        sys.modules,
        "benchmarks.phonetic_rescore",
        SimpleNamespace(find_candidates=lambda *_args, **_kwargs: []),
    )

    assert shallow_fusion.select_tune_items(
        service, fold, work=tmp_path, random_controls=0
    ) == {"current"}


def test_item_cache_strips_replacement_char_from_old_and_new_records(tmp_path) -> None:
    cache = tmp_path / "cache.jsonl"
    metadata = {"kind": "metadata", "cache_version": 1}
    cache.write_text(
        json.dumps(metadata)
        + "\n"
        + json.dumps({"kind": "item", "id": "old", "text": "before\ufffdafter"})
        + "\n",
        encoding="utf-8",
    )

    assert _read_item_cache(cache, metadata)["old"]["text"] == "beforeafter"
    _append_item_cache(cache, "new", "before\ufffdafter", {})
    assert _read_item_cache(cache, metadata)["new"]["text"] == "beforeafter"


def test_backend_strips_replacement_char_before_shared_postprocessing() -> None:
    backend = object.__new__(shallow_fusion.ShallowFusionBackend)
    backend._model = SimpleNamespace(
        sample_rate=16_000,
        _tokenizer=None,
        _generate_single_chunk=lambda *_args, **_kwargs: ("decoded\ufffdtext", 1, 1),
    )
    backend._mx = SimpleNamespace(clear_cache=lambda: None)
    backend._make_sampler = lambda *_args, **_kwargs: None
    backend._qwen = SimpleNamespace(
        split_audio_into_chunks=lambda samples, **_kwargs: [(samples, None)]
    )
    backend._mask_cache = {}
    backend.beta = 0.0
    backend.sequences = []

    transcription = backend.transcribe(np.zeros(16_000, dtype=np.float32))

    assert transcription.text == "decodedtext"


def test_beta_zero_decode_writes_hypotheses_and_reports_only_differing_ids(
    tmp_path, monkeypatch, capsys
) -> None:
    service = shallow_fusion.SERVICES[0]
    fold = service
    target_path = tmp_path / "shallow-fusion" / "targets" / f"{fold}.json"
    _write_json(target_path, {"format": shallow_fusion.CACHE_VERSION, "base_terms": [], "name_terms": []})
    _write_json(shallow_fusion._answers_path(tmp_path, service), {"items": []})
    legacy_path = shallow_fusion._legacy_baseline_path(tmp_path, service)
    _write_json(legacy_path, {"one": "old-one", "two": "old-two"})
    wav_path = tmp_path / f"{service}.wav"
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    wav_path.write_bytes(b"audio")

    item_ids = ["one", "two"]
    monkeypatch.setattr(
        shallow_fusion.gold_offline_eval,
        "load_items",
        lambda _path: [{"id": item_id} for item_id in item_ids],
    )
    run_calls = 0

    def fake_run_evaluation(_args, **kwargs):
        nonlocal run_calls
        run_calls += 1
        cached = kwargs["cached_results"]
        if cached:
            return (
                {item_id: record["text"] for item_id, record in cached.items()},
                {item_id: record["timing"] for item_id, record in cached.items()},
            )
        output = {"one": "decoded\ufffdtext", "two": "new transcript"}
        for item_id, text in output.items():
            kwargs["item_callback"](item_id, text, {"decode_s": 0.01})
        return output, {item_id: {"decode_s": 0.01} for item_id in output}

    monkeypatch.setattr(
        shallow_fusion.gold_offline_eval, "run_evaluation", fake_run_evaluation
    )

    result = shallow_fusion._decode_service(
        service,
        fold,
        0,
        False,
        "F",
        "full",
        work=tmp_path,
        model_path=tmp_path / "model",
    )
    beta0_path = shallow_fusion._beta0_hypotheses_path(tmp_path, service)
    assert result[0] == {"one": "decodedtext", "two": "new transcript"}
    assert json.loads(beta0_path.read_text(encoding="utf-8")) == result[0]

    cache_path = shallow_fusion._cache_path(
        tmp_path,
        fold=fold,
        service=service,
        beta=0,
        include_names=False,
        system="F",
        subset="full",
    )
    metadata = json.loads(cache_path.read_text(encoding="utf-8").splitlines()[0])
    assert metadata["cache_version"] == shallow_fusion.CACHE_VERSION
    assert metadata["baseline"] == shallow_fusion._fingerprint(legacy_path)

    second_result = shallow_fusion._decode_service(
        service,
        fold,
        0,
        False,
        "F",
        "full",
        work=tmp_path,
        model_path=tmp_path / "model",
    )
    assert second_result[0] == result[0]
    assert run_calls == 2
    log = capsys.readouterr().err
    assert "beta=0 vs eval/m8-c3: 2 item(s) differ" in log
    assert "\none\n" in log and "\ntwo\n" in log
    assert "decodedtext" not in log and "new transcript" not in log


def test_tune_false_change_guard_uses_beta_zero_baseline(tmp_path, monkeypatch) -> None:
    services = shallow_fusion.SERVICES[:2]
    monkeypatch.setattr(shallow_fusion, "SERVICES", services)
    expected_baselines = {}
    for service in services:
        item_id = f"item-{service}"
        beta0_text = f"current-{service}"
        expected_baselines[item_id] = f"{beta0_text}|dictionary"
        _write_json(
            shallow_fusion._beta0_hypotheses_path(tmp_path, service),
            {item_id: beta0_text},
        )
        _write_json(
            shallow_fusion._legacy_baseline_path(tmp_path, service),
            {item_id: f"stale-{service}"},
        )
        _write_json(
            tmp_path / "shallow-fusion" / "targets" / f"{service}.json",
            {"format": shallow_fusion.CACHE_VERSION, "base_terms": [], "name_terms": []},
        )

    monkeypatch.setattr(
        shallow_fusion,
        "select_tune_items",
        lambda service, _fold, **_kwargs: {f"item-{service}"},
    )
    monkeypatch.setattr(
        shallow_fusion.gold_offline_eval,
        "load_items",
        lambda path: [
            {
                "id": f"item-{path.stem.removesuffix('-pad25')}",
                "reference": "reference",
            }
        ],
    )
    monkeypatch.setattr(
        shallow_fusion,
        "_decode_service",
        lambda service, _fold, _beta, _names, _system, _subset, **kwargs: (
            {f"item-{service}": "fused"},
            {},
            kwargs["selected_ids"],
        ),
    )
    monkeypatch.setattr(
        shallow_fusion,
        "_apply_dictionary",
        lambda hypotheses, _path: {
            item_id: f"{text}|dictionary" for item_id, text in hypotheses.items()
        },
    )
    false_change_baselines = []
    monkeypatch.setattr(
        shallow_fusion,
        "item_has_false_change",
        lambda _reference, baseline, _fusion, _terms: (
            false_change_baselines.append(baseline) or False
        ),
    )
    monkeypatch.setattr(
        shallow_fusion.church_eval_report,
        "item_errors",
        lambda _items, _hypotheses, fold=True: (np.array([0]), np.array([1])),
    )

    shallow_fusion.tune(work=tmp_path, betas=(1.0,), random_controls=0)

    assert false_change_baselines
    assert set(false_change_baselines) == set(expected_baselines.values())


def test_report_uses_beta_zero_for_a_b_and_false_change_baseline(
    tmp_path, monkeypatch
) -> None:
    services = shallow_fusion.SERVICES
    expected_a = {}
    expected_b = {}
    all_items = []
    for service in services:
        item_id = f"item-{service}"
        beta0_text = f"current-{service}"
        expected_a[item_id] = beta0_text
        expected_b[item_id] = f"{beta0_text}|dictionary"
        _write_json(
            shallow_fusion._beta0_hypotheses_path(tmp_path, service),
            {item_id: beta0_text},
        )
        _write_json(
            shallow_fusion._legacy_baseline_path(tmp_path, service),
            {item_id: f"stale-{service}"},
        )
        _write_json(
            shallow_fusion._answers_path(tmp_path, service),
            {
                "items": [
                    {
                        "id": item_id,
                        "reference": "reference",
                        "start_s": 0.0,
                        "end_s": 1.0,
                    }
                ]
            },
        )
        _write_json(
            tmp_path / "shallow-fusion" / "targets" / f"{service}.json",
            {
                "format": shallow_fusion.CACHE_VERSION,
                "base_terms": [{"text": "target"}],
                "name_terms": [],
            },
        )
        all_items.append(
            {
                "id": item_id,
                "service": service,
                "reference": "reference",
                "start_s": 0.0,
                "end_s": 1.0,
            }
        )

    results_path = tmp_path / "tune-results.json"
    _write_json(
        results_path,
        {
            "format": shallow_fusion.CACHE_VERSION,
            "folds": {
                service: {
                    "choice": {
                        "beta": 1.0,
                        "include_names": False,
                        "errors": 0,
                        "false_changes": 0,
                        "objective": 0,
                    }
                }
                for service in services
            },
        },
    )
    observed_hypotheses = []
    false_change_baselines = []
    monkeypatch.setattr(
        shallow_fusion.church_eval_report,
        "load_answers",
        lambda _work, _services: all_items,
    )
    monkeypatch.setattr(
        shallow_fusion.church_eval_report,
        "item_errors",
        lambda _items, hypotheses, fold=True: (
            observed_hypotheses.append(dict(hypotheses))
            or (np.array([0]), np.array([1]))
        ),
    )

    class FakeScorer:
        def __init__(self, _items, _iterations, _seed):
            pass

        def cer(self, _errors, _lengths, _scope):
            return {"cer": 0.01, "ci95_low": 0.009, "ci95_high": 0.011}

        def delta(self, _new, _base, _scope):
            return {"delta_cer": -0.001, "ci95_low": -0.002, "ci95_high": -0.0005}

    monkeypatch.setattr(shallow_fusion.church_eval_report, "Scorer", FakeScorer)
    monkeypatch.setattr(
        shallow_fusion,
        "_apply_dictionary",
        lambda hypotheses, _path: {
            item_id: f"{text}|dictionary" for item_id, text in hypotheses.items()
        },
    )

    def fake_load_full_cache(*, service, beta, system, **_kwargs):
        item_id = f"item-{service}"
        if beta == 0:
            return {}, {item_id: {"decode_s": 0.1}}
        return {item_id: f"{system}-{service}"}, {item_id: {"decode_s": 0.12}}

    monkeypatch.setattr(shallow_fusion, "_load_full_cache", fake_load_full_cache)

    def record_false_change(_reference, baseline, _fusion, _terms):
        false_change_baselines.append(baseline)
        return False

    monkeypatch.setattr(shallow_fusion, "item_has_false_change", record_false_change)
    shallow_fusion.report(
        work=tmp_path,
        results_path=results_path,
        markdown_path=tmp_path / "report.md",
        iterations=1,
    )

    assert expected_a in observed_hypotheses
    assert expected_b in observed_hypotheses
    assert set(false_change_baselines) == set(expected_b.values())
