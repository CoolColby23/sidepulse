"""Benchmark the local reply classifier on canonical and SidePulse-log examples."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from pathlib import Path

from sidepulse.reply_classifier import DEFAULT_MODEL, ReplyClassifier

LABELED = [
    ("Can you send the document?", "REPLY_REQUIRED"),
    ("Please send the document.", "REPLY_REQUIRED"),
    ("Let me know when you arrive.", "REPLY_REQUIRED"),
    ("Which option should we choose?", "REPLY_REQUIRED"),
    ("Thanks, I received it.", "NO_REPLY_REQUIRED"),
    ("Just an FYI, the deployment is complete.", "NO_REPLY_REQUIRED"),
    ("Sounds good.", "NO_REPLY_REQUIRED"),
    ("Have a good weekend.", "NO_REPLY_REQUIRED"),
]


def log_examples(path: Path, limit: int) -> list[str]:
    messages: list[str] = []
    if limit <= 0:
        return messages
    if path.exists():
        for line in path.read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("hook_event") == "Stop" and event.get("message"):
                text = " ".join(event["message"].split())
                if text not in messages:
                    messages.append(text)
    return messages[-limit:]


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(len(ordered) * fraction + 0.999999) - 1))]


def rss_mib() -> float | None:
    try:
        import psutil
        return psutil.Process(os.getpid()).memory_info().rss / 2**20
    except ImportError:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--log", type=Path, default=Path.home() / ".local/state/sidepulse/agent-monitor/event-status.jsonl")
    parser.add_argument("--log-examples", type=int, default=12)
    parser.add_argument("--warm-runs", type=int, default=20)
    args = parser.parse_args()

    started = time.perf_counter()
    classifier = ReplyClassifier(args.model)
    load_seconds = time.perf_counter() - started
    rss_after_load = rss_mib()

    examples = [(text, expected, "canonical") for text, expected in LABELED]
    examples += [(text, None, "event-status.jsonl") for text in log_examples(args.log, args.log_examples)]
    rows = []
    for text, expected, source in examples:
        before = time.perf_counter()
        result = classifier.classify(text)
        rows.append({
            "source": source, "message": text, "expected": expected,
            "predicted": result.label, "latency_ms": (time.perf_counter() - before) * 1000,
            "raw_output": result.raw_output,
        })

    warm_text = "Could you check this for me?"
    latencies = []
    for _ in range(args.warm_runs):
        before = time.perf_counter()
        classifier.classify(warm_text)
        latencies.append((time.perf_counter() - before) * 1000)
    elapsed = sum(latencies) / 1000
    correct = sum(row["predicted"] == row["expected"] for row in rows if row["expected"])
    labeled_count = sum(row["expected"] is not None for row in rows)
    counts = {label: sum(row["predicted"] == label for row in rows) for label in ("REPLY_REQUIRED", "NO_REPLY_REQUIRED", "UNCERTAIN")}
    print(json.dumps({
        "model": args.model,
        "log_path": str(args.log),
        "cold_model_load_seconds": load_seconds,
        "rss_after_load_mib": rss_after_load,
        "examples": rows,
        "summary": {
            "example_count": len(rows), "prediction_counts": counts,
            "labeled_count": labeled_count, "correct": correct,
            "accuracy": correct / labeled_count if labeled_count else None,
            "warm_runs": args.warm_runs,
            "warm_mean_ms": statistics.mean(latencies),
            "warm_median_ms": statistics.median(latencies),
            "warm_p95_ms": percentile(latencies, 0.95),
            "throughput_messages_per_second": args.warm_runs / elapsed,
        },
    }, indent=2))


if __name__ == "__main__":
    main()
