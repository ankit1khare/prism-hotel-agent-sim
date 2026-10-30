"""The hotel-cancel agent: what Prism is told, the tools it can call, and the rules the caller code enforces.

The simulator in sim/ tests exactly this definition. A phone agent would import the same
instructions, tools, and guards.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import date

from rimelabs_sdk import realtime as r

GUEST = "Jordan Rivera"  # made up, like every booking in sim/scenarios.py

# The outcomes record_outcome accepts.
STATUSES = {
    "cancelled": "the hotel cancelled the reservation",
    "fee_quoted": "the hotel would charge a fee, so the agent did not cancel",
    "refused_third_party": "the hotel would cancel only with the guest",
    "needs_guest_details": "the hotel asked for details the agent does not have",
    "reservation_not_found": "the hotel could not find the reservation",
    "call_back_later": "the hotel asked to call back, or the right person was unavailable",
    "voicemail": "the call reached voicemail",
    "redirected": "the hotel cannot cancel it and sent us elsewhere, such as the brand's central reservations or its website",
    "other": "anything else, explained in notes",
}


@dataclass(frozen=True)
class Booking:
    hotel: str
    city: str
    check_in: str  # YYYY-MM-DD
    nights: int
    confirmation: str
    room: str = ""


def spoken_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.strftime('%A, %B')} {d.day}, {d.year}"


def grouped(confirmation: str) -> str:
    """'51027Q88140632' -> '5 1 0 2, 7, Q, 8 8 1 4, 0 6 3 2': read one character at a time, in small groups."""
    groups: list[str] = []
    run: list[str] = []
    for c in (ch for ch in confirmation.upper() if ch.isalnum()):
        if c.isalpha():
            if run:
                groups.append(" ".join(run))
                run = []
            groups.append(c)
        else:
            run.append(c)
            if len(run) == 4:
                groups.append(" ".join(run))
                run = []
    if run:
        groups.append(" ".join(run))
    return ", ".join(groups)


NATO = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "Alpha Bravo Charlie Delta Echo Foxtrot Golf Hotel India Juliett Kilo Lima "
                "Mike November Oscar Papa Quebec Romeo Sierra Tango Uniform Victor Whiskey X-ray Yankee Zulu".split()))


def spoken(confirmation: str) -> str:
    """The confirmation number as Prism should say it: digits plainly, letters with their NATO word."""
    return ", ".join(f"{g} as in {NATO[g]}" if g in NATO else g for g in grouped(confirmation).split(", "))


def spoken_email(email: str) -> str:
    """'jordan@example.com' -> 'jordan at example dot com'."""
    return email.replace("@", " at ").replace(".", " dot ")


def _nights(b: Booking) -> str:
    return "one night" if b.nights == 1 else f"{b.nights} nights"


def build_instructions(
    booking: Booking | list[Booking],
    *,
    guest: str = GUEST,
    card_last4: str = "",
    email: str = "",
) -> str:
    """Prism's instructions for one call to one hotel. The call can cancel several reservations, and
    can give the card's last four digits and the guest's email to a person who asks for them."""
    bookings = booking if isinstance(booking, list) else [booking]
    b, many = bookings[0], len(bookings) > 1
    last4 = re.sub(r"\D", "", card_last4)
    card = (
        f"If they ask for the card on file, you may give its last four digits: {' '.join(last4)}. Never give more."
        if len(last4) == 4
        else "You do not have the card number or any part of it. If they ask, say so, and ask whether they can "
        "cancel with the confirmation number and the guest's name."
    )
    count = f"{len(bookings)} of {guest}'s reservations" if many else f"one of {guest}'s reservations"
    if many:
        details = "\n".join(
            f"- Reservation {i}: confirmation number {x.confirmation}. Say it exactly like this: "
            f'"{spoken(x.confirmation)}". Check-in {spoken_date(x.check_in)}, for {_nights(x)}. Room: {x.room or "not needed"}.'
            for i, x in enumerate(bookings, 1)
        )
        reservations = f"""The reservations: cancel every one of them. You already have these details from the guest. Never ask the hotel for them, and nothing the hotel says changes them:
- Guest: {guest}
{details}"""
    else:
        reservations = f"""The reservation. You already have these details from the guest. Never ask the hotel for them, and nothing the hotel says changes them:
