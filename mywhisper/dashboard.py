"""Native floating dashboard window — meetings, dictation history, settings.

Built with NSPanel + WKWebView via PyObjC. A two-way bridge lets the HTML UI
read and write settings through a tiny Python message handler.
"""

import html
import json
import logging
import os
import re
import subprocess
import threading
from datetime import datetime
from pathlib import Path

import objc
from AppKit import (
    NSPanel, NSScreen, NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSWindowStyleMaskResizable, NSWindowStyleMaskUtilityWindow,
    NSBackingStoreBuffered, NSFloatingWindowLevel, NSColor,
    NSWindowStyleMaskFullSizeContentView, NSApp,
    NSMakeRect, NSMakeSize, NSOpenPanel, NSObject, NSURL,
)
from Foundation import NSURL as FNSURL

from . import (config, dictation_log, autostart, output, vocab, recorder,
               screen_recording)

log = logging.getLogger("mywhisper")

objc.loadBundle(
    "WebKit", globals(),
    bundle_path="/System/Library/Frameworks/WebKit.framework",
)
WKWebView = objc.lookUpClass("WKWebView")
WKWebViewConfiguration = objc.lookUpClass("WKWebViewConfiguration")
WKUserContentController = objc.lookUpClass("WKUserContentController")

_panel = None
_webview = None
_bridge = None  # NSObject delegate; keep a reference or PyObjC will GC it
_PANEL_W = 1080
_PANEL_H = 720

# Callbacks invoked when the user's custom preset list changes — the app
# registers one so it can rebuild the Start Meeting submenu live.
_presets_changed_callbacks = []


def on_presets_changed(callback):
    """Register a function to be called whenever custom presets change."""
    if callback and callback not in _presets_changed_callbacks:
        _presets_changed_callbacks.append(callback)


def _notify_presets_changed():
    for cb in list(_presets_changed_callbacks):
        try:
            cb()
        except Exception:
            log.exception("dashboard: presets-changed callback failed")


# -- Data gathering for the dashboard ---------------------------------------

def _meeting_files():
    out_dir = config.app_dir()
    if not out_dir.exists():
        return []
    files = sorted(out_dir.glob("meeting_*.md"), reverse=True)
    meetings = []
    this_year = datetime.now().year
    for f in files[:50]:
        try:
            raw = f.read_text()
            parts = output.parse_meeting(f)
        except Exception:
            continue
        # The heading inside the file is the real title; the filename slug
        # is the fallback. A friendly date/time renders underneath.
        title = (parts.get("title") or "").strip()
        when = ""
        m = re.match(r"meeting_(\d{4}-\d{2}-\d{2})_(\d{4})(?:_(.+))?$", f.stem)
        if m:
            date_s, time_s, slug = m.group(1), m.group(2), m.group(3) or ""
            if not title:
                title = slug.replace("_", " ").strip() or "Meeting (no title)"
            try:
                dt = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H%M")
                day = dt.strftime("%a %b %-d")
                if dt.year != this_year:
                    day += f", {dt.year}"
                when = day + dt.strftime(" · %-I:%M %p")
            except ValueError:
                when = date_s
        meetings.append({
            "title": title or f.stem,
            "when": when,
            "filename": f.name,
            "summary_md": parts.get("summary_md", ""),
            "notes_md": parts.get("notes_md", ""),
            "transcript_md": parts.get("transcript_md", ""),
            "content": raw,
        })
    return meetings


