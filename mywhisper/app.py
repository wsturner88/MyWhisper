import logging
import os
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import rumps

from . import (audio, autostart, config, dashboard, diarize, dictation_log,
               hotkeys, output, paste, recorder, sounds, summarize,
               transcribe, vocab, waveform)

HELPER_NAME = "mywhisper-sysaudio"

# Menu bar label. Image icons do not render reliably on recent macOS, so
# the indicator is plain text — this is what shows in the menu bar.
_TITLES = {
    "idle": "🎙️ MyWhisper",
    "dictation": "🔴 Recording",
    "meeting": "🔴 Recording",
    "processing": "⏳ Working",
}

_LOG_PATH = Path.home() / "MyWhisper" / "mywhisper.log"
logging.basicConfig(
    filename=str(_LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
)
log = logging.getLogger("mywhisper")

# 0.3s at 16kHz — anything shorter is treated as "no audio captured".
_MIN_SAMPLES = 4800

# Push-to-talk must be held at least this long, or it's treated as an
# accidental tap and discarded without transcribing.
_MIN_HOLD = 1.0


def _helper_path():
    env = os.environ.get("MYWHISPER_HELPER")
    if env and os.path.exists(env):
        return env
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for candidate in (os.path.join(root, "helper", HELPER_NAME),
                      os.path.join(root, HELPER_NAME)):
        if os.path.exists(candidate):
            return candidate
    return ""


def _tmp_wav():
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="mywhisper_")
    os.close(fd)
    return path


def _cleanup(*paths):
    for path in paths:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