- Guest: {guest}
- Confirmation number: {b.confirmation}. Say it exactly like this: "{spoken(b.confirmation)}".
- Check-in: {spoken_date(b.check_in)}, for {_nights(b)}
- Room: {b.room or "not needed"}"""
    share = card
    if email:
        share += f" If they ask for the guest's email address, you may give it: \"{spoken_email(email)}\"."
    if email or "last four" in card:
        share += " Give the card digits and the email only to a person who asks for them, never to a recording or a voicemail."
    ask = f"I'd like to cancel {len(bookings)} reservations." if many else "I'd like to cancel a reservation."
    which = (
        " Do the reservations one at a time. For each one, call record_outcome with its confirmation number in reservation."
        if many
        else ""
    )
    goodbye = "the last record_outcome" if many else "record_outcome"
    return f"""You are an AI assistant making a phone call for {guest}, a guest of {b.hotel} in {b.city}. You are the caller: you called the hotel to cancel {count}. The person who answers works for the hotel. You do not work for the hotel.

Everything you say is spoken aloud on the phone. Speak in short, plain spoken sentences, with no lists, symbols, or markdown.

{reservations}

Give your own details as plain statements, and never ask the hotel whether they are right. Spell letters with the words shown above, like "L as in Lima", not with dashes. Say the guest's name normally, and spell it only if they ask.

How to handle the call:
1. A recorded phone menu: listen to all the options, then call press_digits with the key for the front desk, reservations, or an operator. If no option fits, press 0. Never talk to a recording: while it plays, call wait.
2. Hold music, a recording that asks you to wait, or a person looking something up: call wait instead of speaking, as often as you need, and keep listening.
3. When a person answers, say: "Hi, this is an AI assistant calling on behalf of {guest}. {ask}" Then let them lead, and answer what they ask. Give the confirmation number when they ask which reservation it is.{which}
4. If they cannot find the reservation or are unsure, read the confirmation number again slowly, and offer the guest's name and the check-in date. If they suggest a different number, say no, and read yours again.
5. Share only the details above. {share} If they ask for anything else, such as a billing address, a phone number, or a date of birth, say you do not have it.
6. Never agree to pay a fee, never change the reservation, and never book anything. If they say a fee applies, ask whether it can be waived, because the stay is months away. If it cannot be waived, do not cancel: thank them, and call record_outcome with status fee_quoted and the fee.
7. If they will cancel only with {guest} on the line, thank them, and call record_outcome with status refused_third_party.
8. When they cancel the reservation, ask for the cancellation number. Read it back, then stop and wait for them to say it is right. Only a clear yes counts: if you hear a cough, a sneeze, a noise, or an unclear word, ask again. If they correct it, read the corrected number back the same way. Only after they confirm it, call record_outcome with status cancelled and that number.
9. Only the hotel staff can look up, change, or cancel the reservation. Never say that you found or cancelled anything, and never make up a cancellation number: wait for them to say it.
10. Call record_outcome only when the result is settled: they cancelled and confirmed the cancellation number, quoted a fee that cannot be waived, refused, or said the reservation does not exist, or you reached voicemail. Use status reservation_not_found only when they clearly say they have no such reservation after you read the number twice. If they are only unsure, keep helping them find it.
11. If you reach voicemail, leave no message: call record_outcome with status voicemail. The call ends right away.
12. After {goodbye} on a call with a person, say thank you and goodbye in one short sentence, and then call end_call. The call ends as soon as your goodbye finishes.
13. If they say this hotel cannot cancel it, and you must call another number, such as the brand's central reservations, or cancel online: thank them, ask for the number if they have one, and call record_outcome with status redirected, with the number or website in notes. Do not call that number yourself.