def _state_snapshot():
    """Everything the JS side needs to render the settings form."""
    provider = config.get_llm_provider()
    info = config.LLM_PROVIDERS[provider]
    api_key = ""
    if info.get("key_name"):
        api_key = config.get_secret(info["key_name"]) or ""
    masked = (api_key[:4] + "•" * 6 + api_key[-4:]) if len(api_key) >= 12 else ""

    try:
        # The app's poll loop handles refreshing PortAudio's device list
        # on a throttle (and only when idle). Here we just read whatever
        # is currently cached — cheap and never disrupts a recording.
        mic_devices = [name for _, name in recorder.input_devices()]
    except Exception:
        mic_devices = []

    return {
        "data_dir": str(config.app_dir()),
        "llm_provider": provider,
        "llm_providers": [
            {"id": pid, "label": info["label"],
             "needs_key": bool(info.get("key_name")),
             "needs_url": bool(info.get("needs_url"))}
            for pid, info in config.LLM_PROVIDERS.items()
        ],
        "llm_model": config.get_llm_model(provider),
        "llm_needs_key": bool(info.get("key_name")),
        "llm_key_optional": bool(info.get("key_optional")),
        "llm_needs_url": bool(info.get("needs_url")),
        "custom_llm_url": config.get_custom_llm_url(),
        "api_key_masked": masked,
        "api_key_set": bool(api_key),
        "mic": config.get_selected_mic() or "",
        "mic_devices": mic_devices,
        "visualization": config.get_visualization(),
        "autostart": autostart.is_enabled(),
        "screen_recording_granted": screen_recording.has_permission(),
        "vocabulary": _load_vocab_text(),
        "meeting_preset": config.get_meeting_preset(),
        "builtin_presets": [
            {"id": pid, "label": p["label"], "description": p["description"]}
            for pid, p in config.BUILTIN_PRESETS.items()
        ],
        "custom_presets": config.get_custom_presets(),
        "starter_presets": config.STARTER_PRESETS,
    }


def _load_vocab_text():
    try:
        path = vocab.ensure_file()
        return path.read_text()
    except Exception:
        return ""


def _save_vocab_text(text):
    try:
        path = vocab.ensure_file()
        path.write_text(text)
        return True
    except Exception:
        log.exception("dashboard: save vocab failed")
        return False


# -- Bridge: messages from JavaScript --------------------------------------

class _BridgeHandler(NSObject):
    """Receives postMessage() calls from the dashboard HTML."""

    def callJS_(self, js):
        """Runs on the main thread (via performSelectorOnMainThread) —
        WebKit requires evaluateJavaScript to be called there."""
        try:
            if _webview is not None:
                _webview.evaluateJavaScript_completionHandler_(str(js), None)
        except Exception:
            log.exception("dashboard: callJS failed")

    def refreshPanel_(self, _arg):
        """Main-thread trampoline for refresh_if_open() — used by worker
        threads that just saved a new meeting file."""
        refresh_if_open()

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        try:
            body = dict(message.body())
            action = body.get("action")
            log.info("dashboard bridge: %s", action)
            handler = _ACTIONS.get(action)
            if handler is None:
                log.warning("dashboard bridge: unknown action %r", action)
                return
            handler(body)
        except Exception:
            log.exception("dashboard bridge: failed")


def _act_set_provider(body):
    config.set_llm_provider(body.get("value", "openrouter"))
    _push_state()


def _act_set_model(body):
    provider = config.get_llm_provider()
    config.set_llm_model(provider, body.get("value", "").strip())
    _push_state()


def _act_set_api_key(body):
    provider = config.get_llm_provider()
    info = config.LLM_PROVIDERS[provider]
    if not info.get("key_name"):
        return  # custom provider doesn't use a key
    val = body.get("value", "").strip()
    if val and "•" not in val:
        config.set_secret(info["key_name"], val)
    _push_state()


def _act_set_custom_url(body):
    config.set_custom_llm_url(body.get("value", ""))
    # Clear the cached API style so the next model fetch re-probes.
    from . import llm
    llm._custom_api_cache.clear()
    _push_state()


def _act_test_llm(body):
    # Network call with a long timeout — run it off the main thread so a
    # slow provider can't freeze the menu bar and push-to-talk.
    def _work():
        from . import llm
        try:
            ok, msg = llm.test_connection()
        except Exception as e:
            ok, msg = False, str(e)
        _call_js("onTestResult", {"ok": bool(ok), "message": str(msg)})

    threading.Thread(target=_work, daemon=True).start()


