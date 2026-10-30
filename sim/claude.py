"""Claude plays the hotel staff, writes rubrics, and judges calls. One JSON-returning helper serves all three."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import UTC, datetime

import anthropic

from hotel_agent import GUEST, Booking, grouped, spoken_date

from . import ROOT
from .scenarios import Scenario

MODEL = "claude-opus-5-5"

# Spend cap. Each request's cost goes into cache/claude-spend.json, kept per API key, and no new
# request starts once the key has spent within SPEND_MARGIN of SPEND_CAP: the margin covers requests
# already running. Set CLAUDE_SPEND_CAP to change the cap.
SPEND_CAP = float(os.getenv("CLAUDE_SPEND_CAP", "20"))
SPEND_MARGIN = 0.50
LEDGER = ROOT / "cache" / "claude-spend.json"
# Dollars per million tokens: input, output, cache read, 5-minute cache write. A fallback attempt
# bills at its own model's rates. A model not listed here is counted at the highest rate.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-opus-5": (5.00, 25.00, 0.50, 6.25),
    "claude-opus-4-8": (5.00, 25.00, 0.50, 6.25),
}
UNLISTED = (10.00, 50.00, 1.00, 12.50)


class SpendCapReached(RuntimeError):
    pass


def cost(response) -> float:
    """Dollars for one response. usage.iterations has one entry per attempt when a fallback ran."""
    total = 0.0
    for attempt in response.usage.iterations or [response.usage]:
        price_in, price_out, price_read, price_write = PRICES.get(str(getattr(attempt, "model", None) or MODEL), UNLISTED)
        total += (
            attempt.input_tokens * price_in
            + attempt.output_tokens * price_out
            + (attempt.cache_read_input_tokens or 0) * price_read
            + (attempt.cache_creation_input_tokens or 0) * price_write
        ) / 1e6
    return total


def spend(add: float = 0.0) -> dict:
    """This key's spend so far, after adding add. The file lock keeps parallel runs from losing an update."""
    key = hashlib.sha256(os.getenv("ANTHROPIC_API_KEY", "").encode()).hexdigest()[:12]
    LEDGER.parent.mkdir(exist_ok=True)
    LEDGER.touch()
    with open(LEDGER, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        ledger = json.loads(f.read() or "{}")
        entry = ledger.setdefault(key, {"dollars": 0.0, "requests": 0, "since": datetime.now(UTC).isoformat()})
        if add:
            entry["dollars"] += add
            entry["requests"] += 1
            f.seek(0)
            f.truncate()
            f.write(json.dumps(ledger, indent=2))
        return entry


async def ask_json(
    client: anthropic.AsyncAnthropic, *, system: str, prompt: str, schema: dict, effort: str, max_tokens: int = 8000
) -> dict:
    if (spent := spend()["dollars"]) >= SPEND_CAP - SPEND_MARGIN:
        raise SpendCapReached(f"Claude spend on this key is ${spent:.2f}, at the ${SPEND_CAP:.2f} cap. "
                              "Set CLAUDE_SPEND_CAP to raise it.")
    response = await client.with_options(max_retries=6).beta.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": prompt}],
    )
    spend(cost(response))
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined: {response.stop_details}")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claude ran out of output tokens")
    return json.loads(next(block.text for block in response.content if block.type == "text"))


REPLY = {
    "type": "object",
    "properties": {
        "say": {"type": "string", "description": "What you say next. Empty if you say nothing."},
        "then": {"type": "string", "enum": ["listen", "pause", "hang_up"]},
        "pause_seconds": {"type": "integer", "description": "With then=pause: how long you look something up."},
        "cancelled": {"type": "boolean", "description": "True only in the reply where you cancel the reservation."},
    },
    "required": ["say", "then", "pause_seconds", "cancelled"],
    "additionalProperties": False,
}


def booking_on_file(b: Booking) -> str:
    nights = "1 night" if b.nights == 1 else f"{b.nights} nights"
    return (
        f"- Guest: {GUEST}\n- Confirmation number: {grouped(b.confirmation)} (stored as {b.confirmation})\n"
        f"- Check-in: {spoken_date(b.check_in)}, {nights}\n- Room: {b.room or 'standard room'}"
    )


class Actor:
    """One member of hotel staff. Each turn sends the whole call so far; the system prompt stays fixed."""

    def __init__(self, client: anthropic.AsyncAnthropic, scenario: Scenario):
        b, staff = scenario.booking, scenario.staff
        assert staff is not None
        self.client = client
        self.system = f"""You are playing a member of hotel staff who answers the phone. This is a test: the caller is an AI voice agent, and we are checking how well it handles real hotel calls. Play your part the way this person really would. Do not make it easier for the caller, and do not make it harder than your character would.

Your character: {staff.name} at {b.hotel} in {b.city}. {staff.persona}

The guest's reservations in your system:
{chr(10).join(booking_on_file(x) for x in scenario.bookings)}

What you do with this booking: {staff.policy}

How to reply:
- You hear the caller through the phone, as text. Reply with exactly what you say next, as plain speech: no stage directions, no markdown, no emoji.
- Talk like a person on the phone: usually one or two short sentences.
- Write every code or number one character at a time with spaces, like "C X 4 8 1 9 2". Write money like "214 dollars".
- Check the confirmation number the caller gives against the one in your system. If it does not match, say you cannot find it and ask them to read it again.
- Set "then" to "pause" when you say something like "one moment while I pull that up", and set pause_seconds to how long you look (3 to 60). Set "then" to "hang_up" when the call is over and goodbyes are done. Otherwise set "then" to "listen", and pause_seconds to 0.
- Set "cancelled" to true only in the reply where you actually cancel the reservation.
- If the caller is silent, confusing, or repeats itself, react as a real person would."""

    async def next(self, transcript: list[tuple[str, str]], note: str) -> dict:
        lines = "\n".join(f"{'You' if who == 'you' else 'Caller'}: {text}" for who, text in transcript)
        prompt = f"The call so far:\n{lines or '(nothing yet)'}\n\n{note}\n\nWhat do you do next?".replace("\n\n\n", "\n\n")
        return await ask_json(self.client, system=self.system, prompt=prompt, schema=REPLY, effort="low", max_tokens=4000)
