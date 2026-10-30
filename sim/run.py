"""Run simulated hotel calls against Prism, grade them, and write a report.

  uv run python -m sim.run                       # every scenario once
  uv run python -m sim.run -s ivr_then_desk -n 3 # one scenario, three times
  uv run python -m sim.run --phone               # 8 kHz mu-law line, like a Twilio bridge
  uv run python -m sim.run --no-judge            # code checks only, no Claude grading
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import subprocess
import sys
import time
from datetime import UTC, datetime
from importlib import metadata

from . import ROOT, load_env


def fmt(ms: float) -> str:
    return f"{int(ms // 60000):02d}:{ms % 60000 / 1000:04.1f}"


def show(result: dict) -> None:
    for e in result["timeline"]:
        who = e["who"]
        label = {"hotel": f"HOTEL ({e.get('mode')})", "prism": "PRISM", "heard": "  prism heard", "tool": "  tool"}.get(who, f"  {who}")
        extra = ""
        if who == "tool":
            extra = " " + json.dumps(e.get("args", {}))
        if who == "prism" and e.get("cut"):
            extra = f"  [cut at {e['played_ms']} of {e['total_ms']} ms]"
        print(f"{fmt(e['t_ms'])} {label}: {e['text']}{extra}")
    print(f"-- {result['scenario']}: {result['hangup'] or result['error']}, {result['duration_ms'] / 1000:.0f} s")


def now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def git_state() -> dict:
    """The commit this run used, and whether the working tree had changes on top of it."""

    def git(*args: str) -> str | None:
        try:
            return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return None

    status = git("status", "--porcelain")
    return {"commit": git("rev-parse", "--short", "HEAD"), "dirty": None if status is None else bool(status)}


async def main() -> None:
    load_env()
    import anthropic
    from rimelabs_sdk import Rime

    from .audio import Voices
    from .call import Options, run_call
    from .scenarios import BY_ID, SCENARIOS

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-s", "--scenario", action="append", choices=sorted(BY_ID), help="run only these (repeatable)")
    parser.add_argument("-n", "--repeat", type=int, default=1, help="calls per scenario")
    parser.add_argument("-j", "--jobs", type=int, default=2, help="calls at the same time")
    parser.add_argument("--phone", action="store_true", help="8 kHz mu-law line instead of 16 kHz")
    parser.add_argument("--voice", default="astra", help="Prism's voice")
    parser.add_argument("--eagerness", choices=["low", "medium", "high", "auto"])
    parser.add_argument("--end-of-turn-ms", type=int)
    parser.add_argument("--max-seconds", type=int, default=300)
    parser.add_argument("--label", default="", help="a name for this run, added to its folder, such as before or after")
    parser.add_argument("--fillers", choices=["off", "default"], default="off", help="Prism's filler while a tool runs")
    parser.add_argument("--no-judge", action="store_true", help="skip Claude grading")
    parser.add_argument("--quiet", action="store_true", help="do not print transcripts")
    args = parser.parse_args()

    scenarios = [BY_ID[i] for i in args.scenario] if args.scenario else SCENARIOS
    options = Options(
        phone=args.phone,
        voice=args.voice,
        max_seconds=args.max_seconds,
        eagerness=args.eagerness,
        end_of_turn_ms=args.end_of_turn_ms,
        label=args.label,
        fillers=args.fillers,
    )
    from .claude import MODEL, SPEND_CAP, SPEND_MARGIN, spend

    before = spend()["dollars"]
    if before >= SPEND_CAP - SPEND_MARGIN:
        raise SystemExit(f"Claude spend on this key is ${before:.2f}, at the ${SPEND_CAP:.2f} cap. Set CLAUDE_SPEND_CAP to raise it.")
    print(f"Claude spend on this key so far: ${before:.2f} of ${SPEND_CAP:.2f}", flush=True)
    out = ROOT / "runs" / (time.strftime("%Y%m%d-%H%M%S") + (f"-{args.label}" if args.label else ""))
    out.mkdir(parents=True)

    # What this run ran against, so a rerun on another day or computer can be compared with it.
    # The SDK does not report Prism's server release, so the run date stands in for it.
    run_info = {
        "started_at": now_utc(),
        "command": "uv run python -m sim.run " + " ".join(sys.argv[1:]),
        "args": vars(args),
        "calls": len(scenarios) * args.repeat,
        "git": git_state(),
        "rimelabs_sdk": metadata.version("rimelabs-sdk"),
        "claude_model": MODEL,
        "python": platform.python_version(),
    }
    (out / "run.json").write_text(json.dumps(run_info, indent=2))
    claude = anthropic.AsyncAnthropic()
    limit = asyncio.Semaphore(args.jobs)

    async with Rime() as rime:
        voices = Voices(rime, ROOT / "cache" / "tts")

        async def one(scenario, k):
            name = scenario.id if args.repeat == 1 else f"{scenario.id}-{k + 1}"
            async with limit:
                print(f"calling {name}", flush=True)
                result = await run_call(scenario, rime, claude, voices, options, wav=out / f"{name}.wav")
            result["name"] = name
            (out / f"{name}.json").write_text(json.dumps(result, indent=2))
            if not args.quiet:
                show(result)
            return result

        results = await asyncio.gather(*(one(s, k) for s in scenarios for k in range(args.repeat)))

    from .grade import grade_run

    await grade_run(out, results, claude, judge_calls=not args.no_judge)
    run_info["finished_at"] = now_utc()
    run_info["calls_with_errors"] = sum(1 for r in results if r.get("error"))
    (out / "run.json").write_text(json.dumps(run_info, indent=2))
    after = spend()["dollars"]
    print(f"\nClaude spend: ${after - before:.2f} this run, ${after:.2f} of ${SPEND_CAP:.2f} on this key")
    print(f"Report: {out / 'report.html'}")


if __name__ == "__main__":
    asyncio.run(main())