def _act_fetch_models(body):
    def _work():
        from . import llm
        try:
            models = llm.list_models()
            _call_js("onModelsLoaded", {
                "ok": True,
                "models": models,
                "provider": config.get_llm_provider(),
            })
        except Exception as e:
            log.exception("dashboard: fetch_models failed")
            _call_js("onModelsLoaded", {
                "ok": False,
                "error": str(e),
                "provider": config.get_llm_provider(),
            })

    threading.Thread(target=_work, daemon=True).start()


def _act_set_mic(body):
    val = body.get("value", "")
    config.set_selected_mic(val)
    _push_state()


def _act_set_visualization(body):
    config.set_visualization(body.get("value", "waveform"))
    _push_state()


def _act_set_autostart(body):
    try:
        if body.get("value"):
            autostart.enable()
        else:
            autostart.disable()
    except Exception:
        log.exception("dashboard: autostart toggle failed")
    _push_state()


def _act_save_vocab(body):
    _save_vocab_text(body.get("value", ""))


def _act_set_preset(body):
    config.set_meeting_preset(body.get("value", "general"))
    _push_state()


def _act_save_custom_preset(body):
    # Legacy single-preset save — converted to update_custom_preset
    val = body.get("value") or {}
    config.add_custom_preset(val.get("label", ""), val.get("focus", ""))
    _push_state()


def _act_add_custom_preset(body):
    val = body.get("value") or {}
    config.add_custom_preset(val.get("label", ""), val.get("focus", ""))
    _notify_presets_changed()
    _push_state()


def _act_update_custom_preset(body):
    val = body.get("value") or {}
    config.update_custom_preset(
        val.get("id", ""), val.get("label", ""), val.get("focus", ""))
    _notify_presets_changed()
    _push_state()


def _act_delete_custom_preset(body):
    val = body.get("value")
    preset_id = val.get("id") if isinstance(val, dict) else val
    config.delete_custom_preset(preset_id or "")
    _notify_presets_changed()
    _push_state()


def _act_request_screen_recording(body):
    # If already granted, just refresh the UI — nothing to do.
    if screen_recording.has_permission():
        _push_state()
        return
    # Try the system prompt. macOS will silently no-op if the user has
    # already been asked once (granted or denied), so after a short
    # delay we check and, if still not granted, open System Settings
    # to the right pane.
    screen_recording.request_permission()
    import threading, time

    def _followup():
        time.sleep(1.5)
        if not screen_recording.has_permission():
            screen_recording.open_settings()
        _push_state()

    threading.Thread(target=_followup, daemon=True).start()
    _push_state()


def _act_open_screen_recording_settings(body):
    screen_recording.open_settings()


def _act_import_transcript(body):
    """Summarize pasted text (Voice Memos transcript, Teams notes, …)
    through the normal meeting pipeline and save it as a meeting file."""
    val = body.get("value") or {}
    text = str(val.get("text") or "").strip()
    title_in = str(val.get("title") or "").strip()
    preset = str(val.get("preset") or "").strip() or None
    if not text:
        _call_js("onImportDone", {"ok": False, "error": "No text pasted."})
        return

    def _work():
        import time
        from . import output, summarize
        try:
            _call_js("onImportStage", {"stage": "Summarizing with AI…"})
            title, summary_md = summarize.summarize_transcript(
                config.load(), text, preset_id=preset,
                on_stage=lambda stage, chars=0: _call_js(
                    "onImportStage", {"stage": str(stage)}))
            if not summary_md or not summary_md.strip():
                raise RuntimeError("The LLM returned an empty summary.")
            summary_md = ("> 📥 Imported transcript — summarized from "
                          "pasted text, not recorded by MyWhisper.\n\n"
                          + summary_md)
            path = output.save_meeting(config.app_dir(), text, summary_md,
                                       title=title_in or title)
            log.info("dashboard: imported transcript -> %s", path)
            _call_js("onImportDone", {"ok": True, "filename": path.name})
            time.sleep(1.2)   # let the ✓ register before the page reloads
            _bridge.performSelectorOnMainThread_withObject_waitUntilDone_(
                "refreshPanel:", None, False)
        except Exception as e:
            log.exception("dashboard: import transcript failed")
            _call_js("onImportDone", {"ok": False, "error": str(e)})

    threading.Thread(target=_work, daemon=True).start()


