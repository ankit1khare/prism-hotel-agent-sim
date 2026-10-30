"""The hotels Prism calls. Every hotel, person, and number here is made up."""

from __future__ import annotations

from dataclasses import dataclass, field

from hotel_agent import Booking


@dataclass(frozen=True)
class Staff:
    name: str
    voice: str  # Rime voice
    persona: str  # how they sound and act
    policy: str  # what they will and will not do with this booking


@dataclass(frozen=True)
class Ivr:
    greeting: str
    menu: str
    keys: dict[str, str]  # key -> "desk", "repeat", or "say:<recording>"
    good_keys: frozenset[str]
    voice: str = "celeste"


@dataclass(frozen=True)
class Expect:
    status: str
    cancellation_number: str = ""
    fee_dollars: int | None = None


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    brief: str  # what happens and what a good call looks like, for the rubric writer
    booking: Booking
    answer: str  # "desk", "ivr", or "voicemail"
    expect: Expect
    staff: Staff | None = None
    ivr: Ivr | None = None
    hold_seconds: int = 0
    voicemail: str = ""
    voicemail_voice: str = "sage"
    tags: tuple[str, ...] = field(default_factory=tuple)
    more_bookings: tuple[Booking, ...] = ()  # more of the guest's reservations at this hotel
    expect_more: tuple[Expect, ...] = ()  # the right outcome for each of more_bookings
    card_last4: str = ""  # what Prism may share when asked
    email: str = ""

    @property
    def bookings(self) -> list[Booking]:
        return [self.booking, *self.more_bookings]

    @property
    def expects(self) -> list[Expect]:
        return [self.expect, *self.expect_more]

    def instructions(self) -> str:
        """Exactly what Prism is told on this call. The judge and the rubric writer read the same text."""
        from hotel_agent import build_instructions

        return build_instructions(self.bookings, card_last4=self.card_last4, email=self.email)


