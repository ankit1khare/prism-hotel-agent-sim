# Prism hotel agent simulator

A voice agent built on [Prism](https://docs.rime.ai/docs/prism/overview), Rime's speech-to-speech model, that phones a hotel to cancel a guest's reservations. It comes with a simulator that tests the agent against made-up hotels before it places a real call.

In the simulator, Claude plays the hotel staff and Rime TTS gives them voices. Prism hears that audio and answers in real time, the way it would on a phone line. Each call is then graded by code checks and a Claude judge. No phone calls are made, and every hotel, guest and number in this repository is made up.

| Part | What it is |
|---|---|
| `hotel_agent.py` | The agent: Prism's instructions, its tools (`wait`, `press_digits`, `record_outcome`, `end_call`), and the rules the caller code enforces. |
| `sim/` | The simulator: scenarios, the simulated phone line, the Claude-played staff, grading, and an HTML report. |

## The agent

Prism gets the guest's booking (name, confirmation number, dates) and a few rules. It is the caller. It never asks the hotel for details it already holds, and it never agrees to a fee. It also records one outcome before it hangs up.

Instructions alone do not make a phone agent safe, so the caller code also enforces these rules:

| Rule | Where it is enforced |
|---|---|
| Record a cancellation number only after the hotel said it and gave a clear yes to the read-back. A sneeze is not a yes. | `record_outcome` refuses and tells Prism to ask again (`unconfirmed`). |
| Hang up only after recording an outcome. | `end_call` refuses until then (`NOT_ENDED`). |
| Hang up when the goodbye finishes, not while the hotel is talking. | The caller watches playback (`HANG_UP_AFTER_GOODBYE_S`). |
| Check in when the line goes quiet. | The caller tells Prism to ask whether the hotel is still there after 20 seconds of quiet. If the hotel asked Prism to wait, or Prism just pressed a key, it waits 60 seconds (`check_in_after`). |
| Stay quiet on hold, during recordings, and while the hotel looks something up. | Prism calls the `wait` tool instead of speaking. |
| Say letters clearly. | The confirmation number is spelled for Prism in code, as in "L as in Lima" (`spoken`). |
| Say nothing while a tool runs. | Every tool turns Prism's filler off (`QUIET`), so no filler lands on a phone menu or a voicemail. |

## Run the simulator

You need [uv](https://docs.astral.sh/uv/), a [Rime API key](https://app.rime.ai/), and an [Anthropic API key](https://console.anthropic.com/). Put both keys in a `.env` file in the repository root:

```dotenv
RIME_API_KEY=your_rime_key
ANTHROPIC_API_KEY=your_anthropic_key
```

Then run every scenario once:

```bash
uv run python -m sim.run
```

Each run writes a folder in `runs/` with an HTML report and the grades. It also saves a JSON file and a stereo WAV for each call, with the hotel on the left and Prism on the right.

```
 hotel_agent.py (Prism's instructions + tools)
        │
        ▼
 Prism realtime session ◄──── Line: 20 ms frames, in real time ◄──── hotel speech (Rime TTS),
  run_conversation()                                                   phone menu, hold music, voicemail
        │
        └────► Speaker: Prism's replies play in real time ────► the staff hear them (Claude decides what to say)
                                                                 tool calls: press_digits drives the menu,
                                                                 record_outcome and end_call end the call
 after the call: code checks + a Claude judge ──► runs/<time>/report.html, a JSON file and a stereo WAV per call
```

| Option | What it does |
|---|---|
| `-s ivr_then_desk` | Run one scenario. Repeat the flag for more. |
| `-n 3` | Call each scenario 3 times. One call is an example, not evidence. |
| `-j 4` | Run 4 calls at the same time (default 2). A busy computer can delay the audio, which shows up as false cut-ins. |
| `--phone` | Send Prism 8 kHz mu-law audio, as a phone bridge does, instead of 16 kHz. |
| `--voice aurora` | Prism's voice (default `astra`). |
| `--eagerness low`, `--end-of-turn-ms 1500` | Prism's turn detection settings. |
| `--fillers default` | Let Prism speak fillers while tools run. They are off by default. |
| `--label after` | Add a name to the run's folder, such as `before` and `after` a change. |
| `--no-judge` | Code checks only, with no Claude grading. |

To grade a run again: `uv run python -m sim.grade runs/<folder>`. To compare two runs side by side, for example before and after a change to the instructions: `uv run python -m sim.compare runs/<a> runs/<b>`.

### Cost

Every hotel turn, rubric and grade is a Claude request, so a large run costs money. The simulator caps Claude spend at $20 per API key. It records each request's cost in `cache/claude-spend.json` and prints what each run spent. Once the key is within 50 cents of the cap, it starts no new request. Set `CLAUDE_SPEND_CAP` to change it.

## Scenarios

| Id | What happens | Right outcome |
|---|---|---|
| `desk_free_cancel` | The front desk answers and cancels for free. | cancelled, `CX48192` |
| `ivr_then_desk` | A phone menu, 15 seconds of hold music, then an agent who looks the booking up. | press 1 or 2, then cancelled, `R77310` |
| `fee_nonrefundable` | A 214 dollar fee that cannot be waived. | fee_quoted, 214 dollars, not cancelled |
| `third_party_refused` | The hotel asks for card digits, then refuses to cancel for a third party. | refused_third_party |
| `voicemail` | A voicemail greeting and a beep. | voicemail, no message left |
| `asks_card_then_cancels` | The hotel says the number is not found and asks for card digits and a zip code. | cancelled, `45K2Z9` |
| `unsure_clerk_wrong_number` | An unsure clerk offers a wrong number, goes quiet for 35 seconds, and answers the read-back with "Hm." | cancelled, `T6284` |
| `quiet_after_hello` | The clerk says only "Hello?", then goes quiet for 50 seconds without asking Prism to wait. | Prism asks if they are still there, then cancelled, `41906` |
| `two_bookings_same_hotel` | Two reservations at one hotel. The clerk checks the card's last four digits and the email first. | both cancelled, `MG4821` and `MG4822` |
| `redirect_to_brand` | The front desk cannot cancel a booking made on the brand's website and gives a central reservations number. | redirected, with the number in notes |

To add a scenario, add a `Scenario` to `sim/scenarios.py`. It needs the booking, who answers, the staff persona and policy, and the right outcome.

## Grading

Each failure is an **ear** error (Prism misheard or mistimed) or a **brain** error (Prism heard right but chose wrong), with a subcategory:

| Category | Subcategories |
|---|---|
| ear | transcription, turn_taking, noise |
| brain | behavior, policy, tool_call_choice, tool_call_arguments, speech |

Code checks the facts:

- the recorded status, cancellation number and fee
- the menu key
- silence during recordings
- cutting in while the hotel is mid-sentence
- hanging up
- the transcript's word error rate
- how often a guard had to step in

A wrong cancellation number is an ear error when Prism misheard it, and a brain error when the hotel never said it.

Claude writes a rubric for each scenario once and saves it in `rubrics/<id>.json`, which you can edit. Claude then grades each call against the rubric and lists any errors that the rubric missed. If you change the agent's instructions or a scenario, delete that scenario's rubric so it is written again.

Every run also writes `run.json`, with the command, the git commit, the `rimelabs-sdk` version, the Claude model, and the start and finish times. Each call's JSON keeps the settings that Prism applied, so you can repeat a run and compare it.

## Limits

- The simulated staff read Prism's text, not its audio, except for lines with no text, which are transcribed with Rime STT. They take 2 to 3 seconds to answer, because Claude decides each line.
- Claude plays each hotel a little differently on every run. Use `-n` before you trust a number.
- The simulator does not place phone calls. To call a real phone, connect the same instructions, tools and guards to a telephony bridge, such as a SIP trunk through LiveKit.

## License

[MIT](LICENSE)