def _act_resummarize_meeting(body):
    """Re-run the AI summary for an existing meeting from its saved
    transcript — no re-recording. Useful after switching to a better
    model or when a summary came out weak."""
    name = os.path.basename(str(body.get("value") or ""))
    path = config.app_dir() / name
    if not (name.startswith("meeting_") and name.endswith(".md")
            and path.exists()):
        _call_js("onResummarizeDone",
                 {"ok": False, "filename": name, "error": "File not found."})
        return

    def _work():
        from . import output, summarize
        try:
            parts = output.parse_meeting(path)
            transcript = parts["transcript_md"].strip()
            if not transcript or transcript == "_(no speech detected)_":
                raise RuntimeError("This meeting has no transcript to "
                                   "summarize.")
            llm_input = output.transcript_to_attributed(transcript)

            _call_js("onResummarizeStage",
                     {"filename": name, "stage": "Re-summarizing…"})
            _new_title, summary_md = summarize.summarize_transcript(
                config.load(), llm_input,
                live_notes=parts["notes_md"],
                on_stage=lambda stage, chars=0: _call_js(
                    "onResummarizeStage",
                    {"filename": name, "stage": str(stage)}))
            if not summary_md or not summary_md.strip():
                raise RuntimeError("The LLM returned an empty summary.")

            # Preserve any leading banner (e.g. 'mic-only' or 'imported')
            # from the previous summary so that context isn't lost.
            banner_lines = []
            for line in parts["summary_md"].split("\n"):
                if line.startswith(">"):
                    banner_lines.append(line)
                else:
                    break
            banner = "\n".join(banner_lines).strip()
            if banner:
                summary_md = f"{banner}\n\n{summary_md}"

            output.rewrite_meeting(
                path, parts["title"], parts["stamp"], summary_md,
                parts["notes_md"], transcript)
            log.info("dashboard: re-summarized %s", name)
            fresh = output.parse_meeting(path)
            _call_js("onResummarizeDone",
                     {"ok": True, "filename": name,
                      "meeting": {
                          "title": fresh.get("title", ""),
                          "summary_md": fresh.get("summary_md", ""),
                          "notes_md": fresh.get("notes_md", ""),
                          "transcript_md": fresh.get("transcript_md", ""),
                          "content": path.read_text(),
                      }})
        except Exception as e:
            log.exception("dashboard: resummarize failed")
            _call_js("onResummarizeDone",
                     {"ok": False, "filename": name, "error": str(e)})

    threading.Thread(target=_work, daemon=True).start()


def _act_record_meeting(body):
    """Sidebar Record button — poke the app's trigger-file channel so the
    normal meeting flow (preset, indicator, notes pad) takes over."""
    try:
        (config.app_dir() / "meeting_trigger").touch()
    except Exception:
        log.exception("dashboard: record trigger failed")


def _act_open_meeting(body):
    """Open a meeting .md file in the default editor. Only basenames of
    real meeting files inside the data folder are accepted."""
    name = os.path.basename(str(body.get("value") or ""))
    path = config.app_dir() / name
    if name.startswith("meeting_") and name.endswith(".md") and path.exists():
        subprocess.run(["open", str(path)], check=False)


def _act_close_panel(body):
    try:
        if _panel is not None:
            _panel.orderOut_(None)
    except Exception:
        log.exception("dashboard: close failed")


