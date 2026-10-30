"""One simulated phone call: Prism calls a fake hotel. Both sides run in this process; no phone network.

The hotel is two adapters for the SDK's conversation runner:
- Line, the source: what Prism hears. A frame every 20 ms, in real time: hotel speech, hold music,
  or quiet line noise.
- Speaker, the playback: where Prism's replies play, in real time, so interruptions and playback
  reports behave as they would on a phone.
Claude plays the staff (sim/claude.py), Rime TTS gives the hotel its voices, and Prism runs the
hotel_agent definition: the same instructions and tools a phone agent would use.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from rimelabs_sdk import RimeInputError
from rimelabs_sdk import realtime as r

import hotel_agent as agent

from . import audio
from .claude import Actor
from .scenarios import Scenario

FRAME_S = 0.02
QUIET_MODES = {"ivr", "hold", "voicemail"}  # a recording is playing, so Prism should say nothing
REPLY_GAP_MS = 700  # how long staff wait after Prism stops before they answer
SILENCE_MS = 8000  # how long staff wait on a silent caller before they speak


@dataclass
class Options:
    phone: bool = False  # 8 kHz mu-law line, like a Twilio bridge
    voice: str = "astra"  # Prism's voice
    max_seconds: int = 300
    eagerness: str | None = None
    end_of_turn_ms: int | None = None
    endpoint: str | None = None
    label: str = ""  # a name for the run, such as "before" or "after" a change to the instructions
    fillers: str = "off"  # "off": prism_filler none on every tool; "default": the server decides


class Line:
    """The source: what Prism hears."""

    def __init__(self, call: Call, phone: bool):
        self.call = call
        self.phone = phone
        self.rate = 8000 if phone else audio.RATE
        self.frame = int(self.rate * 2 * FRAME_S)
        self.quiet = audio.Loop(audio.line_noise(self.rate, phone))
        self.background = self.quiet
        self.filter = audio.Phone() if phone else None
        self.pending = bytearray()
        self.playing: list[dict] = []
        self.recording = bytearray()
        self.started_ms: float | None = None
        self.next_at: float | None = None
        self.closed = False

    def ms(self, size: int) -> float:
        return size / (self.rate * 2) * 1000

    @property
    def speaking(self) -> bool:
        return bool(self.pending)

    def feed(self, entry: dict, speech16k: bytes) -> None:
        """Queue more of a hotel line. Lines stream in from TTS, so this runs once per chunk."""
        if entry.get("cut") or not speech16k:
            return
        data = self.filter(speech16k) if self.filter else speech16k
        now = self.call.now_ms()
        if "t_ms" not in entry:
            entry["t_ms"] = round(now + self.ms(len(self.pending)))
            self.playing = [e for e in self.playing if e["end_ms"] > now]
            self.playing.append(entry)
        self.pending += data
        entry["end_ms"] = round(now + self.ms(len(self.pending)))

    def cut(self) -> None:
        """Stop the hotel mid-sentence, as a phone menu does when a key is pressed."""
        now = self.call.now_ms()
        for entry in self.playing:
            if entry["end_ms"] > now:
                entry["end_ms"] = round(max(now, entry["t_ms"]))
                entry["cut"] = True
        self.pending.clear()
        self.playing.clear()

    async def read(self) -> r.AudioChunk | None:
        loop = asyncio.get_running_loop()
        if self.closed:
            return None
        now = loop.time()
        if self.next_at is None or now - self.next_at > 0.2:
            self.next_at = now  # first frame, or the loop stalled: do not send a burst
        self.next_at += FRAME_S
        if self.next_at > now:
            await asyncio.sleep(self.next_at - now)
        if self.closed:
            return None
        if self.started_ms is None:
            self.started_ms = self.call.now_ms()
        if self.pending:
            data = bytes(self.pending[: self.frame])
            del self.pending[: self.frame]
            data += self.quiet(self.frame - len(data))
        else:
            data = self.background(self.frame)
        self.recording += data
        return r.AudioChunk(data=data, format=r.PCMFormat(sample_rate=self.rate))


class Played:
    """One Prism output. The speaker plays it at real speed and reports how much was heard."""

    def __init__(self, ref: r.OutputRef):
        self.ref = ref
        self.buf = bytearray()
        self.rate = 24000
        self.cursor = 0
        self.ended = False
        self.cut = False
        self.started_ms: float | None = None
        self.finished_ms: float | None = None
        self.receipt = asyncio.get_running_loop().create_future()
        self.receipt.add_done_callback(lambda f: f.cancelled() or f.exception())

    @property
    def played_ms(self) -> float:
        return self.cursor / (self.rate * 2) * 1000

    @property
    def total_ms(self) -> float:
        return len(self.buf) / (self.rate * 2) * 1000

    def write(self, chunk: r.AudioChunk) -> None:
        if not self.receipt.done():
            self.rate = chunk.format.sample_rate
            self.buf += chunk.data

    def end(self) -> None:
        self.ended = True

    async def completion(self) -> float:
        return await asyncio.shield(self.receipt)


class Speaker:
    """The playback: Prism's replies, played in order, in real time, into the hotel's ear."""

    def __init__(self, call: Call):
        self.call = call
        self.queue: deque[Played] = deque()
        self.current: Played | None = None
        self.outputs: dict[r.OutputRef, Played] = {}
        self.last_audio_ms = 0.0
        self.segments: list[tuple[float, bytes, int]] = []  # for the recording

    @property
    def busy(self) -> bool:
        return self.current is not None or any(not p.receipt.done() for p in self.queue)

    def open(self, ref: r.OutputRef) -> Played:
        played = self.outputs[ref] = Played(ref)
        self.queue.append(played)
        return played

    def interrupt(self, refs) -> None:
        for ref in refs:
            played = self.outputs.get(ref)
            if played and not played.receipt.done():
                played.cut = True
                if played is not self.current:
                    self.finish(played)

    async def stop(self) -> None:
        for played in [*self.queue, self.current]:
            if played and not played.receipt.done():
                played.cut = True
        while self.current is not None:
            await asyncio.sleep(FRAME_S)

    def finish(self, played: Played) -> None:
        played.finished_ms = self.call.now_ms()
        if not played.receipt.done():
            played.receipt.set_result(played.played_ms)
        if played.started_ms is not None:
            self.call.played.append(played)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        last = loop.time()
        while True:
            await asyncio.sleep(FRAME_S)
            now = loop.time()
            elapsed, last = now - last, now
            if self.current is None:
                while self.queue and self.queue[0].receipt.done():
                    self.queue.popleft()
                if not self.queue:
                    continue
                self.current = self.queue.popleft()
            played = self.current
            if not played.cut:
                step = min(len(played.buf) - played.cursor, int(played.rate * elapsed) * 2)
                if step > 0:
                    at = self.call.now_ms() - elapsed * 1000
                    if played.started_ms is None:
                        played.started_ms = at
                    self.segments.append((at, bytes(played.buf[played.cursor : played.cursor + step]), played.rate))
                    played.cursor += step
                    self.last_audio_ms = self.call.now_ms()
            if played.cut or (played.ended and played.cursor >= len(played.buf)):
                self.finish(played)
                self.current = None


@dataclass
class ToolRound:
    calls: list = field(default_factory=list)
    abandoned: bool = False
    continuation: asyncio.Task | None = None


class Tools:
    """Runs Prism's tool calls when each response ends. This follows rime-sdk's ManualTools example
    (python/examples/realtime/manual_tools.py), with one change: Prism is asked to continue only
    after the tools in hotel_agent.CONTINUE_AFTER."""

    def __init__(self, session, tasks: asyncio.TaskGroup, execute):
        self.session, self.tasks, self.execute = session, tasks, execute
        self.rounds: dict[str, ToolRound] = {}
        self.pending: set[asyncio.Task] = set()
        self.epoch = 0

    def on_event(self, event) -> None:
        payload = event.payload
        if isinstance(payload, (r.SpeechStarted, r.ResponseStarted)):
            self.epoch += 1
            if isinstance(payload, r.ResponseStarted):
                self.rounds[payload.response.response_id] = ToolRound()
        elif isinstance(payload, r.ResponseAbandoned):
            if state := self.rounds.get(payload.response.response_id):
                state.abandoned = True
                if state.continuation:
                    state.continuation.cancel()
        elif isinstance(payload, r.ToolCall):
            self.rounds.setdefault(payload.call.response_id, ToolRound()).calls.append(payload)
        elif isinstance(payload, r.ResponseEnded):
            response_id = payload.response.response_id
            state = self.rounds.get(response_id)
            if state and state.calls and not state.abandoned and payload.status == "completed":
                task = self.tasks.create_task(self.finish(payload.response, state, self.epoch))
                self.pending.add(task)
                task.add_done_callback(self.pending.discard)
                task.add_done_callback(lambda _: self.rounds.pop(response_id, None))
            else:
                self.rounds.pop(response_id, None)

    async def finish(self, response, state: ToolRound, epoch: int) -> None:
        results = []
        for call in state.calls:
            if state.abandoned:
                return
            results.append(await self.execute(call))
            await self.session.submit_tool_result(call.call, results[-1])
        names = {call.name for call in state.calls}
        voicemail = any(c.name == "record_outcome" and c.arguments.get("status") == "voicemail" for c in state.calls)
        if "end_call" in names or voicemail or not (names & agent.CONTINUE_AFTER or agent.ASK_IF_THERE in results):
            return
        if epoch == self.epoch and not state.abandoned:
            try:
                state.continuation = asyncio.create_task(self.session.continue_reply(response))
                await state.continuation
            except asyncio.CancelledError:
                if not state.abandoned or asyncio.current_task().cancelling():
                    raise
            except RimeInputError:
                if epoch == self.epoch and not state.abandoned:
                    raise
            except r.RimeRealtimeError as error:
                if error.fault.scope != "event" or error.fault.code != "tool_continuation_unavailable":
                    raise


class Call:
    def __init__(self, scenario: Scenario, rime, voices: audio.Voices, claude, options: Options):
        self.rime = rime
        self.s = scenario
        self.o = options
        self.t0 = asyncio.get_running_loop().time()
        self.voices = voices
        self.actor = Actor(claude, scenario) if scenario.staff else None
        self.line = Line(self, options.phone)
        self.speaker = Speaker(self)
        self.music = audio.Loop(audio.hold_music(self.line.rate, options.phone))
        self.timeline: list[dict] = []
        self.modes: list[tuple[int, str]] = []
        self.mode = ""
        self.texts: dict[r.OutputRef, str] = {}
        self.deltas: dict[r.OutputRef, str] = {}
        self.stt: dict[r.OutputRef, str] = {}  # Prism lines with no text event, transcribed
        self.played: list[Played] = []
        self.heard_upto = 0
        self.transcript: list[tuple[str, str]] = []  # what the staff heard and said
        self.active: set[str] = set()
        self.last_event_ms = 0.0
        self.tools: Tools | None = None
        self.speech = asyncio.Lock()  # one hotel action at a time
        self.end_requested = False
        self.outcome_at = 0.0
        self.outcomes = agent.Outcomes(scenario.bookings)
        self.session = None
        self.check_ins = 0
        self.asked_check_in = 0.0  # when Prism was last told to check in
        self.last_hotel_end = 0.0
        self.hangup_after_line = False
        self.hangup = ""
        self.hotel_cancelled = False
        self.actor_busy = False
        self.pause_until = 0.0
        self.last_staff_end = 0.0
        self.silences = 0
        self.menu_end = 0.0
        self.menu_plays = 0
        self.hold_until = 0.0
        self.next_hold_message = 0.0
        self.beep_end = 0.0

    def now_ms(self) -> float:
        return (asyncio.get_running_loop().time() - self.t0) * 1000

    def log(self, who: str, text: str, **extra) -> dict:
        entry = {"t_ms": round(self.now_ms()), "who": who, "text": text, **extra}
        self.timeline.append(entry)
        return entry

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.modes.append((round(self.now_ms()), mode))

    # Prism's side

    def on_event(self, event) -> None:
        p = event.payload
        self.last_event_ms = self.now_ms()
        if isinstance(p, r.TranscriptFinal):
            self.log("heard", p.text)
        elif isinstance(p, r.SpeechStopped):
            # What ended the hotel's turn, as Prism reports it (rimelabs-sdk 0.1.0a20).
            self.log("turn_end", str(getattr(p, "end_reason", None)), audio_end_ms=p.audio_end_ms)
        elif isinstance(p, r.TextDelta):
            self.deltas[p.output] = self.deltas.get(p.output, "") + p.delta
        elif isinstance(p, r.TextDone):
            self.texts[p.output] = p.text
            if (keys := agent.spoken_press(p.text)) and self.mode == "ivr" and self.tools:
                self.log("event", f"Prism said a key press aloud; pressed {keys} anyway")
                self.tools.tasks.create_task(self.press(keys))
        elif isinstance(p, r.ResponseStarted):
            self.active.add(p.response.response_id)
        elif isinstance(p, (r.ResponseEnded, r.ResponseAbandoned)):
            self.active.discard(p.response.response_id)
            if isinstance(p, r.ResponseEnded) and p.status != "completed":
                self.log("event", f"Prism response {p.status}" + (f": {p.reason}" if p.reason else ""))
        elif isinstance(p, r.ToolCall):
            self.log("tool", p.name, args=p.arguments)
        elif isinstance(p, r.FaultEvent):
            self.log("fault", f"{p.error.code}: {p.error.message}", scope=p.error.scope)
        if self.tools:
            self.tools.on_event(event)

    async def execute(self, call: r.ToolCall) -> str:
        args = call.arguments
        if call.name == "press_digits":
            digits = str(args.get("digits", ""))
            result = f"Pressed {digits}." if agent.KEYS.fullmatch(digits) else "Only 0 to 9, * and # work."
            if agent.KEYS.fullmatch(digits):
                await self.press(digits)
        elif call.name == "wait":
            result = "Waiting."
            if self.asked_check_in and self.now_ms() - self.asked_check_in < 10000:
                self.asked_check_in = 0.0  # refuse once, so Prism cannot loop on it
                result = agent.ASK_IF_THERE
        elif call.name == "record_outcome":
            result = await agent.record_when_heard(self.outcomes, args, self.said)
            if self.outcomes.complete and not self.outcome_at:
                self.outcome_at = self.now_ms()  # the goodbye that follows ends the call
            if result.startswith(agent.RECORDED) and args.get("status") == "voicemail":
                self.end_requested = True  # hang up now: nothing more to say to a voicemail
        elif call.name == "end_call":
            result = self.outcomes.end_refusal()
            if result is None:
                self.end_requested = True
                result = "The call ends when your goodbye finishes playing."
        else:
            result = f"There is no tool named {call.name}."
        self.log("tool_result", result, name=call.name)
        return result

    def said(self) -> list[tuple[str, str]]:
        """The call so far as Prism knows it: its own lines, and its transcript of the hotel."""
        items = [(e["t_ms"], "hotel", e["text"]) for e in self.timeline if e["who"] == "heard"]
        for ref, played in self.speaker.outputs.items():
            if text := self.texts.get(ref) or self.deltas.get(ref):
                at = played.started_ms if played.started_ms is not None else self.now_ms()
                items.append((at, "caller", text))
        return [(who, text) for _, who, text in sorted(items)]

    def prism_busy(self) -> bool:
        stale = self.now_ms() - self.last_event_ms > 6000
        tools = bool(self.tools and self.tools.pending)
        return self.speaker.busy or tools or (bool(self.active) and not stale)

    def unheard(self) -> tuple[list[Played], int] | None:
        """Prism's finished lines since the staff last listened, and the index to mark them heard.
        None while a finished line's text event may still arrive."""
        lines = []
        for played in self.played[self.heard_upto :]:
            if self.text_of(played) is None and self.now_ms() - (played.finished_ms or 0) < 1500:
                return None
            lines.append(played)
        return lines, len(self.played)

    def text_of(self, played: Played) -> str | None:
        """Prism's words for one line: its text event, else the text so far if it was cut off, else
        the STT transcript. A filler can arrive with no text."""
        text = self.texts.get(played.ref)
        if not (text or "").strip() and played.cut:
            text = self.deltas.get(played.ref) or text
        if not (text or "").strip() and played.ref in self.stt:
            text = self.stt[played.ref]
        return None if text is None else " ".join(text.split())

    def no_text(self, played: Played) -> bool:
        return not (self.texts.get(played.ref) or self.deltas.get(played.ref) or "").strip()

    async def words(self, played: Played) -> str:
        """What the hotel heard of one Prism line. A line with no text event, such as a filler while a
        tool runs, is transcribed from the audio with Rime STT."""
        text = self.text_of(played)
        if not text and played.played_ms > 250 and played.ref not in self.stt:
            pcm = audio.to_pcm(audio.resample(audio.samples(bytes(played.buf[: played.cursor])), played.rate, audio.RATE))
            try:
                result = await self.rime.stt.transcribe(audio.wav(pcm, audio.RATE), language="en")
                self.stt[played.ref] = text = result.text
            except Exception as error:  # noqa: BLE001 - a missed transcript should not end the call
                self.stt[played.ref] = text = ""
                self.log("fault", f"Could not transcribe a Prism line: {error}")
            return text
        if text and played.cut and played.total_ms and played.ref not in self.stt:
            kept = text.split()[: round(len(text.split()) * played.played_ms / played.total_ms)]
            text = " ".join(kept) + " —"
        return text or ""

    async def listen(self, lines: list[Played]) -> list[str]:
        return [text for played in lines if (text := await self.words(played)).strip(" —")]

    # The hotel's side

    async def hotel_says(self, text: str, voice: str, mode: str) -> dict:
        entry = {"who": "hotel", "mode": mode, "text": text, "voice": voice}
        self.timeline.append(entry)
        self.check_ins = 0
        asked = self.now_ms()
        async for chunk in self.voices.stream(text, voice):
            self.line.feed(entry, chunk)
        entry.setdefault("t_ms", round(self.now_ms()))
        entry.setdefault("end_ms", entry["t_ms"])
        entry["tts_ms"] = round(entry["t_ms"] - asked)
        return entry

    def hang_up(self, reason: str) -> None:
        self.log("event", f"Call ended: {reason}")
        self.hangup = reason
        self.set_mode("ended")
        self.line.closed = True

    async def play_menu(self) -> None:
        ivr = self.s.ivr
        text = (f"{ivr.greeting} " if not self.menu_plays else "") + ivr.menu
        entry = await self.hotel_says(text, ivr.voice, "ivr")
        self.menu_plays += 1
        self.menu_end = entry["end_ms"]

    async def press(self, digits: str) -> None:
        async with self.speech:
            if self.mode != "ivr" or not self.s.ivr:
                return
            action = self.s.ivr.keys.get(digits[0], "invalid")
            self.line.cut()
            self.log("event", f"Phone menu got key {digits[0]}: {action}")
            if action == "desk":
                entry = await self.hotel_says("Please hold while we connect your call.", self.s.ivr.voice, "ivr")
                self.hold_until = entry["end_ms"] + self.s.hold_seconds * 1000
                self.next_hold_message = entry["end_ms"] + 20000
                self.line.background = self.music
                self.set_mode("hold")
            elif action.startswith("say:"):
                await self.hotel_says(action[4:], self.s.ivr.voice, "ivr")
                await self.play_menu()
            elif action == "repeat":
                await self.play_menu()
            else:
                await self.hotel_says("Sorry, that is not a valid option.", self.s.ivr.voice, "ivr")
                await self.play_menu()

    async def staff_turn(self, note: str, heard: list[Played]) -> None:
        self.actor_busy = True
        try:
            for text in await self.listen(heard):
                self.transcript.append(("caller", text))
            asked = self.now_ms()
            reply = await self.actor.next(self.transcript, note)
            actor_ms = round(self.now_ms() - asked)
            say = reply["say"].strip()
            if reply["cancelled"] and not self.hotel_cancelled:
                self.hotel_cancelled = True
                self.log("event", "The hotel cancelled the reservation")
            async with self.speech:
                if say:
                    self.transcript.append(("you", say))
                    entry = await self.hotel_says(say, self.s.staff.voice, "staff")
                    entry["actor_ms"] = actor_ms
                    self.last_staff_end = entry["end_ms"]
                else:
                    self.last_staff_end = self.now_ms()
            if reply["then"] == "pause":
                seconds = min(max(reply["pause_seconds"], 2), 60)
                self.pause_until = self.last_staff_end + seconds * 1000
                self.log("event", f"Staff look something up for {seconds} s")
            elif reply["then"] == "hang_up":
                self.hangup_after_line = True
        finally:
            self.actor_busy = False

    async def nudge(self, now: float) -> None:
        """Ask Prism to check in when the line has been quiet, as the phone agent does."""
        if self.line.speaking:
            self.last_hotel_end = now
            return
        quiet = now - max(self.last_hotel_end, self.speaker.last_audio_ms, self.last_event_ms if self.active else 0)
        if quiet < agent.CHECK_IN_AFTER_S * 1000 or quiet < agent.check_in_after(self.said()) * 1000:
            return
        if self.session and self.check_ins < agent.MAX_CHECK_INS and not self.prism_busy():
            self.check_ins += 1
            self.log("event", f"Asked Prism to check in after {quiet / 1000:.0f} s of quiet")
            self.last_hotel_end = now
            self.asked_check_in = now
            await self.session.request_reply(instruction=agent.check_in(quiet / 1000))

    async def staff_tick(self, now: float) -> None:
        if self.actor_busy or self.line.speaking or (self.pause_until and now < self.pause_until):
            return
        got = self.unheard()
        if got is None:
            return
        heard, upto = got
        if self.pause_until:
            self.pause_until = 0
            self.heard_upto = upto
            return await self.staff_turn("You are back from looking it up.", heard)
        if heard:
            self.silences = 0
            if not self.prism_busy() and now - self.speaker.last_audio_ms >= REPLY_GAP_MS:
                self.heard_upto = upto
                await self.staff_turn("", heard)
            return
        self.heard_upto = upto
        quiet_for = now - max(self.last_staff_end, self.speaker.last_audio_ms)
        if not self.prism_busy() and quiet_for >= SILENCE_MS * (self.silences + 1):
            self.silences += 1
            if self.silences > 3:
                await self.hotel_says("I'm going to hang up now. Goodbye.", self.s.staff.voice, "staff")
                self.hangup_after_line = True
                return
            await self.staff_turn(f"The caller has said nothing for {SILENCE_MS * self.silences // 1000} seconds.", [])

    async def hotel(self) -> None:
        s = self.s
        if s.answer == "desk":
            self.set_mode("staff")
            await self.staff_turn("The phone rings and you pick up.", [])
        elif s.answer == "ivr":
            self.set_mode("ivr")
            async with self.speech:
                await self.play_menu()
        elif s.answer == "voicemail":
            self.set_mode("voicemail")
            await self.hotel_says(s.voicemail, s.voicemail_voice, "voicemail")
            beep = {"who": "hotel", "mode": "voicemail", "text": "[beep]"}
            self.timeline.append(beep)
            self.line.feed(beep, audio.tone(1000, 500))
            self.beep_end = beep["end_ms"]
        while True:
            await asyncio.sleep(0.1)
            now = self.now_ms()
            if now > self.o.max_seconds * 1000:
                return self.hang_up("time limit")
            if self.end_requested and not self.speaker.busy and now - self.speaker.last_audio_ms > 300:
                return self.hang_up("Prism ended the call")
            if self.outcome_at and not self.speaker.busy and not self.line.speaking:
                # The phone agent hangs up when the goodbye after record_outcome finishes playing,
                # but not over the hotel: if the hotel answers the goodbye, Prism gets to reply first.
                goodbye = [p.started_ms for p in self.played if p.started_ms and p.started_ms > self.outcome_at]
                answered = [e for e in self.timeline if e.get("who") == "hotel" and goodbye and e.get("t_ms", 0) > goodbye[0]]
                if answered:
                    self.outcome_at = max(e["end_ms"] for e in answered)
                elif goodbye and now - self.speaker.last_audio_ms >= agent.HANG_UP_AFTER_GOODBYE_S * 1000:
                    return self.hang_up("Prism's goodbye finished")
                elif not goodbye and not self.prism_busy() and now - self.outcome_at >= agent.HANG_UP_IF_SILENT_S * 1000:
                    return self.hang_up("Prism said nothing after recording the outcome")
            if self.hangup_after_line and not self.line.speaking:
                return self.hang_up("the hotel hung up")
            if self.mode == "ivr" and not self.speech.locked() and not self.line.speaking:
                if now - self.menu_end > 7000 and not self.prism_busy():
                    async with self.speech:
                        if self.menu_plays >= 3:
                            await self.hotel_says("We did not receive a selection. Goodbye.", s.ivr.voice, "ivr")
                            self.hangup_after_line = True
                        else:
                            await self.play_menu()
            elif self.mode == "hold":
                if now >= self.hold_until:
                    self.line.background = self.line.quiet
                    self.set_mode("staff")
                    got = self.unheard()
                    if got:
                        self.heard_upto = got[1]
                    await self.staff_turn("You pick up a call transferred from the phone menu.", got[0] if got else [])
                elif now >= self.next_hold_message:
                    self.next_hold_message = now + 20000
                    await self.hotel_says("Thank you for holding. Someone will be with you shortly.", s.ivr.voice, "hold")
            elif self.mode == "staff":
                await self.nudge(now)
                await self.staff_tick(now)
            elif self.mode == "voicemail" and now - self.beep_end > 30000:
                return self.hang_up("voicemail recording timed out")

    # Results

    def mode_at(self, t_ms: float) -> str:
        mode = ""
        for at, m in self.modes:
            if at <= t_ms:
                mode = m
        return mode

    def result(self) -> dict:
        prism = [
            {
                "t_ms": round(p.started_ms),
                "end_ms": round(p.finished_ms or p.started_ms),
                "who": "prism",
                "text": self.text_of(p) or "",
                "no_text_event": self.no_text(p),
                "played_ms": round(p.played_ms),
                "total_ms": round(p.total_ms),
                "cut": p.cut,
                "mode": self.mode_at(p.started_ms),
            }
            for p in self.played
        ]
        return {
            "scenario": self.s.id,
            "options": asdict(self.o),
            "duration_ms": round(self.now_ms()),
            "hangup": self.hangup,
            "hotel_cancelled": self.hotel_cancelled,
            "modes": self.modes,
            "timeline": sorted(self.timeline + prism, key=lambda e: (e["t_ms"], e["who"] != "hotel")),
        }


