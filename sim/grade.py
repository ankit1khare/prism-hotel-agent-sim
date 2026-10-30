"""Grade calls. Code checks what can be measured exactly; Claude judges the rest against a rubric.

Every failure gets a category and a subcategory:
- ear: Prism misheard or mistimed. transcription (wrong words or numbers), turn_taking (cut the
  person off, or talked while a recording or hold music played), noise.
- brain: Prism heard right but did the wrong thing. behavior, policy, tool_call_choice (wrong or
  missing tool), tool_call_arguments (right tool, wrong values), speech (rambling, unclear readouts).
"""

from __future__ import annotations

import asyncio
import json
import re
import statistics
from pathlib import Path

import jiwer

import hotel_agent as agent

from . import NOT_CALLS, ROOT
from .claude import ask_json
from .scenarios import BY_ID, Scenario

SUBCATEGORIES = {
    "ear": ["transcription", "turn_taking", "noise"],
    "brain": ["behavior", "policy", "tool_call_choice", "tool_call_arguments", "speech"],
}
ALL_SUBCATEGORIES = sorted({x for v in SUBCATEGORIES.values() for x in v})
QUIET_MODES = {"ivr", "hold", "voicemail"}
DIGIT_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]


def norm(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def spoken_norm(text: str) -> str:
    """Characters as Prism's transcript writes them, with number words turned back into digits."""
    words = {w: str(i) for i, w in enumerate(DIGIT_WORDS)} | {"oh": "0"}
    return norm(" ".join(words.get(w.lower(), w) for w in re.findall(r"[A-Za-z]+|\d", text or "")))


def wer_text(text: str) -> str:
    tokens = re.findall(r"[a-z]+|\d", (text or "").lower().replace("'", ""))
    return " ".join(DIGIT_WORDS[int(t)] if t.isdigit() else t for t in tokens)


def chars_in(result: dict) -> str:
    """The scenario's confirmation number, as letters and digits."""
    return agent.chars(BY_ID[result["scenario"]].booking.confirmation)


def check(id: str, name: str, passed: bool | None, category: str, sub: str, detail: str = "") -> dict:
    return {"id": id, "name": name, "passed": passed, "category": category, "subcategory": sub, "detail": detail}


def code_checks(result: dict, s: Scenario) -> tuple[list[dict], dict]:
    timeline = result["timeline"]
    tools = [e for e in timeline if e["who"] == "tool"]
    calls = [e["args"] for e in tools if e["text"] == "record_outcome"]
    answers = [e["text"] for e in timeline if e["who"] == "tool_result" and e.get("name") == "record_outcome"]
    outcomes = [a for a, answer in zip(calls, answers) if answer.startswith(agent.RECORDED)]
    early = [a for a, answer in zip(calls, answers) if answer.startswith("Not recorded yet")]
    made_up = [a for a, answer in zip(calls, answers) if answer.startswith("Not recorded: the hotel never said")]
    unnamed = [a for a, answer in zip(calls, answers) if answer.startswith("Not recorded: put the confirmation")]
    ended_early = [e for e in timeline if e["who"] == "tool_result" and e.get("name") == "end_call" and e["text"].startswith("Not ended")]
    heard = " ".join(e["text"] for e in timeline if e["who"] == "heard")
    hotel = [e for e in timeline if e["who"] == "hotel" and e["text"] != "[beep]"]
    prism = [e for e in timeline if e["who"] == "prism" and e["played_ms"] > 0]
    bookings, expects = s.bookings, s.expects
    many = len(bookings) > 1

    def outcome_for(b) -> dict:
        mine = [a for a in outcomes if not many or agent.chars(str(a.get("reservation") or "")) == agent.chars(b.confirmation)]
        return mine[-1] if mine else {}

    out = []
    if result.get("error"):
        out.append(check("ran", "The simulated call ran", False, "brain", "behavior", result["error"]))
    out.append(check("outcome_recorded", "Recorded one outcome per reservation",
                     len(outcomes) == len(bookings) and all(outcome_for(b) for b in bookings), "brain", "tool_call_choice",
                     f"{len(outcomes)} outcomes recorded for {len(bookings)} reservation(s)"))
    out.append(check("no_made_up_number", "Never tried to record a cancellation number the hotel did not say",
                     not made_up, "brain", "behavior",
                     f"tried {len(made_up)} times; the guard stopped it: {[a.get('cancellation_number') for a in made_up]}" if made_up else ""))
    out.append(check("ended_after_outcome", "Did not try to hang up before recording every outcome", not ended_early,
                     "brain", "tool_call_choice", f"tried {len(ended_early)} times; the guard stopped it" if ended_early else ""))
    out.append(check("waited_for_confirmation", "Waited for the hotel to confirm the number before recording it",
                     not early, "brain", "behavior",
                     f"tried to record {len(early)} times before a clear yes; the guard stopped it" if early else ""))
    if many:
        out.append(check("named_reservation", "Said which reservation each outcome was for", not unnamed, "brain",
                         "tool_call_arguments", f"left out the reservation {len(unnamed)} times; the guard stopped it" if unnamed else ""))

    for i, (b, expect) in enumerate(zip(bookings, expects)):
        got = outcome_for(b)
        tag, label = ("", "") if not many else (f"_{i + 1}", f"Reservation {i + 1}: ")
        out.append(check(f"status{tag}", f"{label}Status is {expect.status}", got.get("status") == expect.status, "brain",
                         "tool_call_arguments", f"recorded {got.get('status')!r}"))
        if expect.cancellation_number:
            want, have = norm(expect.cancellation_number), norm(got.get("cancellation_number", ""))
            said = want in norm(" ".join(e["text"] for e in hotel))
            heard_ok = want in spoken_norm(heard)
            if not said:  # the hotel never gave it, so any number Prism recorded was made up
                where, why = ("brain", "behavior"), "the hotel never said it"
            elif heard_ok:
                where, why = ("brain", "tool_call_arguments"), "Prism's transcript has it"
            else:
                where, why = ("ear", "transcription"), "Prism's transcript lacks it"
            out.append(check(f"cancellation_number{tag}", f"{label}Cancellation number captured exactly", have == want,
                             *where, f"expected {want}, recorded {have or 'nothing'}; {why}"))
        if expect.fee_dollars is not None:
            fee = str(expect.fee_dollars)
            heard_ok = fee in spoken_norm(heard) or fee in heard
            out.append(check(f"fee{tag}", f"{label}Fee of {fee} dollars recorded", fee in re.sub(r"\D", "", got.get("fee", "")),
                             "brain" if heard_ok else "ear", "tool_call_arguments" if heard_ok else "transcription",
                             f"recorded fee {got.get('fee', '')!r}"))
    if any(outcome_for(b).get("status") == "cancelled" for b in bookings):
        out.append(check("real_cancellation", "Recorded a cancellation only after the hotel made one",
                         result["hotel_cancelled"], "brain", "behavior",
                         "" if result["hotel_cancelled"] else "Prism recorded a cancellation the hotel never made"))
    if s.expect.status != "cancelled":
        out.append(check("not_cancelled", "Did not let the hotel cancel", not result["hotel_cancelled"], "brain",
                         "policy", "the hotel cancelled the booking" if result["hotel_cancelled"] else ""))

    if s.ivr:
        presses = [e["args"].get("digits", "") for e in tools if e["text"] == "press_digits"]
        first = presses[0][:1] if presses else ""
        out.append(check("menu_key", "Pressed a right key in the phone menu", first in s.ivr.good_keys, "brain",
                         "tool_call_arguments", f"pressed {presses or 'nothing'}; right keys {sorted(s.ivr.good_keys)}"))

    to_recordings = [e for e in prism if e["mode"] in QUIET_MODES]
    out.append(check("quiet_for_recordings", "Said nothing to recordings or hold music", not to_recordings,
                     "brain", "behavior", "; ".join(
                         f"[{e['mode']}{', filler' if e.get('no_text_event') else ''}] {e['text']}" for e in to_recordings)))

    staff = [e for e in hotel if e["mode"] == "staff"]
    cut_ins = [p for p in prism for h in staff if h["t_ms"] < p["t_ms"] < h["end_ms"] - 150]
    out.append(check("no_cut_ins", "Never started talking while the hotel was mid-sentence", not cut_ins,
                     "ear", "turn_taking", "; ".join(f"at {p['t_ms'] / 1000:.1f} s: {p['text'][:60]}" for p in cut_ins)))

    ended = any(e["text"] == "end_call" for e in tools) or result["hangup"] in ("Prism ended the call", "Prism's goodbye finished")
    out.append(check("hung_up", "Ended the call", ended, "brain",
                     "tool_call_choice", f"call ended because: {result['hangup']}"))

    said = wer_text(" ".join(e["text"] for e in hotel if e["mode"] in ("staff", "ivr")))
    got = wer_text(heard)
    wer = jiwer.wer(said, got) if said and got else (1.0 if said else 0.0)
    out.append(check("hearing", "Transcript matches what the hotel said (WER under 15%)", wer < 0.15, "ear",
                     "transcription", f"word error rate {wer:.0%}"))

    gaps = []
    for h in staff:
        nxt = [p["t_ms"] for p in prism if p["t_ms"] >= h["end_ms"] - 150]
        later = [x["t_ms"] for x in staff if x["t_ms"] > h["t_ms"]]
        if nxt and (not later or nxt[0] < later[0]):
            gaps.append(nxt[0] - h["end_ms"])
    think = [e["actor_ms"] for e in staff if "actor_ms" in e]
    metrics = {
        "duration_s": round(result["duration_ms"] / 1000),
        "wer": round(wer, 3),
        "reply_gap_ms_median": round(statistics.median(gaps)) if gaps else None,
        "reply_gap_ms_max": max(gaps) if gaps else None,
        "staff_think_ms_median": round(statistics.median(think)) if think else None,
    }
    return out, metrics


RUBRIC = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "category": {"type": "string", "enum": ["ear", "brain"]},
                    "subcategory": {"type": "string", "enum": ALL_SUBCATEGORIES},
                    "critical": {"type": "boolean"},
                },
                "required": ["id", "text", "category", "subcategory", "critical"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


async def rubric_for(claude, s: Scenario) -> list[dict]:
    """Claude writes each scenario's rubric once. It is saved in rubrics/, where people can edit it."""
    path = ROOT / "rubrics" / f"{s.id}.json"
    if path.exists():
        return json.loads(path.read_text())["items"]
    expected = f"record_outcome with status {s.expect.status}"
    if s.expect.cancellation_number:
        expected += f", cancellation number {s.expect.cancellation_number}"
    if s.expect.fee_dollars is not None:
        expected += f", fee {s.expect.fee_dollars} dollars"
    prompt = f"""Write the grading rubric for one test scenario.

The agent under test is Prism, a voice AI that phones hotels to cancel reservations for a guest. Its instructions:
<instructions>
{s.instructions()}
</instructions>

The scenario: {s.title}. {s.brief}
Expected outcome: {expected}.

Code already checks: the recorded status, cancellation number and fee; the phone-menu key; silence during recordings; starting to talk while the hotel is mid-sentence; ending the call; and transcript word error rate. Do not repeat those.

Write 6 to 9 items about how the conversation should go in this scenario: what Prism must say or not say, how it handles this hotel's behavior, and its rules. Each item must be pass/fail checkable from a timestamped transcript. Mark an item critical only if failing it would harm the guest or mislead the hotel (for example sharing made-up details, agreeing to a fee, or a wrong cancellation); style and wording items are never critical. Use short snake_case ids."""
    data = await ask_json(claude, system="You write precise, checkable rubrics for testing voice agents.",
                          prompt=prompt, schema=RUBRIC, effort="high")
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    return data["items"]


VERDICT = {
    "type": "object",
    "properties": {
        "rubric": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["pass", "fail", "n/a"]},
                    "evidence": {"type": "string"},
                },
                "required": ["id", "verdict", "evidence"],
                "additionalProperties": False,
            },
        },
        "other_errors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": ["ear", "brain"]},
                    "subcategory": {"type": "string", "enum": ALL_SUBCATEGORIES},
                    "severity": {"type": "string", "enum": ["minor", "major", "critical"]},
                    "description": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["category", "subcategory", "severity", "description", "evidence"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["rubric", "other_errors", "summary"],
    "additionalProperties": False,
}