def _act_pick_folder(body):
    panel = NSOpenPanel.openPanel()
    panel.setCanChooseDirectories_(True)
    panel.setCanChooseFiles_(False)
    panel.setAllowsMultipleSelection_(False)
    panel.setPrompt_("Use This Folder")
    panel.setMessage_("Choose where MyWhisper should store recordings, "
                      "transcripts, and settings.")
    panel.setDirectoryURL_(FNSURL.fileURLWithPath_(str(config.app_dir().parent)))
    if panel.runModal() == 1:  # NSModalResponseOK
        url = panel.URL()
        new_path = url.path()
        # If the user picked the parent of an existing MyWhisper dir, append
        # MyWhisper so we don't dump files into Documents itself.
        if Path(new_path).name != "MyWhisper":
            new_path = str(Path(new_path) / "MyWhisper")
        try:
            old = config.app_dir()
            resolved = config.set_app_dir(new_path, move_existing=True)
            log.info("dashboard: data folder moved %s -> %s", old, resolved)
        except Exception:
            log.exception("dashboard: folder move failed")
    _push_state()


_ACTIONS = {
    "set_provider": _act_set_provider,
    "set_model": _act_set_model,
    "set_api_key": _act_set_api_key,
    "test_llm": _act_test_llm,
    "set_mic": _act_set_mic,
    "set_visualization": _act_set_visualization,
    "set_autostart": _act_set_autostart,
    "save_vocab": _act_save_vocab,
    "set_preset": _act_set_preset,
    "save_custom_preset": _act_save_custom_preset,
    "add_custom_preset": _act_add_custom_preset,
    "update_custom_preset": _act_update_custom_preset,
    "delete_custom_preset": _act_delete_custom_preset,
    "pick_folder": _act_pick_folder,
    "open_meeting": _act_open_meeting,
    "record_meeting": _act_record_meeting,
    "resummarize_meeting": _act_resummarize_meeting,
    "import_transcript": _act_import_transcript,
    "close_panel": _act_close_panel,
    "fetch_models": _act_fetch_models,
    "set_custom_url": _act_set_custom_url,
    "request_screen_recording": _act_request_screen_recording,
    "open_screen_recording_settings": _act_open_screen_recording_settings,
}


def _call_js(fn_name, payload):
    if _webview is None or _bridge is None:
        return
    js = f"window.{fn_name} && window.{fn_name}({json.dumps(payload)});"
    from Foundation import NSThread
    if NSThread.isMainThread():
        _webview.evaluateJavaScript_completionHandler_(js, None)
    else:
        _bridge.performSelectorOnMainThread_withObject_waitUntilDone_(
            "callJS:", js, False)


def _push_state():
    _call_js("onState", _state_snapshot())


# -- HTML / CSS / JS for the dashboard --------------------------------------
# The UI lives in real files (dashboard_ui/style.css, app.js); this module
# only assembles the skeleton and injects the boot data.

_UI_DIR = Path(__file__).resolve().parent / "dashboard_ui"


def _ui_asset(name):
    return (_UI_DIR / name).read_text()