async def run_call(scenario: Scenario, rime, claude, voices: audio.Voices, options: Options, wav=None) -> dict:
    call = Call(scenario, rime, voices, claude, options)
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    session_info = None
    turn = None
    if options.eagerness or options.end_of_turn_ms:
        turn = r.TurnDetection(eagerness=options.eagerness, end_of_turn_timeout_ms=options.end_of_turn_ms)
    error = None
    try:
        async with rime.realtime.connect(
            endpoint=options.endpoint,
            voice=options.voice,
            instructions=scenario.instructions(),
            tools=agent.tools(quiet=options.fillers == "off"),
            turn_detection=turn,
        ) as session:
            call.log("event", f"Prism connected (session {session.info.session_id})")
            # Prism's resolved settings, such as the turn detection it applied.
            session_info = asdict(session.info)
            call.session = session
            async with asyncio.TaskGroup() as tasks:
                call.tools = Tools(session, tasks, call.execute)
                speaker = tasks.create_task(call.speaker.run())
                hotel = tasks.create_task(call.hotel())
                await r.run_conversation(session, source=call.line, playback=call.speaker, on_event=call.on_event)
                if not call.hangup:
                    call.hang_up("Prism's session ended")
                for task in (speaker, hotel, *call.tools.pending):
                    task.cancel()
    except Exception as exc:  # noqa: BLE001 - one failed call should not stop the run
        errors = exc.exceptions if isinstance(exc, ExceptionGroup) else [exc]
        closed_after_hangup = call.hangup and all("session closed" in str(e) for e in errors)
        if not closed_after_hangup:  # Prism closing the session after the call ended is a normal end
            error = "; ".join(f"{type(e).__name__}: {e}" for e in errors)
            call.log("fault", f"The simulator stopped: {error}")
    for played in call.played:
        if not call.text_of(played):
            await call.words(played)
    result = call.result()
    result["started_at"] = started_at
    result["session"] = session_info
    result["error"] = error
    if wav:
        audio.write_call(wav, bytes(call.line.recording), call.line.rate, call.line.started_ms or 0, call.speaker.segments)
    return result
