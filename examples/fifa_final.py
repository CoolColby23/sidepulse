#!/usr/bin/env python3
"""Show the FIFA World Cup final score on a SidePulse Pro LED bar.

Spain fills from the left in red, Argentina from the right in blue. Each side
shows a dim team-color baseline, and one LED per goal lights at full blast.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


REPO_SRC = Path(__file__).resolve().parents[1] / "src"
if REPO_SRC.exists():
    sys.path.insert(0, str(REPO_SRC))

from sidepulse.device_writer import (  # noqa: E402
    DEFAULT_FILE_NAME,
    DeviceWriteError,
    validate_led_text,
    write_led_program,
)
from sidepulse.led_status import led_count_for_target  # noqa: E402


SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
LEFT_TEAM = "Spain"
RIGHT_TEAM = "Argentina"
LEFT_RGB = (255, 0, 0)
RIGHT_RGB = (0, 0, 255)
DEFAULT_POLL_SECONDS = 30.0
DEFAULT_DIM = 0.012
DEFAULT_TRANSITION_MS = 250
CELEBRATION_SECONDS = 3.6
HTTP_TIMEOUT_SECONDS = 15.0


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def normalize_led_count(led_count: int) -> int:
    return max(2, min(8, int(led_count)))


def rgb_hex(rgb: tuple[int, int, int], brightness: float = 1.0) -> str:
    brightness = clamp(brightness)
    red, green, blue = (int(round(channel * brightness)) for channel in rgb)
    return f"#{red:02X}{green:02X}{blue:02X}"


def build_led_program(
    left_goals: int,
    right_goals: int,
    *,
    led_count: int = 8,
    dim: float = DEFAULT_DIM,
    transition_ms: int = DEFAULT_TRANSITION_MS,
) -> str:
    count = normalize_led_count(led_count)
    left_side = count // 2
    right_side = count - left_side
    left_lit = min(max(0, int(left_goals)), left_side)
    right_lit = min(max(0, int(right_goals)), right_side)
    duration = max(0, int(transition_ms))

    segments: list[str] = []
    for index in range(count):
        if index < left_side:
            rgb = LEFT_RGB
            lit = index < left_lit
        else:
            rgb = RIGHT_RGB
            lit = index >= count - right_lit
        color = rgb_hex(rgb, 1.0 if lit else dim)
        token = f"{index}:{color}"
        if duration:
            token += f" {duration}ms cosine"
        segments.append(token)

    program = "; ".join(segments)
    validate_led_text(program)
    return program


def build_celebration_program(rgb: tuple[int, int, int]) -> str:
    program = f"{rgb_hex(rgb)} 500ms pulse\noff 100ms none\nrepeat 3"
    validate_led_text(program)
    return program


def fetch_scoreboard(url: str = SCOREBOARD_URL) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "sidepulse-fifa-final/1.0"})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return json.load(response)


def team_matches(competitor: dict[str, Any], team_name: str) -> bool:
    team = competitor.get("team") or {}
    wanted = team_name.casefold()
    for key in ("displayName", "shortDisplayName", "name", "location"):
        value = team.get(key)
        if isinstance(value, str) and wanted in value.casefold():
            return True
    return False


def find_match(data: Any) -> dict[str, Any] | None:
    for event in data.get("events") or []:
        for competition in event.get("competitions") or []:
            competitors = competition.get("competitors") or []
            left = next((c for c in competitors if team_matches(c, LEFT_TEAM)), None)
            right = next((c for c in competitors if team_matches(c, RIGHT_TEAM)), None)
            if left is None or right is None:
                continue
            status = (event.get("status") or {}).get("type") or {}
            return {
                "left_goals": int(left.get("score") or 0),
                "right_goals": int(right.get("score") or 0),
                "state": status.get("state", "pre"),
                "detail": status.get("shortDetail") or status.get("detail") or "",
            }
    return None


def parse_score(value: str) -> tuple[int, int]:
    try:
        left_text, right_text = value.split("-", 1)
        return int(left_text), int(right_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected SPAIN-ARGENTINA goals, e.g. 2-1") from exc


def terminal_preview(left_goals: int, right_goals: int, *, led_count: int) -> str:
    count = normalize_led_count(led_count)
    left_side = count // 2
    cells: list[str] = []
    for index in range(count):
        if index < left_side:
            rgb, lit = LEFT_RGB, index < min(left_goals, left_side)
        else:
            rgb, lit = RIGHT_RGB, index >= count - min(right_goals, count - left_side)
        red, green, blue = (c if lit else max(1, c // 6) for c in rgb)
        char = "#" if lit else "."
        cells.append(f"\033[38;2;{red};{green};{blue}m{char}\033[0m")
    return "".join(cells)


def resolve_target(args: argparse.Namespace) -> Path | None:
    if args.dry_run and args.device is None:
        return None
    return write_led_program(
        "off",
        device_path=args.device,
        file_name=args.file_name,
        dry_run=True,
    )


def run_monitor(args: argparse.Namespace) -> int:
    target = resolve_target(args)
    led_count = (
        normalize_led_count(args.led_count)
        if args.led_count is not None
        else (led_count_for_target(target) if target is not None else 8)
    )
    destination = "dry-run" if args.dry_run else str(target)
    print(f"fifa final: {LEFT_TEAM} (red, left) vs {RIGHT_TEAM} (blue, right) -> {destination}")

    last_score: tuple[int, int] | None = None
    last_program: str | None = None

    try:
        while True:
            if args.score is not None:
                match: dict[str, Any] | None = {
                    "left_goals": args.score[0],
                    "right_goals": args.score[1],
                    "state": "in",
                    "detail": "manual score",
                }
            else:
                try:
                    match = find_match(fetch_scoreboard())
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                    print(f"fifa final: scoreboard fetch failed: {exc}", file=sys.stderr)
                    match = None

            if match is None:
                if args.score is None:
                    print(f"fifa final: no {LEFT_TEAM} vs {RIGHT_TEAM} match on the scoreboard")
            else:
                score = (match["left_goals"], match["right_goals"])
                if last_score is not None and score != last_score and not args.dry_run:
                    scorer_rgb = LEFT_RGB if score[0] > last_score[0] else RIGHT_RGB
                    write_led_program(
                        build_celebration_program(scorer_rgb),
                        device_path=target,
                        file_name=args.file_name,
                    )
                    time.sleep(CELEBRATION_SECONDS)
                    last_program = None

                program = build_led_program(
                    score[0],
                    score[1],
                    led_count=led_count,
                    dim=args.dim,
                    transition_ms=args.transition_ms,
                )
                if program != last_program:
                    if not args.dry_run:
                        write_led_program(program, device_path=target, file_name=args.file_name)
                    last_program = program

                if score != last_score:
                    bar = terminal_preview(score[0], score[1], led_count=led_count)
                    print(
                        f"{bar} {LEFT_TEAM} {score[0]} - {score[1]} {RIGHT_TEAM}"
                        f" ({match['detail']})".rstrip()
                    )
                last_score = score

                if match["state"] == "post":
                    print("fifa final: full time, holding final score")
                    return 0

            if args.once:
                return 0 if match is not None else 1
            time.sleep(max(5.0, float(args.poll)))
    except KeyboardInterrupt:
        return 0
    finally:
        if args.off_on_exit and target is not None and not args.dry_run:
            try:
                write_led_program("off", device_path=target, file_name=args.file_name)
            except (DeviceWriteError, OSError) as exc:
                print(f"fifa final: could not turn LEDs off: {exc}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Show the FIFA World Cup final on SidePulse Pro LEDs: "
            f"{LEFT_TEAM} in red from the left, {RIGHT_TEAM} in blue from the right. "
            "Each side keeps a dim team-color baseline; goals light LEDs at full blast."
        ),
    )
    parser.add_argument(
        "--device",
        type=Path,
        help="Mounted device folder or LED program file. Defaults to auto-detecting /Volumes.",
    )
    parser.add_argument(
        "--file-name",
        default=DEFAULT_FILE_NAME,
        help=f"Target file name when --device is a folder. Default: {DEFAULT_FILE_NAME}.",
    )
    parser.add_argument("--led-count", type=int, help="LED count. Defaults to device name, then 8.")
    parser.add_argument(
        "--poll",
        type=float,
        default=DEFAULT_POLL_SECONDS,
        help=f"Seconds between scoreboard checks. Default: {DEFAULT_POLL_SECONDS:.0f}.",
    )
    parser.add_argument(
        "--dim",
        type=float,
        default=DEFAULT_DIM,
        help="Baseline brightness for unlit LEDs, 0.0 to 1.0.",
    )
    parser.add_argument(
        "--transition-ms",
        type=int,
        default=DEFAULT_TRANSITION_MS,
        help="Controller-side cosine fade per update; set 0 for immediate updates.",
    )
    parser.add_argument(
        "--score",
        type=parse_score,
        help=f"Skip the API and show a fixed {LEFT_TEAM}-{RIGHT_TEAM} score, e.g. 2-1.",
    )
    parser.add_argument("--once", action="store_true", help="Check once, write the LEDs, and exit.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview without writing to a SidePulse Pro device.",
    )
    parser.add_argument("--off-on-exit", action="store_true", help="Write 'off' when the monitor exits.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run_monitor(args)
    except DeviceWriteError as exc:
        print(f"fifa final: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"fifa final: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