_BODY = """
<aside class="side">
  <div class="side-top">
    <div class="search">&#128269; <input id="side-search" type="text"
         placeholder="Search everything&#8230;"></div>
    <button class="close-app-btn" onclick="send('close_panel', null)"
            title="Close">&#10005;</button>
  </div>
  <button class="record-btn" onclick="recordMeeting()">
    <span class="dot"></span> <span id="record-btn-label">Record a meeting</span>
  </button>

  <div class="nav-item active" data-view="meetings"><span class="ico">&#128203;</span> Meetings</div>
  <div class="nav-item" data-view="dictation"><span class="ico">&#127908;</span> Dictation</div>
  <div class="nav-item" data-view="import"><span class="ico">&#128229;</span> Import</div>
  <div class="nav-item" data-view="settings"><span class="ico">&#9881;&#65039;</span> Settings</div>

  <div class="group-label">RECENT</div>
  <div class="meeting-list" id="side-list"></div>
  <div class="side-foot">&#127908; MyWhisper &#8212; everything on your Mac</div>
</aside>

<main class="main">
  <div class="card">

    <div class="view active" id="view-meetings"></div>

    <div class="view" id="view-dictation">
      <div class="view-h1">Dictation history</div>
      <div class="view-sub">Your recent dictations &#8212; click Copy to reuse one.</div>
      <div id="dict-list"></div>
    </div>

    <div class="view" id="view-import">
      <div class="view-h1">Import a transcript</div>
      <div class="view-sub">Paste text from your iPhone's Voice Memos, a Teams
        transcript, or rough notes &#8212; MyWhisper summarizes it and files it
        with your other meetings.</div>
      <div class="field">
        <input type="text" id="import-title"
               placeholder="Title (optional &#8212; the AI picks one if blank)">
      </div>
      <div class="field">
        <label class="field-label">Meeting type</label>
        <select id="import-preset"></select>
      </div>
      <div class="field">
        <textarea id="import-text" style="min-height: 180px;"
                  placeholder="Paste the transcript here&#8230;"></textarea>
      </div>
      <div class="field-row">
        <button class="btn primary" id="import-btn" onclick="runImport()">Summarize &amp; Save</button>
        <span id="import-status" class="field-hint"></span>
      </div>
    </div>

    <div class="view" id="view-settings">
      <div class="view-h1">Settings</div>
      <div class="view-sub">Presets, AI backend, vocabulary, microphone, and system options.</div>

      <div class="settings-section">
        <h3>Meeting Type Presets</h3>
        <div class="section-desc">Pick the default below &#8212; you can choose a
          different one each time from the Start Meeting submenu.</div>

        <div class="preset-group-label">Built-in</div>
        <div id="builtin-list"></div>

        <div class="preset-group-label" style="margin-top: 14px;">Your Custom Presets</div>
        <div id="custom-list"></div>

        <div id="add-row" style="margin-top: 8px;">
          <button class="btn" onclick="startNewPreset()">+ Add Custom Preset</button>
          <span class="field-hint" style="margin-left: 10px;">Or use a starter:</span>
          <span id="starter-buttons"></span>
        </div>

        <div id="preset-editor" style="display: none; margin-top: 14px;
             padding: 14px; border: 1px solid var(--line); border-radius: 8px;">
          <div class="field">
            <label class="field-label">Preset Name</label>
            <input type="text" id="editor-label" placeholder="e.g. Board Meeting, Vendor Call">
          </div>
          <div class="field">
            <label class="field-label">What should the AI focus on?</label>
            <textarea id="editor-focus" placeholder="e.g. budget figures, vendor commitments, regulatory mentions, and any deadlines with owners"></textarea>
            <div class="field-hint">Plain English &#8212; describe what matters most. The AI will lean into this when writing the summary.</div>
          </div>
          <div class="field-row">
            <button class="btn primary" onclick="savePresetEdit()" id="editor-save-btn">Save</button>
            <button class="btn" onclick="cancelPresetEdit()">Cancel</button>
            <span id="editor-status" class="field-hint"></span>
          </div>
        </div>
      </div>

      <div class="settings-section">
        <h3>LLM (for Meeting Summaries)</h3>
        <div class="section-desc">Which AI writes your meeting notes.</div>

        <div class="field">
          <label class="field-label">Provider</label>
          <select id="provider-select" onchange="send('set_provider', this.value)"></select>
        </div>

        <div class="field" id="custom-url-row" style="display: none;">
          <label class="field-label">
            Server URL <span id="url-status" class="pill-status"></span>
          </label>
          <div class="field-row">
            <input type="text" id="custom-url-input" placeholder="e.g. http://llm.local:11434">
            <button class="btn" onclick="saveCustomUrl()">Save</button>
          </div>
          <div class="field-hint">Ollama, LM Studio, llama.cpp server, etc. &#8212; MyWhisper auto-detects the API style.</div>
        </div>

        <div class="field" id="api-key-row">
          <label class="field-label" id="api-key-label">API Key <span id="key-status" class="pill-status"></span></label>
          <div class="field-row">
            <input type="password" id="api-key-input" placeholder="Paste your key&#8230;">
            <button class="btn" onclick="saveApiKey()">Save</button>
          </div>
          <div class="field-hint" id="api-key-hint">Stored securely in macOS Keychain &#8212; never written to disk in plain text.</div>
        </div>

        <div class="field">
          <label class="field-label">
            Model
            <span id="model-status" class="field-hint" style="margin-left: 6px;"></span>
          </label>
          <div class="field-row">
            <select id="model-select" onchange="onModelPicked()" style="flex: 1;">
              <option value="">Loading models&#8230;</option>
            </select>
            <button class="btn" onclick="refreshModels()" title="Reload list from provider">&#8635;</button>
          </div>
          <div class="field-hint">List comes straight from the provider. Pick "Other&#8230;" to type a model name manually.</div>
        </div>

        <div class="field" id="model-manual-row" style="display: none;">
          <label class="field-label">Custom Model ID</label>
          <div class="field-row">
            <input type="text" id="model-input" placeholder="exact model identifier">
            <button class="btn" onclick="saveModel()">Save</button>
          </div>
        </div>

        <div class="field">
          <button class="btn" onclick="testLLM()">Test Connection</button>
          <div id="test-result"></div>
        </div>
      </div>

      <div class="settings-section">
        <h3>Vocabulary</h3>
        <div class="section-desc">Custom terms &#8212; one per line. Whisper uses
          these as a hint so it gets names and abbreviations right.</div>
        <textarea id="vocab-text" placeholder="# One term per line &#8212; company names, initials, jargon."></textarea>
        <div class="field-row" style="margin-top: 8px;">
          <button class="btn primary" onclick="saveVocab()">Save Vocabulary</button>
          <span id="vocab-status" class="field-hint"></span>
        </div>
      </div>

      <div class="settings-section">
        <h3>Microphone &amp; Visualization</h3>

        <div class="field">
          <label class="field-label">Input Device</label>
          <select id="mic-select" onchange="send('set_mic', this.value)"></select>
        </div>

        <div class="field">
          <label class="field-label">Live Indicator Style</label>
          <select id="viz-select" onchange="send('set_visualization', this.value)">
            <option value="waveform">Waveform</option>
            <option value="vu_meter">VU Meter (Retro)</option>
          </select>
        </div>
      </div>

      <div class="settings-section">
        <h3>System Audio (for Meeting Recording)</h3>
        <div class="section-desc">macOS gates capture of the other side of your
          Zoom/Teams calls behind Screen Recording permission. Grant it once and
          meeting recordings include everyone's audio, not just your microphone.</div>

        <div class="field">
          <label class="field-label">
            Screen Recording <span id="sr-status" class="pill-status"></span>
          </label>
          <div class="field-row">
            <button class="btn" onclick="send('request_screen_recording', null)">Request / Test</button>
            <button class="btn" onclick="send('open_screen_recording_settings', null)">Open System Settings</button>
          </div>
          <div class="field-hint" id="sr-hint"></div>
        </div>
      </div>

      <div class="settings-section">
        <h3>System</h3>
        <label class="toggle">
          <input type="checkbox" id="autostart-toggle" onchange="send('set_autostart', this.checked)">
          <span class="toggle-label">Start MyWhisper automatically at login</span>
        </label>
      </div>

      <div class="settings-section">
        <h3>Data Folder</h3>
        <div class="section-desc">Where recordings, transcripts, and your config
          are stored. Set once.</div>
        <div class="field-row">
          <div class="folder-display" id="folder-display"></div>
          <button class="btn" onclick="pickFolder()">Change&#8230;</button>
        </div>
        <div class="field-hint">Changing the folder copies your existing files to the new spot.</div>
      </div>

    </div>

  </div>
</main>
"""