SCENARIOS = [
    Scenario(
        id="desk_free_cancel",
        title="Front desk answers, free cancellation",
        brief="A friendly front desk agent answers, takes the guest name and confirmation number, cancels for free, "
        "and reads out an alphanumeric cancellation number.",
        booking=Booking("Harborview Inn", "Portland, Maine", "2027-03-12", 2, "84122907", "one king room"),
        answer="desk",
        staff=Staff(
            "Dana",
            "luna",
            "Warm and quick. You have worked the front desk for years.",
            "This booking can be cancelled for free until 48 hours before arrival. When the caller has given the "
            "guest name and a matching confirmation number, cancel it and give the cancellation number C X 4 8 1 9 2. "
            "Read it once, and repeat it if asked.",
        ),
        expect=Expect("cancelled", "CX48192"),
        tags=("happy path",),
    ),
    Scenario(
        id="ivr_then_desk",
        title="Phone menu, hold music, then reservations",
        brief="A recorded menu answers. The caller should press 1 (reservations) or 2 (front desk) without talking "
        "to the recording, wait silently through hold music, then cancel with an agent who puts them on a short "
        "hold to look up the booking.",
        booking=Booking("The Belmont Hotel", "Chicago", "2027-04-02", 1, "7731Q045"),
        answer="ivr",
        ivr=Ivr(
            greeting="Thank you for calling The Belmont Hotel on Wacker Drive.",
            menu="For reservations, press 1. For the front desk, press 2. For Tavern 9, our restaurant, press 3. "
            "To hear these options again, press star.",
            keys={
                "1": "desk",
                "2": "desk",
                "0": "desk",
                "3": "say:Tavern 9 is open daily from 5 p.m. to 11 p.m.",
                "*": "repeat",
            },
            good_keys=frozenset({"1", "2"}),
        ),
        hold_seconds=15,
        staff=Staff(
            "Marcus",
            "orion",
            "Calm, friendly reservations agent. The call was transferred to you from the phone menu, so you answer "
            "with 'Reservations, this is Marcus.'",
            "Free cancellation. After the caller gives the confirmation number, say 'One moment while I pull that "
            "up' and pause for about 8 seconds. Then confirm the check-in date with the caller, cancel, and give "
            "cancellation number R 7 7 3 1 0.",
        ),
        expect=Expect("cancelled", "R77310"),
        tags=("phone menu", "hold"),
    ),
    Scenario(
        id="fee_nonrefundable",
        title="Non-refundable rate, fee quoted",
        brief="The agent says a 214 dollar fee applies and cannot be waived, then asks whether to cancel anyway. "
        "The caller should ask for a waiver once, decline the fee, not cancel, and record the fee.",
        booking=Booking("Seaside Grand", "San Diego", "2027-05-21", 3, "30598871", "two queen beds, ocean view"),
        answer="desk",
        staff=Staff(
            "Priya",
            "lyra",
            "Polite but firm. You follow the rate rules exactly.",
            "This booking was made at a non-refundable advance-purchase rate. Cancelling now costs the first night, "
            "214 dollars, and you cannot waive it; no manager can. Explain the fee clearly. If the caller asks for a "
            "waiver, say no kindly. Then ask: 'Would you like me to go ahead and cancel it with the fee?' Do not "
            "cancel unless the caller clearly agrees to the fee.",
        ),
        expect=Expect("fee_quoted", fee_dollars=214),
        tags=("fee", "pressure"),
    ),
    Scenario(
        id="third_party_refused",
        title="Hotel will only cancel with the guest",
        brief="The agent asks for card digits the caller does not have, then refuses to cancel for a third party "
        "and offers an email address. The caller should not invent details, should thank them, and record "
        "refused_third_party.",
        booking=Booking("The Larkspur Hotel", "Boston", "2027-02-26", 2, "LK6620953"),
        answer="desk",
        staff=Staff(
            "Greg",
            "walnut",
            "By the book, and a little wary of callers who are not the guest.",
            "For security, you cancel only if the guest is on the line, or the guest emails from the address on the "
            "booking. First ask for the last four digits of the card on file. Whatever the caller says, you will "
            "not cancel on this call: explain the policy, and offer that the guest can call, or email "
            "reservations at larkspur hotel dot com.",
        ),
        expect=Expect("refused_third_party"),
        tags=("refusal", "missing details"),
    ),
    Scenario(
        id="voicemail",
        title="Voicemail",
        brief="The call goes to voicemail. The caller should say nothing, leave no message, record voicemail, and "
        "hang up.",
        booking=Booking("Pinecrest Lodge", "South Lake Tahoe", "2027-01-15", 4, "2208815"),
        answer="voicemail",
        voicemail="Hi, you've reached the front desk at Pinecrest Lodge. We're helping other guests right now. "
        "Please leave your name, your number, and a short message after the tone, and we'll call you back.",
        expect=Expect("voicemail"),
        tags=("voicemail",),
    ),
    Scenario(
        id="asks_card_then_cancels",
        title="Busy agent: asks to repeat, asks for card details",
        brief="A fast-talking agent says she cannot find the confirmation number and asks for it again slowly, then "
        "asks for card digits and a billing zip code the caller does not have. The caller should read the number "
        "again clearly, say it does not have the card details without guessing, then cancel using name and "
        "check-in date and capture the cancellation number exactly.",
        booking=Booking("Hotel Ardent", "Austin", "2027-06-04", 2, "AU190477", "one king room"),
        answer="desk",
        staff=Staff(
            "Rosa",
            "celeste",
            "Fast-talking and busy, with a loud lobby behind you. You speak quickly and informally.",
            "When the caller first reads the confirmation number, say you are not finding it and ask them to read it "
            "slowly, even if it was right. Then ask for the last four digits of the card on file and the billing "
            "zip code. If the caller says they do not have them, ask for the guest's name and the check-in date "
            "instead; if those match, cancel and give cancellation number 4 5 K 2 Z 9. If the caller gives card "
            "digits or a zip code, say they do not match what you have.",
        ),
        expect=Expect("cancelled", "45K2Z9"),
        tags=("repeat", "missing details"),
    ),
]

SCENARIOS.append(
    Scenario(
        id="unsure_clerk_wrong_number",
        title="Unsure clerk offers a wrong number",
        brief="A new, unsure clerk asks the caller to confirm its details and suggests a wrong confirmation number. "
        "The caller should keep its own details, never ask the hotel for them, "
        "turn down the wrong number, read the right one again, and get the booking cancelled.",
        booking=Booking("Lakeview Suites", "Madison, Wisconsin", "2027-04-16", 1, "59031884"),
        answer="desk",
        staff=Staff(
            "Tyler",
            "andromeda",
            "New at the front desk and unsure of yourself. You hesitate and say 'um' a lot.",
            "Start with 'Lakeview Suites, um, this is Tyler.' Ask which reservation. When the caller reads the "
            "confirmation number, say you are not sure and ask whether they can confirm the guest's name and the "
            "number. Then suggest a wrong number yourself: 'Is it one two three four five?' If the caller agrees to "
            "the wrong number or repeats it, say you can't find anything under that, and wait. If the caller says no "
            "and reads the right number again, say 'let me look' and look it up for about 35 seconds without "
            "speaking. Then find the booking, confirm the check-in date, cancel it at no charge, and give "
            "cancellation number T 6 2 8 4. The first time the caller reads it back, answer only 'Hm.', as if you "
            "were distracted; when they ask again, say yes. If the caller reads it back wrong, correct it.",
        ),
        expect=Expect("cancelled", "T6284"),
        tags=("pressure",),
    )
)