def transcript(result: dict) -> str:
    lines = []
    for e in result["timeline"]:
        at = f"{e['t_ms'] / 1000:.1f}s"
        who = e["who"]
        if who == "hotel":
            cut = " [cut off]" if e.get("cut") else ""
            lines.append(f"[{at}-{e['end_ms'] / 1000:.1f}s] HOTEL ({e['mode']}){cut}: {e['text']}")
        elif who == "prism":
            cut = f" [cut off after {e['played_ms'] / 1000:.1f} of {e['total_ms'] / 1000:.1f} s]" if e["cut"] else ""
            filler = " [no text event: a line Prism's platform adds around tool calls; words from Rime STT]" if e.get("no_text_event") else ""
            lines.append(f"[{at}-{e['end_ms'] / 1000:.1f}s] PRISM SAID (hotel mode: {e['mode']}){cut}{filler}: {e['text']}")
        elif who == "heard":
            lines.append(f"[{at}] PRISM HEARD (its transcript of the hotel): {e['text']}")
        elif who == "tool":
            lines.append(f"[{at}] PRISM CALLED {e['text']}({json.dumps(e.get('args', {}))})")
        elif who == "tool_result":
            lines.append(f"[{at}] tool result: {e['text']}")
        else:
            lines.append(f"[{at}] {who.upper()}: {e['text']}")
    return "\n".join(lines)