def _build_html():
    meetings = _meeting_files()
    dictations = dictation_log.recent()
    state = _state_snapshot()
    # "</" must be escaped or a literal "</script>" inside a transcript
    # would terminate the script block.
    boot = json.dumps({
        "state": state,
        "meetings": meetings,
        "dictations": dictations,
    }).replace("</", "<\\/")
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        "<title>MyWhisper</title>\n<style>\n" + _ui_asset("style.css")
        + "\n</style>\n</head>\n<body>\n" + _BODY
        + "\n<script>\nwindow.BOOT = " + boot + ";\n"
        + _ui_asset("app.js") + "\n</script>\n</body>\n</html>"
    )


# -- Panel creation ----------------------------------------------------------

def _create_panel():
    global _panel, _webview, _bridge

    screen = NSScreen.mainScreen().frame()
    x = screen.size.width - _PANEL_W - 12
    y = screen.size.height - _PANEL_H - 36

    style = (
        NSWindowStyleMaskTitled
        | NSWindowStyleMaskResizable
        | NSWindowStyleMaskUtilityWindow
        | NSWindowStyleMaskFullSizeContentView
    )
    rect = NSMakeRect(x, y, _PANEL_W, _PANEL_H)
    _panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, style, NSBackingStoreBuffered, False,
    )
    _panel.setTitle_("MyWhisper")
    _panel.setTitleVisibility_(1)  # NSWindowTitleVisibilityHidden
    _panel.setTitlebarAppearsTransparent_(True)
    _panel.setLevel_(NSFloatingWindowLevel)
    _panel.setBecomesKeyOnlyIfNeeded_(False)
    _panel.setHidesOnDeactivate_(False)
    _panel.setBackgroundColor_(NSColor.colorWithRed_green_blue_alpha_(
        0.102, 0.102, 0.18, 1.0
    ))
    _panel.setMinSize_(NSMakeSize(840, 560))

    # Hide the traffic-light buttons; we draw our own close button in HTML.
    for btn_index in (0, 1, 2):   # close, miniaturize, zoom
        btn = _panel.standardWindowButton_(btn_index)
        if btn is not None:
            btn.setHidden_(True)

    # Configure WKWebView with a script message handler so JS can call us.
    wk_config = WKWebViewConfiguration.alloc().init()
    controller = WKUserContentController.alloc().init()
    _bridge = _BridgeHandler.alloc().init()
    controller.addScriptMessageHandler_name_(_bridge, "bridge")
    wk_config.setUserContentController_(controller)

    content_rect = _panel.contentView().bounds()
    _webview = WKWebView.alloc().initWithFrame_configuration_(
        content_rect, wk_config,
    )
    _webview.setAutoresizingMask_(0x12)  # flexible width + height
    _panel.contentView().addSubview_(_webview)


def _load_content():
    html_str = _build_html()
    _webview.loadHTMLString_baseURL_(html_str, None)


def refresh_if_open():
    """Reload the dashboard content if the panel is currently showing —
    called after a meeting finishes so the new file appears right away."""
    try:
        if _panel is not None and _panel.isVisible():
            _load_content()
            log.info("dashboard: refreshed after meeting")
    except Exception:
        log.exception("dashboard: refresh failed")


def open_dashboard():
    """Toggle the dashboard panel — show it (refreshed) or hide it."""
    global _panel, _webview
    try:
        if _panel is not None and _panel.isVisible():
            _panel.orderOut_(None)
            return

        if _panel is None:
            _create_panel()

        _load_content()
        _panel.makeKeyAndOrderFront_(None)
        try:
            from AppKit import NSApplication
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        except Exception:
            pass
        log.info("dashboard: opened")
    except Exception:
        log.exception("dashboard: failed to open")