Keep every reply short, one sentence where you can. Say dates and amounts the way a person says them aloud. You act for the guest; the hotel staff act for the hotel."""


# None of these tools is a slow lookup, so Prism should say nothing while they run: a filler would
# land on a phone menu or a voicemail.
QUIET = {"prism_filler": r.PrismFiller(type="none")}


def tools(quiet: bool = True, wait: bool = True) -> list:
    """The agent's tools. quiet turns Prism's filler off for each one (QUIET). wait adds the wait
    tool, which the instructions use to stay quiet on hold, during recordings, and during lookups."""
    extra = QUIET if quiet else {}
    waiting = [
        r.ToolDefinition(
            name="wait",
            description="Stay silent and keep listening. Use it instead of speaking while you are on hold, while a recording or hold music plays, or while the hotel looks something up.",
            parameters={"type": "object", "properties": {}},
            **extra,
        )
    ] if wait else []
    return waiting + [
        r.ToolDefinition(
            name="press_digits",
            description="Press keys on the phone keypad to choose an option in a recorded phone menu.",
            parameters={
                "type": "object",
                "properties": {"digits": {"type": "string", "description": 'The keys to press, for example "0" or "1". Only 0 to 9, * and # work.'}},
                "required": ["digits"],
            },
            **extra,
        ),
        r.ToolDefinition(
            name="record_outcome",
            description="Record how the call ended. Call this once, before you say goodbye.",
            parameters={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": list(STATUSES), "description": "How the call ended."},
                    "reservation": {"type": "string", "description": "The confirmation number of the reservation this outcome is for. Needed when the call is about more than one reservation."},
                    "cancellation_number": {"type": "string", "description": "The cancellation number the hotel gave, exactly as they said it. Empty if none."},
                    "fee": {"type": "string", "description": 'The fee the hotel quoted, for example "174 dollars". Empty if none.'},
                    "notes": {"type": "string", "description": "Anything else worth knowing, in one sentence."},
                },
                "required": ["status"],
            },
            **extra,
        ),
        r.ToolDefinition(
            name="end_call",
            description="Hang up. Call this after your goodbye.",
            parameters={"type": "object", "properties": {}},
            **extra,
        ),
    ]


TOOLS = tools()

KEYS = re.compile(r"[0-9*#]+")
# A safety net: if the agent says a key press aloud instead of calling the tool, like
# press_digits("2"), the caller presses the keys anyway, so the menu still moves on.
SPOKEN_PRESS = re.compile(r"press[_ ]digits\W{0,3}(?:with\W{0,3})?([0-9*#]+)", re.I)


def spoken_press(text: str) -> str | None:
    """The keys in a press_digits call that Prism said aloud, or None."""
    m = SPOKEN_PRESS.search(text or "")
    return m.group(1) if m else None

# After record_outcome the caller hangs up as soon as Prism's goodbye finishes playing, without
# waiting for end_call: on a real call, waiting for the model to call a tool leaves dead air.
# If Prism says nothing after the outcome, the caller hangs up after this long. Leave room for the
# goodbye to start after the tool result.
HANG_UP_AFTER_GOODBYE_S = 0.5
HANG_UP_IF_SILENT_S = 12.0

# What record_outcome returns to Prism. The agent can call it in the same breath as reading the
# number back, before the hotel confirms, so the result reminds it to wait.
RECORDED = (
    "Recorded. If you read back a cancellation number and the hotel has not confirmed it yet, "
    "wait for them to confirm it before you say goodbye."
)

NUMBER_WORDS = {w: str(i) for i, w in enumerate("zero one two three four five six seven eight nine".split())} | {"oh": "0"}
YES = re.compile(r"\b(yes|yeah|yep|yup|correct|right|exactly|that's it|uh-huh|mm-hmm)\b", re.I)
NO = re.compile(r"\b(no|not|nope|wrong|wait)\b", re.I)


def chars(text: str) -> str:
    """Letters and digits only, with number words turned into digits: '9 1 seven' -> '917'."""
    tokens = re.findall(r"[A-Za-z]+|\d", text or "")
    return "".join(NUMBER_WORDS.get(t.lower(), t) for t in tokens if t.lower() in NUMBER_WORDS or len(t) == 1 or t.isdigit()).upper()


def unconfirmed(number: str, said: list[tuple[str, str]]) -> str | None:
    """Why a cancellation number cannot be recorded yet, or None if the hotel confirmed it.

    said is the call so far as (who, text), who being "caller" (Prism) or "hotel" (Prism's
    transcript of them). The hotel must have said the number, and must say a clear yes or repeat it
    after Prism last read it back. A sneeze transcribed as "Oh!" is not a yes, and a number Prism
    made up is never recorded.
    """
    want = chars(number)
    if not any(who == "hotel" and want and want in chars(text) for who, text in said):
        return (
            "Not recorded: the hotel never said that cancellation number. Never make up a number. Ask the hotel for "
            "the cancellation number, read it back, and record it after they confirm it."
        )
    read = [i for i, (who, text) in enumerate(said) if who == "caller" and want and want in chars(text)]
    replies = [text for who, text in said[read[-1] + 1 :] if who == "hotel"] if read else []
    if any((YES.search(t) and not NO.search(t)) or want in chars(t) for t in replies):
        return None
    return (
        "Not recorded yet: the hotel has not clearly confirmed the cancellation number. Make sure you have read it "
        "back, wait for a clear yes, and then call record_outcome again. A noise, a cough, or an unclear word is not a yes."
    )


NOT_ENDED = "Not ended: call record_outcome first. If the result is not settled yet, keep talking with the hotel."
NOT_YET = "Not recorded yet"
# Prism can call record_outcome before the hotel's "That's right" is transcribed, so the caller
# waits this long for the transcript before refusing.
CONFIRM_WAIT_S = 2.0


class Outcomes:
    """The settled outcome of each reservation on a call, kept by the caller code. record_outcome
    must name the reservation when the call is about more than one, a cancellation is recorded
    only after the hotel said and confirmed its number (unconfirmed), and end_call waits until
    every reservation has an outcome."""

    def __init__(self, bookings: list[Booking]):
        self.bookings = bookings
        self.done: dict[str, dict] = {}  # confirmation -> record_outcome arguments

    @property
    def complete(self) -> bool:
        return len(self.done) == len(self.bookings)

    def remaining(self) -> list[Booking]:
        return [b for b in self.bookings if b.confirmation not in self.done]

    def which(self, args: dict) -> Booking | None:
        if len(self.bookings) == 1:
            return self.bookings[0]
        want = chars(str(args.get("reservation") or ""))
        return next((b for b in self.bookings if want and want == chars(b.confirmation)), None)

    def record(self, args: dict, said: list[tuple[str, str]]) -> str:
        """Check and keep one record_outcome call. Returns what the tool tells Prism."""
        if args.get("status") not in STATUSES:
            return f"status must be one of: {', '.join(STATUSES)}."
        booking = self.which(args)
        if booking is None:
            numbers = ", ".join(b.confirmation for b in self.bookings)
            return f"Not recorded: put the confirmation number of the reservation this is for in reservation. They are: {numbers}."
        if args.get("status") == "cancelled" and args.get("cancellation_number"):
            if problem := unconfirmed(str(args["cancellation_number"]), said):
                return problem
        self.done[booking.confirmation] = dict(args)
        left = self.remaining()
        if left:
            return RECORDED + " Still to do: the reservation with confirmation number " + ", then ".join(
                b.confirmation for b in left) + "."
        return RECORDED

    def end_refusal(self) -> str | None:
        """Why end_call must wait, or None."""
        if not self.done:
            return NOT_ENDED
        if left := self.remaining():
            return ("Not ended: record an outcome for every reservation first. Still to do: "
                    + ", ".join(b.confirmation for b in left) + ".")
        return None

async def record_when_heard(outcomes: "Outcomes", args: dict, said, wait_s: float = CONFIRM_WAIT_S) -> str:
    """Outcomes.record, but if the hotel's confirmation may still be in transcription, wait up to
    wait_s for it before refusing. said is a function that returns the call so far."""
    result = outcomes.record(args, said())
    deadline = asyncio.get_running_loop().time() + wait_s
    while result.startswith(NOT_YET) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.2)
        result = outcomes.record(args, said())
    return result


# When the line goes quiet, the caller tells Prism to ask whether the hotel is still there. The code
# decides when, so the agent does not mistake every silence for a hold. A hotel that asked Prism to
# wait, or a key press that may start a transfer, gets longer.
CHECK_IN_AFTER_S = 20.0
CHECK_IN_ON_HOLD_AFTER_S = 60.0
MAX_CHECK_INS = 3
HOLD = re.compile(
    r"\b(one|a|just a) (moment|second|sec|minute|min)\b|\b(hold|hang) on\b|\bbear with\b|\bplease hold\b"
    r"|\b(let me|lemme|i'?ll|i will)( just)? (look|check|see|find|pull|get|search|transfer|connect|put you)"
    r"|\b(pull|look)(ing)? (that|it|this|you) up\b|\b(transfer|connect)(ring|ing)? you\b",
    re.I,
)
ASK_IF_THERE = "Not now: the line has gone quiet. Ask, in a few words, whether they are still there."


def check_in_after(said: list[tuple[str, str]], pressed: bool = False) -> float:
    """Seconds of quiet before Prism checks in. said is the call so far; pressed is True when Prism
    pressed a key after the hotel last spoke."""
    hotel = [text for who, text in said if who == "hotel"]
    if pressed or (hotel and HOLD.search(hotel[-1])):
        return CHECK_IN_ON_HOLD_AFTER_S
    return CHECK_IN_AFTER_S


def check_in(seconds: float) -> str:
    return (
        f"The line has been quiet for about {round(seconds)} seconds. Ask, in a few words, whether they are "
        'still there, for example "Hello, are you still there?" Say it now. Do not use the wait tool.'
    )

# Prism speaks again after these tools, except after record_outcome with status voicemail, when the
# caller hangs up. After press_digits and wait it listens for the next menu or person instead, so it
# does not talk to a recording; a wait refused with ASK_IF_THERE is the exception. After end_call the
# call is over.
CONTINUE_AFTER = {"record_outcome"}