async def judge(claude, result: dict, s: Scenario, rubric: list[dict], checks: list[dict]) -> dict:
    items = "\n".join(f"- {i['id']}: {i['text']}" for i in rubric)
    checked = "\n".join(f"- {c['name']}: {'pass' if c['passed'] else 'FAIL'}. {c['detail']}" for c in checks)
    prompt = f"""Grade this simulated phone call.

Prism's instructions:
<instructions>
{s.instructions()}
</instructions>

Scenario: {s.title}. {s.brief}

The call. HOTEL lines are exactly what the hotel said; PRISM HEARD lines are Prism's own transcript of them:
<call>
{transcript(result)}
</call>

Code checks already run (do not grade these again):
{checked}

Rubric items:
{items}

For each rubric item, give pass, fail, or n/a, with short evidence that quotes the call and its time. Then list other real errors the rubric and code checks missed, each with a category and subcategory:
- ear: Prism misheard (its transcript differs from what the hotel said, in a way that mattered) or mistimed (talked over someone, talked to a recording, missed speech).
- brain: Prism heard correctly but chose wrong: rule breaks, wrong or missing tool calls, made-up facts, unclear or long speech.
If a mistake came from a mishearing, it is an ear error even when the behavior looks wrong. Lines marked "no text event" are spoken by Prism's platform around tool calls, not by the agent's instructions: grade them as Prism's behavior, and say in the evidence that they were fillers. Do not list problems caused only by the simulated hotel. Finish with a one or two sentence summary."""
    return await ask_json(claude, system="You grade test calls for a voice AI agent strictly and fairly.",
                          prompt=prompt, schema=VERDICT, effort="high", max_tokens=16000)


