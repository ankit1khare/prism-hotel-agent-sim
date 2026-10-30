"""Compare graded runs side by side, for example before and after a change to the instructions.

  uv run python -m sim.compare runs/<first> runs/<second>
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import hotel_agent as agent

from . import NOT_CALLS
from .grade import QUIET_MODES, chars_in

# Asking the hotel for a detail Prism holds, e.g. "What is the confirmation number?". Not "Can you
# cancel with the confirmation number?", which the instructions ask for when there is no card.
ASKS_FOR_OWN = re.compile(
    r"(what is|what's|(could|can|would) you (please )?(provide|give|tell|confirm|read)|do you have)( me)? "
    r"(the |your )?(reservation's )?(confirmation number|guest'?s name|check-in date)",
    re.I,
)
CONFIRM_Q = re.compile(r"(is that (right|correct)|correct\?|right\?)", re.I)
SPELLED_NAME = re.compile(r"\b[A-Z]-[A-Z]-[A-Z]")
# One cut-in in the no_cut_ins detail, "at 12.3 s: <what Prism said>". Prism's words can hold "at ",
# so match the time stamp, not the word.
CUT_IN = re.compile(r"(?:^|; )at \d+\.\d s: ")


def measure(run: Path) -> dict:
    grades = json.loads((run / "grades.json").read_text())
    results = {r["name"]: r for r in (json.loads(p.read_text()) for p in run.glob("*.json") if p.name not in NOT_CALLS)}
    m = {
        "calls": len(grades),
        "clean calls": sum(g["passed"] for g in grades),
        "calls with a critical error": sum(any(f["critical"] for f in g["failures"]) for g in grades),
        "right outcome": sum(all(c["passed"] for c in g["checks"] if c["id"].startswith("status")) for g in grades),
        "errors in all": sum(len(g["failures"]) for g in grades),
        "calls where Prism asked the hotel for its own details": 0,
        "calls where Prism asked the hotel to confirm its own details": 0,
        "calls where Prism spelled the name with dashes": 0,
        "calls with a filler": 0,
        "calls with talk into a menu, hold or voicemail": 0,
        "calls where Prism cut in": sum(not next(c["passed"] for c in g["checks"] if c["id"] == "no_cut_ins") for g in grades),
        "cut-ins in all": sum(
            len(CUT_IN.findall(next(c["detail"] for c in g["checks"] if c["id"] == "no_cut_ins"))) for g in grades
        ),
        "guard stopped a made-up number": 0,
        "guard stopped an early hang-up": 0,
        "outcome recorded during a lookup": 0,
    }
    for g in grades:
        r = results[g["name"]]
        own = chars_in(r)
        prism = [e for e in r["timeline"] if e["who"] == "prism" and e["played_ms"] > 0]
        said = [e for e in prism if not e.get("no_text_event")]
        m["calls where Prism asked the hotel for its own details"] += any(ASKS_FOR_OWN.search(e["text"]) for e in said)
        m["calls where Prism asked the hotel to confirm its own details"] += any(
            CONFIRM_Q.search(e["text"]) and own and own in agent.chars(e["text"]) for e in said
        )
        m["calls where Prism spelled the name with dashes"] += any(SPELLED_NAME.search(e["text"]) for e in said)
        m["calls with a filler"] += any(e.get("no_text_event") for e in prism)
        m["calls with talk into a menu, hold or voicemail"] += any(e["mode"] in QUIET_MODES for e in prism)
        answers = [e["text"] for e in r["timeline"] if e["who"] == "tool_result"]
        m["guard stopped a made-up number"] += any(a.startswith("Not recorded: the hotel never said") for a in answers)
        m["guard stopped an early hang-up"] += any(a == agent.NOT_ENDED for a in answers)
        tl = r["timeline"]
        for i, e in enumerate(tl):
            if e["who"] == "tool" and e["text"] == "record_outcome":
                staff = [x for x in tl[:i] if x["who"] == "hotel" and x.get("mode") == "staff"]
                if staff and re.search(r"moment|let me look|pull (that|it) up|bear with", staff[-1]["text"], re.I):
                    m["outcome recorded during a lookup"] += 1
                    break
    return m


def main() -> None:
    runs = [Path(p) for p in sys.argv[1:]]
    rows = [measure(run) for run in runs]
    names = [run.name for run in runs]
    width = max(len(k) for k in rows[0])
    print(f"{'':{width}}  " + "  ".join(f"{n:>22}" for n in names))
    # What each run ran against. Runs from before run.json existed show "-".
    infos = [json.loads((run / "run.json").read_text()) if (run / "run.json").exists() else {} for run in runs]
    for key, get in (
        ("started (UTC)", lambda i: i.get("started_at", "-")[:16]),
        ("commit", lambda i: (i.get("git") or {}).get("commit") or "-"),
        ("rimelabs-sdk", lambda i: i.get("rimelabs_sdk", "-")),
        ("calls at the same time", lambda i: (i.get("args") or {}).get("jobs", "-")),
    ):
        print(f"{key:{width}}  " + "  ".join(f"{get(i)!s:>22}" for i in infos))
    for key in rows[0]:
        print(f"{key:{width}}  " + "  ".join(f"{row[key]:>22}" for row in rows))


if __name__ == "__main__":
    main()
