import logging
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import rumps

from . import (audio, autostart, calendar_lookup, config, dashboard, diarize,
               dictation_log, hotkeys, meeting_indicator, notes_pad, output,
               paste, recorder, screen_recording, sounds, summarize,
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

# A recording whose loudest RMS window stayed under this was true silence;
# above it, the mic clearly had signal (normal speech peaks ~0.02-0.06 RMS).
_SPEECH_LEVEL = 0.015

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


def _install_edit_menu():
    """Cmd-C/V/X/A/Z resolve through the app's main menu. A menu-bar app
    has no Edit menu, so keyboard paste silently fails in every window
    (dashboard text boxes, import panel). The menu is never visible
    (LSUIElement app) but still routes the shortcuts to whichever text
    field has focus."""
    from AppKit import NSApp, NSMenu, NSMenuItem
    main = NSApp.mainMenu()
    if main is None:
        main = NSMenu.alloc().init()
        NSApp.setMainMenu_(main)
    edit = NSMenu.alloc().initWithTitle_("Edit")
    for title, sel, key in (
            ("Undo", "undo:", "z"),
            ("Redo", "redo:", "Z"),       # uppercase = Cmd-Shift-Z
            ("Cut", "cut:", "x"),
            ("Copy", "copy:", "c"),
            ("Paste", "paste:", "v"),
            ("Select All", "selectAll:", "a")):
        edit.addItem_(
            NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title, sel, key))
    holder = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Edit", None, "")
    holder.setSubmenu_(edit)
    main.addItem_(holder)

    # Window shortcuts so the dashboard behaves like a real app window:
    # Cmd-W closes (hides) it, Cmd-M minimizes it.
    window_menu = NSMenu.alloc().initWithTitle_("Window")
    for title, sel, key in (
            ("Close Window", "performClose:", "w"),
            ("Minimize", "performMiniaturize:", "m")):
        window_menu.addItem_(
            NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title, sel, key))
    wholder = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Window", None, "")
    wholder.setSubmenu_(window_menu)
    main.addItem_(wholder)


@rumps.notifications
def _on_notification_click(info):
    """User clicked one of our notifications. Meeting-ready notifications
    carry the path of the saved .md file — open it."""
    try:
        path = info.get("open_path")
    except Exception:
        path = None
    if path and os.path.exists(path):
        subprocess.run(["open", path], check=False)
        log.info("notification click: opened %s", path)


def _preserve_last_dictation(path):
    """Keep the most recent dictation audio (a single file, overwritten
    every cycle) so a garbled recording can be played back afterwards to
    tell a mic problem from a transcription problem."""
    try:
        if path and os.path.exists(path):
            shutil.move(path, str(config.app_dir() / "last_dictation.wav"))
    except Exception:
        _cleanup(path)


