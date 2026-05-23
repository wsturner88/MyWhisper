# MyWhisper

A local, private alternative to WhisperFlow / Monogram for macOS. Everything
runs on your Mac except the optional LLM step (you choose the provider and
your API key stays in the macOS Keychain).

## Two modes

### Dictation — push-to-talk, paste into anything

Hold **Right Option**, speak, release — the transcript is typed into whatever
app your cursor is in. Fully on-device (MLX Whisper), no network calls.

### Meeting recording — capture, transcribe, summarize

Click the menu bar icon → **Start Meeting ▸** → pick a meeting type. MyWhisper
records your mic and the other side's audio (via ScreenCaptureKit), transcribes
with Whisper, separates speakers with pyannote, then sends the transcript to
your chosen LLM to produce structured notes (Summary, Key Decisions, Action
Items, Open Questions).

A floating indicator with a pulsing red dot, live timer, and Stop button stays
on top of every window while recording.

## What's in the app

- **Menu bar icon** — Start Dictation, Start Meeting ▸ (submenu of presets),
  Stop Meeting, Dashboard, Open Notes Folder, Quit
- **Dashboard** — a floating panel (top-right of the screen) with tabs for
  Meetings (browse past summaries), Dictation (last 20 captures with copy
  buttons), and Settings
- **Floating recording indicator** — appears while a meeting is recording,
  click anywhere on it to stop
- **Waveform / VU meter** — live mic-level indicator shown during dictation

## Meeting Type Presets

The prompt sent to the LLM is shaped by which preset you pick when starting
the meeting. Built-in presets:

- **General Meeting** — balanced summary, decisions, action items
- **Sales Call** — customer needs, objections, pricing, next steps
- **Internal Standup** — done / next / blockers per person

You can add as many **Custom Presets** as you want (each with a name and a
plain-English description of what the AI should focus on), and there are
one-click starters for Board Meeting, Personal/Family, and Doctor/Medical.

## Requirements

- **Apple Silicon Mac** (M1 or later) — MLX Whisper is Apple Silicon only
- **macOS 13 or later**
- **Python 3.10+**
- **Xcode Command Line Tools** (`xcode-select --install`) — to compile the
  audio capture helper

## Install

Clone the repo and run the build script:

```
git clone https://github.com/wsturner88/MyWhisper.git
cd MyWhisper
./build_app.sh
```

This creates a Python virtual environment, installs all dependencies, and
compiles the system-audio helper.

Then start MyWhisper as a background service so it always runs at login and
the menu bar icon shows up correctly:

```
launchctl load ~/Library/LaunchAgents/local.mywhisper.plist
```

(If you don't have the LaunchAgent yet, run `./run.sh` once and toggle
**Start at Login** in the Dashboard.)

## First-time setup

1. Launch MyWhisper — a 🎙️ icon appears in the menu bar
2. Click it → **Dashboard…**
3. In **Settings**, set up your LLM:
   - **Provider**: OpenRouter or Anthropic (Claude)
   - **API Key**: paste your key (stored in macOS Keychain, never on disk)
   - **Model**: pick from the dropdown — it's fetched live from the provider
   - Click **Test Connection** to verify
4. Grant macOS permissions when prompted (see below)

## Permissions

macOS prompts the first time each is needed. To grant them manually, go to
**System Settings → Privacy & Security**:

- **Microphone** — required for both modes
- **Screen Recording** — required for meeting mode (this is how the other
  side's audio is captured from Zoom/Teams)
- **Accessibility** — required for global hotkeys and for pasting dictated text

> The app's permission entries may be listed under **python3.12** rather than
> "MyWhisper" — that's expected. If a permission seems granted but isn't
> working, remove and re-add it, then restart MyWhisper.

## Speaker separation (Hugging Face token)

Speaker diarization uses the free but gated `pyannote/speaker-diarization-3.1`
model. To enable it:

1. Create a free account at huggingface.co
2. Accept the conditions on the model pages for
   `pyannote/speaker-diarization-3.1` and `pyannote/segmentation-3.0`
3. Create an access token (Settings → Access Tokens)
4. Save it: `python -m mywhisper.setup_hf_token <your_token>`

To skip speaker separation, set `enabled = false` under `[diarization]` in
`config.toml` (inside your data folder).

## Hotkeys

- **Hold Right Option** — push-to-talk dictation (the only hotkey)

The hotkey is configurable in `config.toml`:

```toml
[hotkeys]
push_to_talk = "right_option"   # or right_command, right_shift, left_option, etc.
```

Meeting recording does **not** have a hotkey — start it from the menu bar
submenu, stop it from the floating indicator or menu bar.

## Where your files live

By default: `~/Documents/MyWhisper/` — meetings, dictation history, config,
vocabulary, and logs all live here. You can change the location in
**Dashboard → Settings → Data Folder** (your existing files copy over
automatically).

A small pointer file at `~/Library/Application Support/MyWhisper/` remembers
where you put it.

## Choosing the LLM backend

Two providers are supported. Both configured via the Dashboard:

- **OpenRouter** — single API key gives you access to Claude, GPT, Gemini,
  Llama, and ~350 other models. Pay-as-you-go.
- **Anthropic** — direct API access to Claude models.

The model dropdown fetches the provider's live catalog so you don't have to
remember model names.

## Notes & limitations

- Speaker labels are generic ("Speaker 1", "Speaker 2") — rename them in the
  saved notes.
- Diarization runs on CPU and can take a couple of minutes on a long meeting.
- Recording other people has legal and consent implications in many places —
  always tell participants you're taking AI-assisted notes.
- Apple Silicon only (no Intel build path — MLX is the blocker).

## Built with

- [MLX](https://github.com/ml-explore/mlx) (Apple Silicon ML framework)
- [mlx-whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper)
- [pyannote.audio](https://github.com/pyannote/pyannote-audio) — speaker
  diarization
- [rumps](https://github.com/jaredks/rumps) — Python macOS menu bar apps
- [pynput](https://github.com/moses-palmer/pynput) — global hotkeys
- PyObjC + WebKit — the native dashboard panel
- Swift + ScreenCaptureKit — the system-audio helper
