# MyWhisper

A local, private alternative to WhisperFlow / Monogram for macOS.

- **Dictation** — press a hotkey, speak, and the transcribed text is pasted into
  whatever app you're using.
- **Meetings** — press a hotkey to record a meeting (Teams, Zoom, etc.) by
  capturing system audio. It transcribes, separates speakers, and writes
  structured notes.

Everything runs on your machine except the optional LLM step, which you control
(local Ollama, OpenRouter, or the Claude API).

## How it works

1. Audio is captured locally — your mic via PortAudio, system audio via Apple's
   ScreenCaptureKit (no virtual audio device, and nothing joins the meeting).
2. Speech-to-text runs locally with MLX Whisper (Apple Silicon).
3. Speakers are separated locally with pyannote.
4. The transcript is summarized by the LLM backend you choose.
5. Notes are saved to `~/MyWhisper/`. The raw audio is deleted.

## Requirements

- Apple Silicon Mac, macOS 13 or later
- Python 3.10+
- Xcode Command Line Tools (`xcode-select --install`) — to compile the audio helper

## Install

```
./build_app.sh
```

This creates a Python virtual environment, installs dependencies, compiles the
system-audio helper, and builds `MyWhisper.app`.

Then store your secrets (kept in the macOS Keychain, never in a file):

```
./run.sh setup
```

## Permissions

macOS prompts the first time each is needed. You can also grant them in
**System Settings -> Privacy & Security**:

- **Microphone** — required for both modes
- **Screen Recording** — required for meeting mode (this is how system audio is captured)
- **Accessibility** — required for global hotkeys and for pasting dictated text

If a permission is granted but still not working, remove and re-add the app in
that permission's list, then restart MyWhisper.

## Speaker separation (Hugging Face token)

Speaker diarization uses the free but gated `pyannote/speaker-diarization-3.1`
model:

1. Create a free account at huggingface.co
2. Accept the conditions on the model pages for
   `pyannote/speaker-diarization-3.1` and `pyannote/segmentation-3.0`
3. Create an access token (Settings -> Access Tokens)
4. Run `./run.sh setup` and paste the token

To skip speaker separation, set `enabled = false` under `[diarization]` in
`~/MyWhisper/config.toml`.

## Usage

Launch `MyWhisper.app` (or `./run.sh` for development). A microphone icon
appears in the menu bar.

- **Option-D** — start / stop dictation
- **Option-M** — start / stop meeting recording

The icon shows a red dot while recording and an hourglass while processing.
Meeting notes land in `~/MyWhisper/`.

## Choosing the LLM backend

Edit `~/MyWhisper/config.toml`, the `[llm]` section:

- `provider = "ollama"` — your Ollama server (default `http://llm.local:11434`)
- `provider = "openrouter"` — set `openrouter_model`, store the key via `./run.sh setup`
- `provider = "anthropic"` — set `anthropic_model`, store the key via `./run.sh setup`

## Notes & limitations

- The `MyWhisper.app` bundle points at the virtual environment in this folder —
  don't move the folder after building (re-run `build_app.sh` if you do).
- Speaker labels are generic ("Speaker 1", "Speaker 2") — rename them in the
  saved notes.
- Diarization runs on CPU and can take a few minutes for a long meeting.
- Recording other people has legal/consent implications in some places — tell
  participants you're taking AI notes.