SCENARIOS.append(
    Scenario(
        id="quiet_after_hello",
        title="Clerk says hello, then goes quiet",
        brief="The clerk answers with only 'Hello?' and, after the caller's first line, goes silent for about 50 "
        "seconds without saying they will look anything up. "
        "The caller should ask in a few words whether the clerk is still there, "
        "about 20 seconds into the silence and again if nothing comes back, never stay silent the whole time, "
        "and never hang up. When the clerk comes back, the caller cancels the booking and records the "
        "cancellation number only after the clerk confirms the read-back.",
        booking=Booking("Riverside Inn", "Savannah, Georgia", "2027-05-07", 2, "R7K4M2Q9", "1 King"),
        answer="desk",
        staff=Staff(
            "Dana",
            "fern",
            "Distracted: you are also helping a guest at the counter.",
            "Answer with only 'Hello?'. After the caller's first line, say nothing (leave say empty), set then to "
            "pause, and pause for 50 seconds, as if you turned to the guest at the counter. Do not say you will "
            "look anything up. When you come back, say 'Sorry about that, I'm here. How can I help?' Then find the "
            "booking, cancel it at no charge, and give cancellation number 4 1 9 0 6. When the caller reads it "
            "back correctly, say yes.",
        ),
        expect=Expect("cancelled", "41906"),
        tags=("silence",),
    )
)

# A guest can hold several reservations at one hotel, and a front desk may not be able to cancel a
# booking made on the brand's website.
SCENARIOS += [
    Scenario(
        id="two_bookings_same_hotel",
        title="Two reservations at one hotel, card and email check",
        brief="The guest has two reservations at this hotel. The agent asks for the last four digits of the card and "
        "the email on file before cancelling. The caller should give them only when asked, cancel both, and record "
        "each outcome with that reservation's confirmation number and its own cancellation number.",
        booking=Booking("Magnolia Court Hotel", "New Orleans", "2027-03-05", 1, "5HQ2R8TKW1", "1 King"),
        more_bookings=(Booking("Magnolia Court Hotel", "New Orleans", "2027-04-23", 1, "9MD4N7XQ3P", "1 Queen"),),
        answer="desk",
        staff=Staff(
            "Leila",
            "fern",
            "Friendly and organized. You like to verify the guest before you change anything.",
            "The guest has two reservations with you, both shown in your system. The card on file ends in 4 2 4 2 and "
            "the email on file is jordan dot r at example dot com. Before you cancel anything, ask for the last four "
            "digits of the card and the email on the reservation. If they match, cancel the reservation the caller "
            "asks about first and give cancellation number M G 4 8 2 1 for the March reservation, or M G 4 8 2 2 for "
            "the April one. Cancel only what the caller asks for, one at a time. If the card digits or email do not "
            "match, say so and do not cancel.",
        ),
        expect=Expect("cancelled", "MG4821"),
        expect_more=(Expect("cancelled", "MG4822"),),
        card_last4="4242",
        email="jordan.r@example.com",
        tags=("two reservations", "verification"),
    ),
    Scenario(
        id="redirect_to_brand",
        title="Front desk sends us to central reservations",
        brief="The front desk cannot cancel a booking made on the brand's website and gives a central reservations "
        "number. The caller should not argue or invent anything, should ask for or note the number, and record "
        "status redirected with the number in notes.",
        booking=Booking("Silver Strand Resort", "Las Vegas", "2027-03-18", 1, "LV55X210", "1 King"),
        answer="desk",
        staff=Staff(
            "Tom",
            "bond",
            "Helpful but bound by the rules. You have explained this many times.",
            "You can see the reservation, but it was booked on the brand's website, so the front desk cannot cancel "
            "it. Explain that, and say they need to call central reservations at 1 8 0 0, 5 5 5, 0 1 4 7, or cancel "
            "online on the brand's website. Do not cancel it, whatever the caller says.",
        ),
        expect=Expect("redirected"),
        tags=("redirect",),
    ),
]

BY_ID = {s.id: s for s in SCENARIOS}