class MyWhisperApp(rumps.App):
    def __init__(self):
        super().__init__("MyWhisper", title=_TITLES["idle"], quit_button=None)
        log.info("=== MyWhisper starting ===")

        # Hide the Dock icon. The venv's python re-execs Homebrew's
        # Python.app binary, so macOS shows *that* bundle's Dock icon
        # (the Python rocket) and our own Info.plist LSUIElement is
        # never consulted. Setting the activation policy at runtime
        # overrides it regardless of which binary is running us.
        try:
            from AppKit import NSApplication
            NSApplication.sharedApplication().setActivationPolicy_(1)
            log.info("dock icon hidden (accessory activation policy)")
        except Exception:
            log.exception("could not hide Dock icon")
        self.cfg = config.load()
        self.out_dir = config.output_dir(self.cfg)
        self.state = "idle"
        self.mic = recorder.MicRecorder()
        self.sysaudio = recorder.SystemAudioRecorder(_helper_path())
        self.waveform = waveform.Indicator(kind=config.get_visualization())
        self.meeting_panel = meeting_indicator.MeetingIndicator(
            on_stop=lambda: self._events.put(("stop_meeting",)))
        self.notes_panel = notes_pad.NotesPad()
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
            rumps.MenuItem("Open Last Meeting Notes",
                           callback=self._open_last_meeting),
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
        threading.Thread(target=self._scan_recovery, daemon=True).start()

        try:
            _install_edit_menu()
        except Exception:
            log.exception("edit menu install failed")

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

        # Screen Recording permission gates ScreenCaptureKit, which is how
        # we capture the other side of Zoom/Teams calls. Log it loudly so
        # the user can see in the log if it's missing — and we surface it
        # in the dashboard too.
        try:
            sr_ok = screen_recording.has_permission()
            log.info("screen recording (system audio) permission: %s",
                     "GRANTED" if sr_ok else "NOT GRANTED — meetings will "
                     "capture mic-only until you grant it in Dashboard → "
                     "Settings → System Audio")
        except Exception:
            log.exception("screen recording permission check failed")

        log.info("app ready (provider=%s, model=%s)",
                 self.cfg["llm"]["provider"], self.cfg["whisper"]["model"])

    def _prewarm(self):
        try:
            log.info("prewarm: loading speech model")
            transcribe.prewarm(self.cfg["whisper"]["model"])
            log.info("prewarm: speech model ready")
        except Exception:
            log.exception("prewarm failed")

    # -- crash recovery ------------------------------------------------
    # Recordings live in the temp dir while a session is running and are
    # deleted only on a clean finish, so audio found there at startup is
    # a recording some crash orphaned. Bytes on disk win: move them to
    # safety and rebuild what we can.

    def _scan_recovery(self):
        import glob
        import soundfile as sf
        time.sleep(10)   # let startup + model prewarm get going first
        try:
            now = time.time()
            found = []
            for p in sorted(glob.glob(
                    os.path.join(tempfile.gettempdir(), "mywhisper_*.wav"))):
                try:
                    st = os.stat(p)
                    if now - st.st_mtime < 120:
                        continue   # fresh — could belong to a live recorder
                    info = sf.info(p)
                    dur = info.frames / float(info.samplerate or 1)
                except Exception:
                    continue
                if dur < 5.0:
                    _cleanup(p)    # an aborted tap, nothing worth saving
                    continue
                found.append({"path": p, "mtime": st.st_mtime,
                              "dur": dur, "channels": info.channels})
            if not found:
                return

            log.warning("recovery: found %d orphaned recording(s)", len(found))
            rec_dir = config.app_dir() / "recovered"
            rec_dir.mkdir(parents=True, exist_ok=True)
            for f in found:
                ts = datetime.fromtimestamp(f["mtime"]).strftime("%Y-%m-%d_%H%M%S")
                kind = "system" if f["channels"] > 1 else "mic"
                dest = rec_dir / f"recovered_{ts}_{kind}.wav"
                n = 1
                while dest.exists():
                    dest = rec_dir / f"recovered_{ts}_{kind}_{n}.wav"
                    n += 1
                shutil.move(f["path"], dest)
                f["path"] = str(dest)

            # Pair a mic track with a system track recorded at the same
            # time (dual-channel meeting); a lone long mic track is a
            # mic-only meeting; a lone short one is kept but not processed.
            mics = [f for f in found if f["channels"] == 1]
            syss = [f for f in found if f["channels"] > 1]
            # Longest mic tracks first, and pair each with the candidate
            # system track closest in duration — the two tracks of one
            # meeting ran simultaneously, so their lengths nearly match.
            mics.sort(key=lambda f: -f["dur"])
            for mic in mics:
                candidates = [s for s in syss
                              if abs(s["mtime"] - mic["mtime"]) < 90]
                sys_match = min(
                    candidates,
                    key=lambda s: abs(s["dur"] - mic["dur"]),
                    default=None)
                if sys_match:
                    syss.remove(sys_match)
                if sys_match or mic["dur"] >= 90:
                    started = datetime.fromtimestamp(mic["mtime"] - mic["dur"])
                    self._recover_meeting(
                        mic["path"],
                        sys_match["path"] if sys_match else None, started)
                else:
                    self._events.put((
                        "notify", "Unfinished dictation recovered",
                        "Audio saved in the recovered folder "
                        "(menu → Open Notes Folder)."))
        except Exception:
            log.exception("recovery: scan failed")

    def _recover_meeting(self, mic_path, sys_path, started_at):
        log.info("recovery: rebuilding meeting from %s + %s",
                 mic_path, sys_path or "(no system audio)")
        self._events.put(("notify", "Recovering an unfinished meeting",
                          "Found a recording that never finished — "
                          "rebuilding it now."))
        mic_data = audio.load_mono_16k(mic_path)
        sys_data = audio.load_mono_16k(sys_path) if sys_path else None
        segments, labeled = transcribe.transcribe_meeting(
            mic_data, sys_data, self.cfg["whisper"]["model"],
            initial_prompt=vocab.prompt())
        if not segments:
            log.warning("recovery: no speech in orphaned recording")
            return
        transcript_md = output.format_transcript(segments, labeled)
        text = output.attributed_text(segments, labeled)

        cal_event = None
        try:
            cal_event = calendar_lookup.find_meeting_near(started_at)
        except Exception:
            log.exception("recovery: calendar lookup failed")
        cal_title = (cal_event.get("title") if cal_event else "") or ""

        path = output.save_meeting(
            config.app_dir(), transcript_md,
            "## ⏳ Summary pending\n\nIf this never fills in, click Redo "
            "on this meeting in the Dashboard.",
            title=cal_title, stamp=started_at)

        banner = ("> ♻️ Recovered recording — the app closed before this "
                  "meeting finished processing; it was rebuilt "
                  "automatically on restart.")
        title = ""
        try:
            title, summary_md = summarize.summarize_transcript(
                self.cfg, text, preset_id=config.get_meeting_preset())
            if not summary_md or not summary_md.strip():
                raise RuntimeError("empty summary")
        except Exception as e:
            log.exception("recovery: summary failed")
            summary_md = (f"## ⚠️ Summary failed ({e})\n\nThe transcript "
                          "below is intact — click Redo on this meeting "
                          "in the Dashboard to try again.")
        summary_md = f"{banner}\n\n{summary_md}"

        stamp = output.parse_meeting(path)["stamp"]
        output.rewrite_meeting(path, title or cal_title, stamp, summary_md,
                               "", transcript_md)
        if title:
            path = output.rename_meeting(path, title)
        log.info("recovery: rebuilt %s", path)
        self._last_meeting_path = str(path)
        self._events.put(("notify",
                          f"Recovered: {title or cal_title or 'meeting'}",
                          f"Click to open {path.name}",
                          {"open_path": str(path)}))
        self._events.put(("refresh_dashboard",))

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
                # Just log it and push state — the dashboard mic dropdown
                # updates live. No notification: macOS Continuity pops the
                # iPhone in and out constantly during calls, and for normal
                # plug-ins the user already knows what they did.
                added = [d for d in available if d not in prev]
                removed = [d for d in prev if d not in available]
                self._known_devices = list(available)
                for d in added:
                    log.info("audio device added: %s", d)
                for d in removed:
                    log.info("audio device removed: %s", d)
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
    def _notify(self, title, message, data=None):
        try:
            rumps.notification("MyWhisper", title, message, data=data)
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
                    self._notify(event[1], event[2],
                                 event[3] if len(event) > 3 else None)
                elif kind == "paste":
                    try:
                        paste.paste_text(event[1])
                        log.info("dictation: pasted %d characters", len(event[1]))
                    except Exception:
                        log.exception("paste failed")
                elif kind == "refresh_dashboard":
                    dashboard.refresh_if_open()
                elif kind == "done":
                    self.state = "idle"
                    self.meeting_panel.hide()
                    self.notes_panel.hide()
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

    def _open_last_meeting(self, _):
        path = getattr(self, "_last_meeting_path", None)
        if not path or not os.path.exists(path):
            # Nothing from this session — fall back to the newest meeting
            # file on disk.
            existing = sorted(Path(config.app_dir()).glob("meeting_*.md"),
                              key=lambda p: p.stat().st_mtime)
            path = str(existing[-1]) if existing else None
        if path and os.path.exists(path):
            subprocess.run(["open", path], check=False)
        else:
            self._notify("No meetings yet",
                         "Finished meeting notes will show up here.")

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
        self.notes_panel.show()
        self._sysaudio_ok = self.sysaudio.start(_tmp_wav())
        if not self._sysaudio_ok:
            log.warning("meeting: system audio unavailable: %s", self.sysaudio.error)
            # Distinguish permission denial from other failures.
            err = (self.sysaudio.error or "").lower()
            if "declined" in err or "tcc" in err or not screen_recording.has_permission():
                self._notify(
                    "Mic-only meeting (no system audio)",
                    "Screen Recording permission not granted — open Dashboard "
                    "→ Settings → System Audio to fix.")
            else:
                self._notify(
                    "System audio unavailable",
                    f"{self.sysaudio.error or ''} Recording microphone only.")

    def _click_stop_meeting(self, _):
        """Stop button clicked while a meeting is recording."""
        if self.state != "meeting":
            return
        log.info("meeting: stop requested")
        # Capture the user's typed notes BEFORE we hide the pad
        self._meeting_notes = self.notes_panel.get_text()
        if self._meeting_notes:
            log.info("meeting: captured %d chars of live notes",
                     len(self._meeting_notes))
        self.notes_panel.hide()
        # Switch the floating indicator to "processing" mode — it stays
        # visible and shows live LLM progress instead of disappearing.
        self.meeting_panel.set_processing("preparing audio…", 0)
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
                drops = transcribe.last_drops
                had_signal = self.mic.max_level >= _SPEECH_LEVEL
                log.info("dictation: empty transcript (max mic level %.4f)",
                         self.mic.max_level)
                if drops.get("non-english") or drops.get("repetition"):
                    # Whisper produced output but it was hallucinated junk —
                    # the audio reached us garbled. Very different problem
                    # from silence, so say so.
                    self._events.put(("notify", "Dictation",
                                      "Audio came through garbled — kept a "
                                      "copy as last_dictation.wav (menu → "
                                      "Open Notes Folder)."))
                elif had_signal:
                    # The mic clearly had signal, so an empty transcript is
                    # a failure, not silence — don't gaslight the user.
                    self._events.put(("notify", "Dictation",
                                      "Heard audio but couldn't make out "
                                      "words — try again? (kept a copy as "
                                      "last_dictation.wav)"))
                else:
                    self._events.put(("notify", "Dictation",
                                      "No speech detected."))
        except Exception:
            log.exception("dictation: failed")
            self._events.put(("notify", "Dictation failed",
                              "See ~/MyWhisper/mywhisper.log"))
        finally:
            _preserve_last_dictation(mic_wav)
            self._events.put(("done",))
            log.info("dictation: cycle complete")

    def _finish_meeting(self):
        mic_wav, sys_wav, dia_wav = self._mic_wav, None, None
        summary_failed = False
        try:
            log.info("meeting: stopping recorders")
            self._stage("Saving audio…")
            self.mic.stop()
            sys_wav = self.sysaudio.stop()

            mic_data = audio.load_mono_16k(mic_wav)
            sys_data = audio.load_mono_16k(sys_wav) if sys_wav else None
            log.info("meeting: %.1fs mic / %.1fs call audio (system audio: %s)",
                     len(mic_data) / 16000.0,
                     (len(sys_data) / 16000.0) if sys_data is not None else 0.0,
                     "yes" if sys_wav else "no")

            mic_short = len(mic_data) < _MIN_SAMPLES
            sys_short = sys_data is None or len(sys_data) < _MIN_SAMPLES
            if mic_short and sys_short:
                log.warning("meeting: audio too short, skipping")
                self._events.put(("notify", "Meeting",
                                   "No audio captured - check microphone access."))
                return

            model = self.cfg["whisper"]["model"]
            self._stage("Transcribing audio (Whisper)…")
            # Dual-channel: mic = "Me", system audio = "Others". The two
            # streams are transcribed independently and merged — never
            # mixed into one waveform (that was the source of the garbled
            # Japanese/repeat hallucinations).
            segments, speaker_labeled = transcribe.transcribe_meeting(
                mic_data, sys_data, model,
                initial_prompt=vocab.prompt(),
                on_stage=lambda msg: self._stage(msg))
            log.info("meeting: %d merged segments (speaker_labeled=%s)",
                     len(segments), speaker_labeled)

            # Mic-only (in-person) meeting → optionally separate the people
            # in the room with pyannote. Skipped for remote calls, where
            # the Me/Others channel split already gives clean separation.
            if (not speaker_labeled
                    and self.cfg["diarization"].get("enabled")
                    and diarize.available()):
                try:
                    log.info("meeting: running speaker separation (mic-only)")
                    self._stage("Separating speakers…")
                    dia_wav = _tmp_wav()
                    audio.save_16k(dia_wav, mic_data)
                    turns = diarize.diarize(dia_wav)
                    segments = diarize.label_segments(segments, turns)
                    speaker_labeled = True
                except Exception:
                    log.exception("meeting: speaker separation failed")
                    self._events.put(("notify", "Speaker separation skipped", ""))

            transcript_md = output.format_transcript(segments, speaker_labeled)
            text = output.attributed_text(segments, speaker_labeled)
            provider_name = config.LLM_PROVIDERS[config.get_llm_provider()]["label"]
            model_name = config.get_llm_model(config.get_llm_provider()) or "(not set)"

            cal_event = None
            try:
                self._stage("Checking calendar…")
                start_ts = getattr(self, "_meeting_started_at", None)
                cal_event = calendar_lookup.find_meeting_near(start_ts)
            except Exception:
                log.exception("meeting: calendar lookup failed")

            # Crash-safety: write the meeting to disk the moment the
            # transcript exists. If the app dies or is quit during the
            # LLM summary (it happened), the recording is not lost — the
            # file is already there with a pending note, and the
            # dashboard's Redo button can fill in the summary later.
            cal_title = (cal_event.get("title") if cal_event else "") or ""
            self._stage("Saving transcript…")
            path = output.save_meeting(
                config.app_dir(), transcript_md,
                "## ⏳ Summary pending\n\nIf this note never fills in, the "
                "app was closed mid-processing — open the Dashboard and "
                "click Redo on this meeting to generate the summary.",
                title=cal_title,
                live_notes=getattr(self, "_meeting_notes", ""))
            log.info("meeting: transcript saved early -> %s", path)

            title = ""
            try:
                log.info("meeting: summarizing via LLM (provider=%s, model=%s)",
                         provider_name, model_name)

                # Stage callback: streams progress to the floating indicator
                def _on_stage(stage_text, chars):
                    self.meeting_panel.set_processing(stage_text, chars)

                # The calendar event is passed as CONTEXT (real attendee
                # names beat "Speaker 1") but the title comes from what
                # was actually said — calendar titles proved unreliable
                # (generic, or a nearby event that wasn't this meeting).
                title, summary_md = summarize.summarize_transcript(
                    self.cfg, text,
                    preset_id=getattr(self, "_meeting_preset", None),
                    calendar_event=cal_event,
                    live_notes=getattr(self, "_meeting_notes", ""),
                    on_stage=_on_stage,
                )
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

            # If system audio failed, prepend a small banner to the
            # summary so the user always knows what they're looking at.
            if not getattr(self, "_sysaudio_ok", True):
                summary_md = (
                    "> ⚠️ **Microphone-only recording.** The other side of "
                    "the conversation was not captured because Screen "
                    "Recording permission is not granted. Open Dashboard → "
                    "Settings → System Audio to fix for next time.\n\n"
                    + summary_md
                )

            self._stage("Saving notes…")
            final_title = title or cal_title   # calendar = fallback only
            stamp = output.parse_meeting(path)["stamp"]
            output.rewrite_meeting(path, final_title, stamp, summary_md,
                                   getattr(self, "_meeting_notes", ""),
                                   transcript_md)
            if title:
                # The content title arrived after the early save — put
                # it in the filename too (replaces any calendar slug).
                path = output.rename_meeting(path, title)
            log.info("meeting: saved %s", path)
            self._last_meeting_path = str(path)
            if summary_failed:
                sounds.play_failed(self.cfg)
                self._events.put(("notify", "Meeting saved (summary failed)",
                                  "Click to open and see details.",
                                  {"open_path": str(path)}))
            else:
                sounds.play_done(self.cfg)
                self._events.put(("notify",
                                  title or "Meeting notes ready",
                                  f"Click to open {path.name}",
                                  {"open_path": str(path)}))
            self._events.put(("refresh_dashboard",))
        except Exception:
            log.exception("meeting: failed")
            sounds.play_failed(self.cfg)
            self._events.put(("notify", "Meeting failed",
                              "See ~/MyWhisper/mywhisper.log"))
        finally:
            _cleanup(mic_wav, sys_wav, dia_wav)
            self._events.put(("done",))
            log.info("meeting: cycle complete")

    def _stage(self, text):
        """Push a stage label to the floating indicator (background-thread
        safe — set_processing only touches the view, which AppKit
        marshals to the main thread on its own)."""
        try:
            self.meeting_panel.set_processing(text, 0)
        except Exception:
            pass
