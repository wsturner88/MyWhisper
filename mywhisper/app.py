import logging
import os
import queue
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import rumps

from . import (audio, autostart, calendar_lookup, config, dashboard, diarize,
               dictation_log, hotkeys, meeting_indicator, output, paste,
               recorder, sounds, summarize, transcribe, vocab, waveform)

HELPER_NAME = "mywhisper-sysaudio"

# Menu bar label. Image icons do not render reliably on recent macOS, so
# the indicator is plain text — this is what shows in the menu bar.
_TITLES = {
    "idle": "🎙️ MyWhisper",
    "dictation": "🔴 Recording",
    "meeting": "🔴 Recording",
    "processing": "⏳ Working",
}

_LOG_PATH = config.log_path()
_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
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
        self.meeting_panel = meeting_indicator.MeetingIndicator(
            on_stop=lambda: self._events.put(("stop_meeting",)))
        self._events = queue.Queue()
        self._mic_wav = None
        self._dictation_started = 0.0

        # Restore the saved microphone, if that device is still connected.
        saved = config.get_selected_mic()
        if saved and saved in [name for _, name in recorder.input_devices()]:
            self.mic.device = saved
            log.info("restored microphone: %s", saved)

        self.mi_dict = rumps.MenuItem("Start Dictation", callback=self._click_dictation)

        # Start Meeting is a submenu — one menu item per preset. Clicking a
        # preset starts the meeting with that preset directly.
        self.mi_meet_menu = rumps.MenuItem("Start Meeting")
        self._preset_items = {}
        self._rebuild_meeting_submenu()
        # Keep the submenu in sync when the user adds/edits/deletes presets
        # in the dashboard.
        dashboard.on_presets_changed(self._rebuild_meeting_submenu)

        # Stop button — disabled (greyed out) when nothing is recording.
        self.mi_stop = rumps.MenuItem("Stop Recording", callback=None)
        self.mi_status = rumps.MenuItem("Idle", callback=None)

        self.menu = [
            self.mi_dict,
            self.mi_meet_menu,
            self.mi_stop,
            None,
            self.mi_status,
            None,
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

        # Ask for Calendar access once. If user has never been prompted,
        # this shows the system dialog. If already decided, this is a
        # no-op. Either way it's non-blocking — we just log the result.
        try:
            if calendar_lookup.authorization_status() == 0:
                log.info("requesting calendar access")
                calendar_lookup.request_access()
            else:
                log.info("calendar access: %s", calendar_lookup.status_label())
        except Exception:
            log.exception("calendar permission request failed")

        log.info("app ready (provider=%s, model=%s)",
                 self.cfg["llm"]["provider"], self.cfg["whisper"]["model"])

    def _prewarm(self):
        try:
            log.info("prewarm: loading speech model")
            transcribe.prewarm(self.cfg["whisper"]["model"])
            log.info("prewarm: speech model ready")
        except Exception:
            log.exception("prewarm failed")

    # -- meeting submenu rebuilds when presets change ------------------
    def _rebuild_meeting_submenu(self):
        try:
            # clear() fails before the submenu is attached to a parent; that
            # happens on the very first build, when there's nothing to clear
            # anyway, so just swallow the error.
            try:
                self.mi_meet_menu.clear()
            except Exception:
                pass
            self._preset_items = {}
            for pid, info in config.MEETING_PRESETS.items():
                item = rumps.MenuItem(info["label"], callback=self._on_pick_preset)
                item._preset_id = pid
                self._preset_items[pid] = item
                self.mi_meet_menu.add(item)
            log.info("meeting submenu rebuilt (%d presets)",
                     len(self._preset_items))
        except Exception:
            log.exception("meeting submenu rebuild failed")

    # -- live setting sync from the dashboard -------------------------
    def _refresh_settings_from_disk(self):
        """Pick up mic / visualization changes made via the dashboard,
        and notice when audio devices are hot-plugged."""
        try:
            # Refresh PortAudio's device cache at most every 3s, and only
            # when idle (re-init kills active mic streams).
            now = time.monotonic()
            last = getattr(self, "_last_device_refresh", 0.0)
            do_refresh = (
                self.state == "idle"
                and (now - last) >= 3.0
            )
            if do_refresh:
                self._last_device_refresh = now
            available = [name for _, name in recorder.input_devices(
                refresh=do_refresh)]

            # Detect hot-plug changes (new mic plugged in, mic unplugged).
            prev = getattr(self, "_known_devices", None)
            if prev is None:
                # First poll — just record the baseline silently.
                self._known_devices = list(available)
            elif tuple(available) != tuple(prev):
                added = [d for d in available if d not in prev]
                removed = [d for d in prev if d not in available]
                self._known_devices = list(available)
                for d in added:
                    log.info("audio device added: %s", d)
                for d in removed:
                    log.info("audio device removed: %s", d)
                # Tell the user about new mics (most common useful event).
                if added:
                    label = added[0] if len(added) == 1 \
                        else f"{len(added)} new mics"
                    self._notify(
                        "New microphone available",
                        f"{label} — pick it in Settings if you want to use it.",
                    )
                # If the dashboard panel is open, refresh its mic dropdown.
                try:
                    dashboard._push_state()
                except Exception:
                    pass

            saved_mic = config.get_selected_mic()
            new_mic = saved_mic if saved_mic in available else None
            if new_mic != self.mic.device:
                self.mic.device = new_mic
                log.info("mic updated: %s", new_mic or "system default")
            new_viz = config.get_visualization()
            if new_viz != self.waveform.kind:
                self.waveform.set_kind(new_viz)
                log.info("visualization updated from dashboard: %s", new_viz)
        except Exception:
            log.exception("settings refresh failed")

    def _tick_waveform(self, _):
        if self.state == "dictation":
            self.waveform.update(self.mic.level)
        elif self.state == "meeting":
            self.meeting_panel.tick()

    # -- UI helpers ---------------------------------------------------
    def _notify(self, title, message):
        try:
            rumps.notification("MyWhisper", title, message)
        except Exception:
            pass

    def _sync(self):
        """Reflect the current state in the menu bar."""
        if self.state == "idle":
            self.mi_dict.title = "Start Dictation"
            self.mi_meet_menu.title = "Start Meeting"
            self.mi_stop.title = "Stop Recording"
            self.mi_stop.set_callback(None)  # greyed out
            self.mi_status.title = "Idle"
        elif self.state == "dictation":
            self.mi_dict.title = "Stop Dictation"
            self.mi_meet_menu.title = "Start Meeting"
            self.mi_stop.title = "Stop Recording"
            self.mi_stop.set_callback(None)
            self.mi_status.title = "Recording dictation..."
        elif self.state == "meeting":
            self.mi_dict.title = "Start Dictation"
            self.mi_meet_menu.title = "🔴 Meeting in progress"
            self.mi_stop.title = "■ Stop Meeting"
            self.mi_stop.set_callback(self._click_stop_meeting)
            self.mi_status.title = "Recording meeting..."
        elif self.state == "processing":
            self.mi_dict.title = "Working..."
            self.mi_meet_menu.title = "Working..."
            self.mi_stop.title = "Stop Recording"
            self.mi_stop.set_callback(None)
            self.mi_status.title = "Working..."
        self.title = _TITLES[self.state]

    # -- event loop (runs on the main thread) -------------------------
    def _poll(self, _):
        self._check_triggers()
        self._refresh_settings_from_disk()
        try:
            while True:
                event = self._events.get_nowait()
                kind = event[0]
                if kind == "toggle":
                    if event[1] == "dictation":
                        self._click_dictation(None)
                    else:
                        self._toggle_meeting()
                elif kind == "stop_meeting":
                    self._click_stop_meeting(None)
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
                    self._toggle_meeting()

    # -- menu callbacks ----------------------------------------------
    def _open_dashboard(self, _):
        dashboard.open_dashboard()

    def _open_folder(self, _):
        subprocess.run(["open", str(config.app_dir())], check=False)

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

    def _toggle_meeting(self):
        """Used by hotkey/trigger files. Starts with last-used preset, or
        stops if a meeting is already running."""
        if self.state == "idle":
            class _Fake:
                pass
            fake = _Fake()
            fake._preset_id = config.get_meeting_preset()
            self._on_pick_preset(fake)
        elif self.state == "meeting":
            self._click_stop_meeting(None)

    def _on_pick_preset(self, sender):
        """Submenu item clicked — start a meeting with the chosen preset."""
        if self.state != "idle":
            return  # already recording; ignore (Stop button is what they want)
        preset_id = getattr(sender, "_preset_id", "general")
        self._meeting_preset = preset_id
        self._meeting_started_at = datetime.now()
        self._calendar_event = None  # set on stop
        config.set_meeting_preset(preset_id)
        log.info("meeting: start requested (preset=%s)", preset_id)
        self._mic_wav = _tmp_wav()
        try:
            self.mic.start(self._mic_wav)
        except Exception:
            log.exception("meeting: could not start microphone")
            self._notify("Microphone error", "Could not start recording.")
            return
        self.state = "meeting"
        self._sync()
        self.meeting_panel.show()
        if not self.sysaudio.start(_tmp_wav()):
            log.warning("meeting: system audio unavailable: %s", self.sysaudio.error)
            self._notify("System audio unavailable",
                         f"{self.sysaudio.error or ''} Recording microphone only.")

    def _click_stop_meeting(self, _):
        """Stop button clicked while a meeting is recording."""
        if self.state != "meeting":
            return
        log.info("meeting: stop requested")
        self.meeting_panel.hide()
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
            provider_name = config.LLM_PROVIDERS[config.get_llm_provider()]["label"]
            model_name = config.get_llm_model(config.get_llm_provider()) or "(not set)"

            # Look up a matching calendar event (Outlook, Google, Apple —
            # whatever's synced to macOS). Soft lookup — None if no match
            # or no permission.
            cal_event = None
            try:
                start_ts = getattr(self, "_meeting_started_at", None)
                cal_event = calendar_lookup.find_meeting_near(start_ts)
            except Exception:
                log.exception("meeting: calendar lookup failed")

            summary_failed = False
            title = ""
            try:
                log.info("meeting: summarizing via LLM (provider=%s, model=%s)",
                         provider_name, model_name)
                title, summary_md = summarize.summarize_transcript(
                    self.cfg, text,
                    preset_id=getattr(self, "_meeting_preset", None),
                    calendar_event=cal_event,
                )
                # Calendar title beats LLM-generated title when present.
                if cal_event and cal_event.get("title"):
                    title = cal_event["title"]
                if not summary_md or not summary_md.strip():
                    raise RuntimeError("Empty summary returned by LLM.")
                if title:
                    log.info("meeting: title = %r", title)
            except Exception as e:
                log.exception("meeting: summary failed")
                summary_failed = True
                summary_md = (
                    "## ⚠️ Summary Failed\n\n"
                    f"The LLM did not produce a summary.\n\n"
                    f"- **Provider:** {provider_name}\n"
                    f"- **Model:** `{model_name}`\n"
                    f"- **Error:** {e}\n\n"
                    "**What to do:** Open MyWhisper → Dashboard → "
                    "Settings → LLM and try a different model, or hit "
                    "Test Connection to diagnose. Your full transcript is "
                    "below — once the LLM works, you can paste it into "
                    "ChatGPT/Claude manually for a one-time summary, or "
                    "re-run with a working setup."
                )

            path = output.save_meeting(config.app_dir(), transcript_md,
                                       summary_md, title=title)
            log.info("meeting: saved %s", path)
            if summary_failed:
                self._events.put(("notify", "Meeting saved (summary failed)",
                                  "Check the file for details."))
            else:
                self._events.put(("notify",
                                  title or "Meeting notes ready",
                                  path.name))
        except Exception:
            log.exception("meeting: failed")
            self._events.put(("notify", "Meeting failed",
                              "See ~/MyWhisper/mywhisper.log"))
        finally:
            _cleanup(mic_wav, sys_wav, mix_wav)
            self._events.put(("done",))
            log.info("meeting: cycle complete")
