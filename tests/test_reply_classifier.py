from __future__ import annotations

import json

from sidepulse.reply_classifier import parse_label


def test_parse_label_accepts_digit_protocol_and_labels() -> None:
    assert parse_label("0") == "NO_REPLY_REQUIRED"
    assert parse_label("1\n") == "REPLY_REQUIRED"
    assert parse_label("2.") == "UNCERTAIN"
    assert parse_label("NO_REPLY_REQUIRED") == "NO_REPLY_REQUIRED"


def test_parse_label_rejects_malformed_or_conflicting_output() -> None:
    assert parse_label("not a label") == "UNCERTAIN"
    assert parse_label("REPLY_REQUIRED NO_REPLY_REQUIRED") == "UNCERTAIN"


def test_benchmark_reads_recent_unique_stop_messages(tmp_path) -> None:
    from examples.benchmark_reply_classifier import log_examples

    path = tmp_path / "events.jsonl"
    events = [
        {"hook_event": "UserPromptSubmit", "message": "ignore"},
        {"hook_event": "Stop", "message": "first  message"},
        {"hook_event": "Stop", "message": "first  message"},
        {"hook_event": "Stop", "message": "second\nmessage"},
    ]
    path.write_text("\n".join(json.dumps(event) for event in events))
    assert log_examples(path, 1) == ["second message"]
    assert log_examples(path, 0) == []
