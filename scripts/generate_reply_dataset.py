"""Generate a reproducible reply-expectation JSONL dataset."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import random
from pathlib import Path

LABELS = ("REPLY_REQUIRED", "NO_REPLY_REQUIRED", "UNCERTAIN")

# These are the examples collected during the original classifier discussion.
HUMAN_EXAMPLES = (
    ("Can you send the document?", "REPLY_REQUIRED", "direct_question"),
    ("Please send the document.", "REPLY_REQUIRED", "request"),
    ("Let me know when you arrive.", "REPLY_REQUIRED", "response_request"),
    ("Which option should we choose?", "REPLY_REQUIRED", "decision"),
    ("Thanks, I received it.", "NO_REPLY_REQUIRED", "thanks"),
    ("Just an FYI, the deployment is complete.", "NO_REPLY_REQUIRED", "information"),
    ("Sounds good.", "NO_REPLY_REQUIRED", "acknowledgement"),
    ("Have a good weekend.", "NO_REPLY_REQUIRED", "closing"),
)

PEOPLE = ("me", "Alex", "Sam", "the client", "your manager")
OBJECTS = ("document", "report", "invoice", "link", "photo", "draft", "build", "schedule")
TIMES = ("today", "tomorrow", "before lunch", "by 4", "this week")
OPTIONS = ("the first option", "the second option", "Tuesday", "Wednesday", "email", "Slack")

SYNTHETIC_TEMPLATES = {
    "REPLY_REQUIRED": (
        ("direct_question", "Can you send {person} the {object} {time}?"),
        ("direct_question", "Could you check the {object}?"),
        ("information_question", "Where is the {object}?"),
        ("information_question", "When will the {object} be ready?"),
        ("request", "Please send the {object} {time}."),
        ("request", "I need the {object} {time}."),
        ("response_request", "Let me know when the {object} is ready."),
        ("response_request", "Tell {person} what you think about the {object}."),
        ("confirmation", "Please confirm you received the {object}."),
        ("confirmation", "Does {time} still work for you?"),
        ("decision", "Which should we choose: {option_a} or {option_b}?"),
        ("invitation", "Would you like to review the {object} {time}?"),
        ("informal_request", "hey can u send the {object}"),
        ("informal_question", "u free {time}?"),
        ("indirect_request", "It would help to know whether {time} works for you."),
    ),
    "NO_REPLY_REQUIRED": (
        ("information", "FYI, the {object} is ready."),
        ("information", "The {object} was sent {time}."),
        ("completion", "Done — I sent the {object} to {person}."),
        ("completion", "The {object} finished successfully."),
        ("acknowledgement", "Sounds good."),
        ("acknowledgement", "Got it."),
        ("acknowledgement", "Okay, noted."),
        ("thanks", "Thanks for the {object}!"),
        ("thanks", "Thank you, I received it."),
        ("closing", "Have a good weekend."),
        ("closing", "Talk soon."),
        ("closing", "No need to reply."),
        ("rhetorical", "Isn't that funny?"),
        ("rhetorical", "Who would have thought?"),
        ("informal_acknowledgement", "k thx"),
    ),
    "UNCERTAIN": (
        ("missing_context", "Probably {time}."),
        ("missing_context", "Maybe {option_a}."),
        ("missing_context", "That depends."),
        ("ambiguous_update", "The meeting moved to {time}."),
        ("ambiguous_update", "I left the {object} with {person}."),
        ("ambiguous_intent", "I was wondering about the {object}."),
        ("ambiguous_intent", "It would be nice to have the {object}."),
        ("fragment", "About the {object}..."),
        ("fragment", "And {time}?"),
        ("social_context", "You know what I mean."),
        ("social_context", "We'll see."),
        ("possible_confirmation", "The {object} is ready, I think."),
        ("possible_confirmation", "So we're going with {option_a}."),
        ("unclear_question", "Right?"),
        ("unclear_question", "Okay?"),
    ),
}


def stable_split(message: str) -> str:
    bucket = int(hashlib.sha256(message.encode()).hexdigest()[:8], 16) % 100
    return "train" if bucket < 80 else "validation" if bucket < 90 else "test"


def render(template: str, values: dict[str, str]) -> str:
    return template.format(**values)


def synthetic_rows(per_label: int, seed: int, excluded: set[str] | None = None) -> list[dict[str, str]]:
    rng = random.Random(seed)
    excluded = excluded or set()
    value_rows = [
        {"person": person, "object": obj, "time": when, "option_a": a, "option_b": b}
        for person, obj, when, a, b in itertools.product(PEOPLE, OBJECTS, TIMES, OPTIONS, OPTIONS)
        if a != b
    ]
    rows: list[dict[str, str]] = []
    for label, templates in SYNTHETIC_TEMPLATES.items():
        candidates = []
        for category, template in templates:
            for values in value_rows:
                message = render(template, values)
                if message not in excluded:
                    candidates.append((message, category))
        candidates = list(dict.fromkeys(candidates))
        rng.shuffle(candidates)
        if per_label > len(candidates):
            raise ValueError(f"requested {per_label} {label} rows; only {len(candidates)} unique rows exist")
        for message, category in candidates[:per_label]:
            rows.append({
                "message": message,
                "label": label,
                "source": "synthetic_template",
                "split": stable_split(message),
                "category": category,
            })
    return rows


def generate(per_label: int, seed: int) -> list[dict[str, str]]:
    rows = [
        {
            "message": message,
            "label": label,
            "source": "human_collected",
            "split": "test",
            "category": category,
        }
        for message, label, category in HUMAN_EXAMPLES
    ]
    rows.extend(synthetic_rows(per_label, seed, {row["message"] for row in rows}))
    for index, row in enumerate(rows, 1):
        row["id"] = f"reply-{index:05d}"
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/reply_expectation.jsonl"))
    parser.add_argument("--per-label", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    rows = generate(args.per_label, args.seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    counts = {label: sum(row["label"] == label for row in rows) for label in LABELS}
    splits = {split: sum(row["split"] == split for row in rows) for split in ("train", "validation", "test")}
    print(json.dumps({"output": str(args.output), "rows": len(rows), "labels": counts, "splits": splits}, indent=2))


if __name__ == "__main__":
    main()