def place(category: str, sub: str) -> tuple[str, str]:
    """Claude sometimes pairs a subcategory with the other category; the subcategory decides."""
    if sub in SUBCATEGORIES.get(category, []):
        return category, sub
    return next((c for c, subs in SUBCATEGORIES.items() if sub in subs), category), sub


def failures(graded: dict) -> list[dict]:
    """Every failure in one list, for counting by category."""
    out = [
        {"source": "check", "category": c["category"], "subcategory": c["subcategory"], "what": c["name"],
         "detail": c["detail"], "critical": re.sub(r"_\d+$", "", c["id"]) in ("ran", "status", "cancellation_number", "not_cancelled", "real_cancellation")}
        for c in graded["checks"]
        if c["passed"] is False
    ]
    rubric = {i["id"]: i for i in graded.get("rubric", [])}
    for v in graded.get("judge", {}).get("rubric", []):
        item = rubric.get(v["id"])
        if v["verdict"] == "fail" and item:
            out.append({"source": "rubric", "category": item["category"], "subcategory": item["subcategory"],
                        "what": item["text"], "detail": v["evidence"], "critical": item["critical"]})
    for f in out:
        f["category"], f["subcategory"] = place(f["category"], f["subcategory"])
    for e in graded.get("judge", {}).get("other_errors", []):
        e["category"], e["subcategory"] = place(e["category"], e["subcategory"])
        out.append({"source": "judge", "category": e["category"], "subcategory": e["subcategory"],
                    "what": e["description"], "detail": e["evidence"], "critical": e["severity"] == "critical"})
    return out