class MyWhisperApp(rumps.App):
    def __init__(self):
        super().__init__("MyWhisper", title=_TITLES["idle"], quit_button=None)
        log.info("=== MyWhisper starting ===")
        self.cfg = config.load()
        self.out_dir = config.output_dir(self.cfg)
        self.state = "idle"
        self.mic = recorder.MicRecorder()
        self.sysaudio = recorder.SystemAudioRecorder(_helper_path())
        self.waveform = waveform.Indicator(kind=config.get_visualization())
        self._events = queue.Queue()
        self._mic_wav = None
        self._dictation_started = 0.0

        # Restore the saved microphone, if that device is still connected.
        saved = config.get_selected_mic()
        if saved and saved in [name for _, name in recorder.input_devices()]:
            self.mic.device = saved
            log.info("restored microphone: %s", saved)

        self.mi_dict = rumps.MenuItem("Start Dictation", callback=self._click_dictation)
        self.mi_meet = rumps.MenuItem("Start Meeting", callback=self._click_meeting)
        self.mi_status = rumps.MenuItem("Idle", callback=None)

        self.mic_menu = rumps.MenuItem("Microphone")
        self._mic_items = {}
        self._build_mic_menu()

        self.llm_menu = rumps.MenuItem("LLM (Summaries)")
        self._llm_provider_items = {}
        self._build_llm_menu()

        self.viz_menu = rumps.MenuItem("Visualization")
        self._viz_items = {}
        for label, kind in (("Waveform", "waveform"),
                            ("VU Meter (Retro)", "vu_meter")):
            item = rumps.MenuItem(label, callback=self._pick_visualization)
            self._viz_items[label] = (item, kind)
            self.viz_menu.add(item)
        self._update_viz_checks()

        self.mi_autostart = rumps.MenuItem("Start at Login",
                                           callback=self._toggle_autostart)
        self.mi_autostart.state = 1 if autostart.is_enabled() else 0

        settings_menu = rumps.MenuItem("Settings")
        settings_menu.add(self.mic_menu)
        settings_menu.add(self.viz_menu)
        settings_menu.add(self.llm_menu)
        settings_menu.add(rumps.separator)
        settings_menu.add(rumps.MenuItem("Edit Vocabulary…",
                                         callback=self._edit_vocab))
        settings_menu.add(rumps.separator)
        settings_menu.add(self.mi_autostart)

        self.menu = [
            self.mi_dict,
            self.mi_meet,
            None,
            self.mi_status,
            None,
            settings_menu,
            rumps.MenuItem("Dashboard…", callback=self._open_dashboard),
            rumps.MenuItem("Open Notes Folder", callback=self._open_folder),
            None,
            rumps.MenuItem("Quit", callback=rumps.quit_application),
        ]

        try:
            ptt_key = hotkeys.resolve(
                self.cfg["hotkeys"].get("push_to_talk", "right_option"))
            self.hotkeys = hotkeys.PushToTalk(
                ptt_key,
                on_down=lambda: self._events.put(("toggle", "dictation")),
                on_up=lambda: self._events.put(("toggle", "dictation")))
            self.hotkeys.start()
            log.info("push-to-talk listener started")
        except Exception:
            log.exception("hotkeys unavailable")

        rumps.Timer(self._poll, 0.2).start()
        rumps.Timer(self._tick_waveform, 0.04).start()
        threading.Thread(target=self._prewarm, daemon=True).start()
        log.info("app ready (provider=%s, model=%s)",
                 self.cfg["llm"]["provider"], self.cfg["whisper"]["model"])

    def _prewarm(self):
        try:
            log.info("prewarm: loading speech model")
            transcribe.prewarm(self.cfg["whisper"]["model"])
            log.info("prewarm: speech model ready")
        except Exception:
            log.exception("prewarm failed")

    # -- microphone menu ----------------------------------------------
    def _build_mic_menu(self):
        default_item = rumps.MenuItem("System Default", callback=self._on_pick_mic)
        self._mic_items = {None: default_item}
        self.mic_menu.add(default_item)
        for _, name in recorder.input_devices():
            if name in self._mic_items:
                continue
            item = rumps.MenuItem(name, callback=self._on_pick_mic)
            self._mic_items[name] = item
            self.mic_menu.add(item)
        self._update_mic_checks()

    def _update_mic_checks(self):
        for key, item in self._mic_items.items():
            item.state = 1 if key == self.mic.device else 0

    def _on_pick_mic(self, sender):
        name = None if sender.title == "System Default" else sender.title
        self.mic.device = name
        config.set_selected_mic(name or "")
        self._update_mic_checks()
        log.info("microphone set to: %s", name or "system default")

    def _update_viz_checks(self):
        for _, (item, kind) in self._viz_items.items():
            item.state = 1 if kind == self.waveform.kind else 0

    def _pick_visualization(self, sender):
        _, kind = self._viz_items[sender.title]
        self.waveform.set_kind(kind)
        config.set_visualization(kind)
        self._update_viz_checks()
        log.info("visualization set to: %s", kind)

    def _toggle_autostart(self, sender):
        try:
            if autostart.is_enabled():
                autostart.disable()
                sender.state = 0
                log.info("autostart disabled")
            else:
                autostart.enable()
                sender.state = 1
                log.info("autostart enabled")
        except Exception:
            log.exception("autostart toggle failed")
            self._notify("Start at Login", "Could not change this setting.")

    def _edit_vocab(self, _):
        subprocess.run(["open", str(vocab.ensure_file())], check=False)

    # -- LLM settings menu ---------------------------------------------
    def _build_llm_menu(self):
        current = config.get_llm_provider()
        for pid, info in config.LLM_PROVIDERS.items():
            item = rumps.MenuItem(info["label"], callback=self._pick_llm_provider)
            item._provider_id = pid
            item.state = 1 if pid == current else 0
            self._llm_provider_items[pid] = item
            self.llm_menu.add(item)
        self.llm_menu.add(rumps.separator)
        self.llm_menu.add(rumps.MenuItem("Set API Key…",
                                         callback=self._set_api_key))
        self.llm_menu.add(rumps.MenuItem("Set Model…",
                                         callback=self._set_llm_model))
        self.llm_menu.add(rumps.separator)
        self.llm_menu.add(rumps.MenuItem("Test Connection…",
                                         callback=self._test_llm))

    def _pick_llm_provider(self, sender):
        pid = sender._provider_id
        config.set_llm_provider(pid)
        for k, item in self._llm_provider_items.items():
            item.state = 1 if k == pid else 0
        log.info("LLM provider set to: %s", pid)

    def _set_api_key(self, _):
        provider = config.get_llm_provider()
        info = config.LLM_PROVIDERS[provider]
        existing = config.get_secret(info["key_name"]) or ""
        masked = ""
        if existing:
            masked = existing[:4] + "•" * (len(existing) - 8) + existing[-4:]

        win = rumps.Window(
            message=f"Enter your {info['label']} API key:",
            title="MyWhisper — API Key",
            default_text=masked,
            ok="Save",
            cancel="Cancel",
            dimensions=(420, 24),
        )
        resp = win.run()
        if resp.clicked and resp.text.strip() and "•" not in resp.text:
            config.set_secret(info["key_name"], resp.text.strip())
            log.info("API key saved for %s", provider)
            self._notify("API Key Saved", info["label"])

    def _set_llm_model(self, _):
        provider = config.get_llm_provider()
        info = config.LLM_PROVIDERS[provider]
        current_model = config.get_llm_model(provider)
        win = rumps.Window(
            message=(
                f"Model name for {info['label']}:\n\n"
                f"OpenRouter examples: anthropic/claude-sonnet-4-6, "
                f"google/gemini-2.5-flash\n"
                f"Anthropic examples: claude-sonnet-4-6, claude-haiku-4-5-20251001"
            ),
            title="MyWhisper — LLM Model",
            default_text=current_model,
            ok="Save",
            cancel="Cancel",
            dimensions=(420, 24),
        )
        resp = win.run()
        if resp.clicked and resp.text.strip():
            config.set_llm_model(provider, resp.text.strip())
            log.info("LLM model set to: %s (provider=%s)", resp.text.strip(), provider)
            self._notify("Model Updated", resp.text.strip())

    def _test_llm(self, _):
        self._notify("Testing Connection…", "Sending a test message…")
        def _run():
            from . import llm
            ok, msg = llm.test_connection()
            if ok:
                self._events.put(("notify", "Connection OK ✓",
                                  f"{msg} is responding."))
            else:
                self._events.put(("notify", "Connection Failed ✗", msg))
        threading.Thread(target=_run, daemon=True).start()

    def _tick_waveform(self, _):
        if self.state == "dictation":
            self.waveform.update(self.mic.level)

    # -- UI helpers ---------------------------------------------------
    def _notify(self, title, message):
        try:
            rumps.notification("MyWhisper", title, message)
        except Exception:
            pass

    def _sync(self):
        labels = {
            "idle": ("Start Dictation", "Start Meeting", "Idle"),
            "dictation": ("Stop Dictation", "Start Meeting", "Recording dictation..."),
            "meeting": ("Start Dictation", "Stop Meeting", "Recording meeting..."),
            "processing": ("Working...", "Working...", "Working..."),
        }
        self.mi_dict.title, self.mi_meet.title, self.mi_status.title = labels[self.state]
        self.title = _TITLES[self.state]

    # -- event loop (runs on the main thread) -------------------------
    def _poll(self, _):
        self._check_triggers()
        try:
            while True:
                event = self._events.get_nowait()
                kind = event[0]
                if kind == "toggle":
                    if event[1] == "dictation":
                        self._click_dictation(None)
                    else:
                        self._click_meeting(None)
                elif kind == "notify":
                    self._notify(event[1], event[2])
                elif kind == "paste":
                    try:
                        paste.paste_text(event[1])
                        log.info("dictation: pasted %d characters", len(event[1]))
                    except Exception:
                        log.exception("paste failed")
                elif kind == "done":
                    self.state = "idle"
                    self._sync()
        except queue.Empty:
            pass

    def _check_triggers(self):
        # A control channel that does not depend on the menu bar or hotkeys:
        # creating one of these files toggles the corresponding mode.
        for filename, kind in (("dictation_trigger", "dictation"),
                               ("meeting_trigger", "meeting")):
            path = config.APP_DIR / filename
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
                log.info("trigger file: %s", kind)
                if kind == "dictation":
                    self._click_dictation(None)
                else:
                    self._click_meeting(None)

    # -- menu callbacks ----------------------------------------------
    def _open_dashboard(self, _):
        dashboard.open_dashboard()

    def _open_folder(self, _):
        subprocess.run(["open", str(self.out_dir)], check=False)

    def _click_dictation(self, _):
        if self.state == "idle":
            log.info("dictation: start requested")
            self._mic_wav = _tmp_wav()
            try:
                self.mic.start(self._mic_wav)
            except Exception:
                log.exception("dictation: could not start microphone")
                self._notify("Microphone error", "Could not start recording.")
                return
            self._dictation_started = time.monotonic()
            self.state = "dictation"
            self._sync()
            self.waveform.show()
            sounds.play_start(self.cfg)
        elif self.state == "dictation":
            held = time.monotonic() - self._dictation_started
            self.waveform.hide()
            if held < _MIN_HOLD:
                # Too quick to be intentional — discard without transcribing.
                log.info("dictation: cancelled (held %.2fs, under %.1fs)",
                         held, _MIN_HOLD)
                self.mic.stop()
                _cleanup(self._mic_wav)
                self.state = "idle"
                self._sync()
                return
            log.info("dictation: stop requested (held %.2fs)", held)
            sounds.play_stop(self.cfg)
            self.state = "processing"
            self._sync()
            threading.Thread(target=self._finish_dictation, daemon=True).start()

    def _click_meeting(self, _):
        if self.state == "idle":
            log.info("meeting: start requested")
            self._mic_wav = _tmp_wav()
            try:
                self.mic.start(self._mic_wav)
            except Exception:
                log.exception("meeting: could not start microphone")
                self._notify("Microphone error", "Could not start recording.")
                return
            self.state = "meeting"
            self._sync()
            if not self.sysaudio.start(_tmp_wav()):
                log.warning("meeting: system audio unavailable: %s", self.sysaudio.error)
                self._notify("System audio unavailable",
                             f"{self.sysaudio.error or ''} Recording microphone only.")
        elif self.state == "meeting":
            log.info("meeting: stop requested")
            self.state = "processing"
            self._sync()
            threading.Thread(target=self._finish_meeting, daemon=True).start()

    # -- background workers -------------------------------------------
    def _finish_dictation(self):
        mic_wav = self._mic_wav
        try:
            log.info("dictation: stopping microphone")
            self.mic.stop()
            size = os.path.getsize(mic_wav) if mic_wav and os.path.exists(mic_wav) else 0
            log.info("dictation: recorded file is %d bytes", size)
            data = audio.load_mono_16k(mic_wav)
            log.info("dictation: %.2fs of audio", len(data) / 16000.0)
            if len(data) < _MIN_SAMPLES:
                log.warning("dictation: audio too short, skipping transcription")
                self._events.put(("notify", "Dictation",
                                   "No audio captured - check microphone access."))
                return
            log.info("dictation: transcribing")
            _, text = transcribe.transcribe_array(
                data, self.cfg["whisper"]["model"],
                initial_prompt=vocab.prompt())
            log.info("dictation: transcript = %r", text[:200])
            if text and self.cfg["dictation"].get("cleanup"):
                try:
                    text = summarize.cleanup_dictation(self.cfg, text)
                    log.info("dictation: text cleaned up via LLM")
                except Exception:
                    log.exception("dictation: cleanup failed, using raw text")
            if text:
                dictation_log.add(text)
                # Pasting uses synthetic keystrokes, which macOS only permits
                # from the main thread — hand it to the poll loop.
                self._events.put(("paste", text))
                log.info("dictation: queued paste of %d characters", len(text))
            else:
                self._events.put(("notify", "Dictation", "No speech detected."))
                log.info("dictation: empty transcript")
        except Exception:
            log.exception("dictation: failed")
            self._events.put(("notify", "Dictation failed",
                              "See ~/MyWhisper/mywhisper.log"))
        finally:
            _cleanup(mic_wav)
            self._events.put(("done",))
            log.info("dictation: cycle complete")

    def _finish_meeting(self):
        mic_wav, sys_wav, mix_wav = self._mic_wav, None, None
        try:
            log.info("meeting: stopping recorders")
            self.mic.stop()
            sys_wav = self.sysaudio.stop()
            mixed = audio.load_mono_16k(mic_wav)
            if sys_wav:
                mixed = audio.mix(mixed, audio.load_mono_16k(sys_wav))
            log.info("meeting: %.1fs of audio (system audio: %s)",
                     len(mixed) / 16000.0, "yes" if sys_wav else "no")
            if len(mixed) < _MIN_SAMPLES:
                log.warning("meeting: audio too short, skipping")
                self._events.put(("notify", "Meeting",
                                   "No audio captured - check microphone access."))
                return
            model = self.cfg["whisper"]["model"]
            log.info("meeting: transcribing")
            segments, text = transcribe.transcribe_array(
                mixed, model, initial_prompt=vocab.prompt())
            log.info("meeting: %d transcript segments", len(segments))

            diarized = False
            if self.cfg["diarization"].get("enabled") and diarize.available():
                try:
                    log.info("meeting: running speaker separation")
                    mix_wav = _tmp_wav()
                    audio.save_16k(mix_wav, mixed)
                    turns = diarize.diarize(mix_wav)
                    segments = diarize.label_segments(segments, turns)
                    diarized = True
                except Exception:
                    log.exception("meeting: speaker separation failed")
                    self._events.put(("notify", "Speaker separation skipped", ""))

            transcript_md = output.format_transcript(segments, diarized)
            try:
                log.info("meeting: summarizing via LLM")
                summary_md = summarize.summarize_transcript(self.cfg, text)
            except Exception as e:
                log.exception("meeting: summary failed")
                summary_md = f"_Summary failed ({e})._"

            path = output.save_meeting(self.out_dir, transcript_md, summary_md)
            log.info("meeting: saved %s", path)
            self._events.put(("notify", "Meeting notes ready", path.name))
        except Exception:
            log.exception("meeting: failed")
            self._events.put(("notify", "Meeting failed",
                              "See ~/MyWhisper/mywhisper.log"))
        finally:
            _cleanup(mic_wav, sys_wav, mix_wav)
            self._events.put(("done",))
            log.info("meeting: cycle complete")