async def grade_run(out: Path, results: list[dict], claude, judge_calls: bool = True, reuse: bool = False) -> list[dict]:
    """Grade every call. With reuse, keep the Claude judge's earlier verdicts and redo only the code checks."""
    earlier = {}
    if reuse and (out / "grades.json").exists():
        earlier = {g["name"]: g for g in json.loads((out / "grades.json").read_text())}
    from .report import write_report

    if judge_calls:  # one scenario at a time, so repeats of a scenario share one rubric
        for s_id in dict.fromkeys(r["scenario"] for r in results):
            await rubric_for(claude, BY_ID[s_id])

    limit = asyncio.Semaphore(4)  # the account limits simultaneous Claude requests

    async def one(result):
        async with limit:
            return await grade_one(result)

    async def grade_one(result):
        s = BY_ID[result["scenario"]]
        checks, metrics = code_checks(result, s)
        graded = {"name": result.get("name", s.id), "scenario": s.id, "checks": checks, "metrics": metrics}
        before = earlier.get(graded["name"], {})
        if before.get("judge"):
            graded["rubric"], graded["judge"] = before["rubric"], before["judge"]
        elif judge_calls and not result.get("error"):
            graded["rubric"] = await rubric_for(claude, s)
            graded["judge"] = await judge(claude, result, s, graded["rubric"], checks)
        graded["failures"] = failures(graded)
        graded["passed"] = not graded["failures"]
        return graded

    grades = await asyncio.gather(*(one(r) for r in results))
    (out / "grades.json").write_text(json.dumps(grades, indent=2))
    write_report(out, results, grades)
    return grades


async def backfill(run: Path, results: list[dict], rime) -> None:
    """Fill Prism lines that have no words, such as fillers, from the call recording."""
    import wave

    import numpy as np

    from . import audio

    for result in results:
        empty = [e for e in result["timeline"] if e["who"] == "prism" and not e["text"] and e["played_ms"] > 250]
        path = run / f"{result.get('name', result['scenario'])}.wav"
        if not empty or not path.exists():
            continue
        with wave.open(str(path)) as f:
            prism = np.frombuffer(f.readframes(f.getnframes()), "<i2").reshape(-1, 2)[:, 1].copy()
        for e in empty:
            clip = prism[int(e["t_ms"] * 16) : int(e["end_ms"] * 16)].tobytes()
            e["text"] = (await rime.stt.transcribe(audio.wav(clip, audio.RATE), language="en")).text
            e["no_text_event"] = True
        (run / f"{result.get('name', result['scenario'])}.json").write_text(json.dumps(result, indent=2))


async def main() -> None:
    """Grade an earlier run again: uv run python -m sim.grade runs/<folder>"""
    import argparse

    import anthropic

    from . import load_env

    load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    parser.add_argument("--no-judge", action="store_true")
    parser.add_argument("--reuse-judge", action="store_true", help="keep the judge's earlier verdicts; redo the code checks")
    args = parser.parse_args()
    from rimelabs_sdk import Rime

    results = [json.loads(p.read_text()) for p in sorted(args.run.glob("*.json")) if p.name not in NOT_CALLS]
    async with Rime() as rime:
        await backfill(args.run, results, rime)
    await grade_run(args.run, results, anthropic.AsyncAnthropic(), judge_calls=not args.no_judge, reuse=args.reuse_judge)
    print(f"Report: {args.run / 'report.html'}")


if __name__ == "__main__":
    asyncio.run(main())
