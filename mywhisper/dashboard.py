"""Native floating dashboard window — meetings, dictation history, settings.

Built with NSPanel + WKWebView via PyObjC. A two-way bridge lets the HTML UI
read and write settings through a tiny Python message handler.
"""

import html
import json
import logging
import os
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

from . import config, dictation_log, autostart, vocab, recorder, screen_recording

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
_PANEL_W = 620
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
    for f in files[:50]:
        try:
            raw = f.read_text()
            name = f.stem.replace("meeting_", "").replace("_", " @ ")
            meetings.append({"name": name, "filename": f.name, "content": raw})
        except Exception:
            pass
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
    from . import llm
    ok, msg = llm.test_connection()
    _call_js("onTestResult", {"ok": bool(ok), "message": str(msg)})


def _act_fetch_models(body):
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
    screen_recording.request_permission()
    _push_state()


def _act_open_screen_recording_settings(body):
    screen_recording.open_settings()


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
    "close_panel": _act_close_panel,
    "fetch_models": _act_fetch_models,
    "set_custom_url": _act_set_custom_url,
    "request_screen_recording": _act_request_screen_recording,
    "open_screen_recording_settings": _act_open_screen_recording_settings,
}


def _call_js(fn_name, payload):
    if _webview is None:
        return
    js = f"window.{fn_name} && window.{fn_name}({json.dumps(payload)});"
    _webview.evaluateJavaScript_completionHandler_(js, None)


def _push_state():
    _call_js("onState", _state_snapshot())


# -- HTML / CSS / JS for the dashboard --------------------------------------

def _build_html():
    meetings = _meeting_files()
    dictations = dictation_log.recent()
    state = _state_snapshot()

    meetings_html = ""
    if not meetings:
        meetings_html = '<p class="empty">No meetings recorded yet.</p>'
    else:
        for i, m in enumerate(meetings):
            safe_content = html.escape(m["content"])
            meetings_html += f"""
            <div class="card">
                <div class="card-header" onclick="toggleMeeting({i})">
                    <span class="card-title">{html.escape(m['name'])}</span>
                    <span class="card-file">{html.escape(m['filename'])}</span>
                    <button class="copy-btn" onclick="copyMeeting({i}, event)">Copy</button>
                    <span class="chevron" id="chev-{i}">&#9654;</span>
                </div>
                <div class="card-body" id="meeting-{i}">
                    <div class="card-body-toolbar">
                        <button class="copy-btn" onclick="copyMeeting({i}, event)">Copy</button>
                        <button class="close-btn-inline" onclick="toggleMeeting({i}); event.stopPropagation();">&times; Close</button>
                    </div>
                    <pre class="card-body-content">{safe_content}</pre>
                </div>
            </div>"""

    dictations_html = ""
    if not dictations:
        dictations_html = '<p class="empty">No dictations yet. Use push-to-talk and they\'ll appear here.</p>'
    else:
        for i, d in enumerate(dictations):
            safe_text = html.escape(d["text"])
            dictations_html += f"""
            <div class="card">
                <div class="card-header">
                    <span class="card-title">{html.escape(d['timestamp'])}</span>
                    <button class="copy-btn" onclick="copyText({i}, event)">Copy</button>
                </div>
                <div class="dict-text" id="dict-{i}">{safe_text}</div>
            </div>"""

    now = datetime.now().strftime("%I:%M %p")
    state_json = json.dumps(state)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MyWhisper</title>
<style>
    :root {{
        --bg: #1a1a2e;
        --surface: #222244;
        --surface2: #2a2a4a;
        --surface3: #303050;
        --accent: #e94560;
        --accent2: #0f3460;
        --text: #eee;
        --text2: #aaa;
        --text3: #777;
        --green: #4ecca3;
        --red: #e94560;
        --radius: 10px;
    }}
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
        font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
        background: var(--bg);
        color: var(--text);
        -webkit-user-select: none;
        user-select: none;
    }}
    .header {{
        background: linear-gradient(135deg, var(--accent2), #16213e);
        padding: 18px 20px 12px;
        border-bottom: 2px solid var(--accent);
        -webkit-app-region: drag;
        position: relative;
    }}
    .close-btn {{
        position: absolute;
        top: 12px;
        right: 14px;
        width: 26px;
        height: 26px;
        border: none;
        background: rgba(255, 255, 255, 0.10);
        color: var(--text);
        font-size: 13px;
        font-weight: 600;
        line-height: 1;
        border-radius: 13px;
        cursor: pointer;
        -webkit-app-region: no-drag;
        transition: all 0.15s;
    }}
    .close-btn:hover {{
        background: var(--accent);
        color: white;
    }}
    .header h1 {{ font-size: 18px; font-weight: 600; }}
    .header h1 span {{ color: var(--accent); }}
    .header .updated {{
        font-size: 11px;
        color: var(--text2);
        margin-top: 3px;
    }}
    .tabs {{
        display: flex;
        background: var(--surface);
        border-bottom: 1px solid #333;
    }}
    .tab {{
        flex: 1;
        padding: 12px 0;
        text-align: center;
        cursor: pointer;
        font-size: 13px;
        font-weight: 500;
        color: var(--text2);
        transition: all 0.2s;
        border-bottom: 3px solid transparent;
    }}
    .tab:hover {{ color: var(--text); background: var(--surface2); }}
    .tab.active {{
        color: var(--accent);
        border-bottom-color: var(--accent);
    }}
    .tab-count {{
        display: inline-block;
        background: var(--accent2);
        color: var(--text2);
        font-size: 10px;
        padding: 1px 6px;
        border-radius: 8px;
        margin-left: 5px;
    }}
    .tab.active .tab-count {{
        background: var(--accent);
        color: white;
    }}
    .scroll-area {{
        overflow-y: auto;
        max-height: calc(100vh - 110px);
        padding: 14px 16px 24px;
    }}
    .content {{ display: none; }}
    .content.active {{ display: block; }}
    .card {{
        background: var(--surface);
        border-radius: var(--radius);
        margin-bottom: 10px;
        overflow: hidden;
        border: 1px solid #333;
    }}
    .card:hover {{ border-color: #555; }}
    .card-header {{
        display: flex;
        align-items: center;
        padding: 12px 14px;
        cursor: pointer;
        gap: 10px;
    }}
    .card-title {{ font-weight: 500; font-size: 13px; flex-shrink: 0; }}
    .card-file {{
        color: var(--text2);
        font-size: 11px;
        flex: 1;
        text-align: right;
    }}
    .chevron {{
        color: var(--text2);
        font-size: 11px;
        transition: transform 0.2s;
    }}
    .chevron.open {{ transform: rotate(90deg); }}
    .card-body {{
        display: none;
        max-height: 380px;
        overflow-y: auto;
        position: relative;
    }}
    .card-body.open {{ display: block; }}
    .card-body-toolbar {{
        position: sticky;
        top: 0;
        z-index: 2;
        display: flex;
        gap: 6px;
        justify-content: flex-end;
        padding: 8px 14px;
        background: linear-gradient(to bottom,
            var(--surface) 0%,
            var(--surface) 85%,
            rgba(34, 34, 68, 0) 100%);
        border-bottom: 1px solid rgba(255, 255, 255, 0.05);
    }}
    .card-body-content {{
        padding: 4px 14px 14px;
        margin: 0;
        font-size: 12px;
        line-height: 1.6;
        color: var(--text2);
        white-space: pre-wrap;
        word-wrap: break-word;
        font-family: inherit;
        -webkit-user-select: text;
        user-select: text;
    }}
    .close-btn-inline {{
        background: transparent;
        color: var(--text2);
        border: 1px solid #555;
        padding: 5px 11px;
        border-radius: 6px;
        font-size: 11px;
        cursor: pointer;
        transition: all 0.15s;
    }}
    .close-btn-inline:hover {{
        background: var(--surface2);
        color: var(--text);
        border-color: #777;
    }}
    .dict-text {{
        padding: 0 14px 12px;
        font-size: 13px;
        line-height: 1.5;
        -webkit-user-select: text;
        user-select: text;
    }}
    .copy-btn, .btn {{
        background: var(--accent2);
        color: var(--text);
        border: 1px solid #444;
        padding: 6px 14px;
        border-radius: 6px;
        font-size: 12px;
        cursor: pointer;
        transition: all 0.15s;
        flex-shrink: 0;
    }}
    .btn:hover, .copy-btn:hover {{
        background: var(--accent);
        border-color: var(--accent);
    }}
    .copy-btn.copied {{
        background: var(--green);
        border-color: var(--green);
        color: #111;
    }}
    .empty {{
        text-align: center;
        color: var(--text2);
        padding: 50px 16px;
        font-size: 14px;
    }}

    /* ----- Settings ----- */
    .settings-section {{
        background: var(--surface);
        border-radius: var(--radius);
        padding: 16px 18px;
        margin-bottom: 14px;
        border: 1px solid #333;
    }}
    .settings-section h3 {{
        font-size: 13px;
        font-weight: 600;
        color: var(--accent);
        margin-bottom: 4px;
        text-transform: uppercase;
        letter-spacing: 0.6px;
    }}
    .settings-section .section-desc {{
        font-size: 11px;
        color: var(--text3);
        margin-bottom: 14px;
    }}
    .field {{ margin-bottom: 14px; }}
    .field:last-child {{ margin-bottom: 0; }}
    .field-label {{
        display: block;
        font-size: 12px;
        color: var(--text2);
        margin-bottom: 6px;
        font-weight: 500;
    }}
    .field-hint {{
        font-size: 10px;
        color: var(--text3);
        margin-top: 4px;
    }}
    .field-row {{
        display: flex;
        gap: 8px;
        align-items: center;
    }}
    input[type=text], input[type=password], select, textarea {{
        background: var(--bg);
        color: var(--text);
        border: 1px solid #444;
        padding: 7px 10px;
        border-radius: 6px;
        font-size: 12px;
        font-family: inherit;
        width: 100%;
        outline: none;
    }}
    input[type=text]:focus, input[type=password]:focus,
    select:focus, textarea:focus {{
        border-color: var(--accent);
    }}
    select {{
        cursor: pointer;
        appearance: none;
        -webkit-appearance: none;
        background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 6'><polygon points='0,0 10,0 5,6' fill='%23aaa'/></svg>");
        background-repeat: no-repeat;
        background-position: right 10px center;
        background-size: 10px 6px;
        padding-right: 28px;
    }}
    textarea {{
        min-height: 110px;
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 11px;
        line-height: 1.5;
        resize: vertical;
    }}
    .folder-display {{
        background: var(--bg);
        border: 1px solid #444;
        padding: 7px 10px;
        border-radius: 6px;
        font-size: 11px;
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        color: var(--text2);
        flex: 1;
        overflow-x: auto;
        white-space: nowrap;
    }}
    .toggle {{
        display: flex;
        align-items: center;
        gap: 8px;
        cursor: pointer;
    }}
    .toggle input {{ accent-color: var(--accent); }}
    .toggle-label {{ font-size: 12px; color: var(--text); }}
    .pill-status {{
        display: inline-block;
        padding: 2px 8px;
        border-radius: 10px;
        font-size: 10px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.4px;
        margin-left: 6px;
    }}
    .pill-status.ok {{ background: var(--green); color: #111; }}
    .pill-status.missing {{ background: var(--red); color: white; }}
    #test-result {{
        font-size: 11px;
        margin-top: 8px;
        min-height: 16px;
    }}
    #test-result.ok {{ color: var(--green); }}
    #test-result.err {{ color: var(--red); }}
    .preset-group-label {{
        font-size: 10px;
        text-transform: uppercase;
        letter-spacing: 0.6px;
        color: var(--text3);
        margin-bottom: 4px;
        font-weight: 600;
    }}
    .preset-row {{
        display: flex;
        align-items: center;
        gap: 8px;
        background: var(--bg);
        border: 1px solid #444;
        border-radius: 6px;
        padding: 8px 10px;
        margin-bottom: 4px;
        transition: all 0.15s;
    }}
    .preset-row:hover {{ border-color: #666; }}
    .preset-row.selected {{
        border-color: var(--accent);
        background: var(--surface2);
    }}
    .preset-row .preset-pick {{
        flex: 1;
        cursor: pointer;
        display: flex;
        align-items: center;
        gap: 8px;
    }}
    .preset-row .preset-radio {{
        width: 14px;
        height: 14px;
        border-radius: 7px;
        border: 1.5px solid #666;
        flex-shrink: 0;
        background: transparent;
    }}
    .preset-row.selected .preset-radio {{
        background: var(--accent);
        border-color: var(--accent);
        box-shadow: inset 0 0 0 2px var(--bg);
    }}
    .preset-row .preset-label {{
        font-size: 12px;
        font-weight: 500;
    }}
    .preset-row .preset-actions {{
        display: flex;
        gap: 4px;
    }}
    .preset-row .icon-btn {{
        background: transparent;
        color: var(--text2);
        border: 1px solid transparent;
        padding: 3px 7px;
        border-radius: 4px;
        font-size: 11px;
        cursor: pointer;
        transition: all 0.15s;
    }}
    .preset-row .icon-btn:hover {{
        background: var(--surface2);
        color: var(--text);
        border-color: #555;
    }}
    .preset-row .icon-btn.delete:hover {{
        color: var(--red);
        border-color: var(--red);
    }}
    .btn-ghost {{
        background: transparent;
        border: 1px solid #444;
    }}
    .btn-ghost:hover {{
        background: var(--surface2);
        border-color: #555;
    }}
    .starter-btn {{
        background: transparent;
        color: var(--text2);
        border: 1px dashed #555;
        padding: 4px 10px;
        border-radius: 12px;
        font-size: 11px;
        cursor: pointer;
        margin: 0 2px;
        transition: all 0.15s;
    }}
    .starter-btn:hover {{
        border-color: var(--accent);
        color: var(--text);
    }}
</style>
</head>
<body>

<div class="header">
    <button class="close-btn" onclick="send('close_panel', null)" title="Close">&#10005;</button>
    <h1>&#127908; <span>MyWhisper</span></h1>
    <div class="updated">Updated {now}</div>
</div>

<div class="tabs">
    <div class="tab active" data-tab="meetings" onclick="switchTab('meetings')">
        Meetings <span class="tab-count">{len(meetings)}</span>
    </div>
    <div class="tab" data-tab="dictation" onclick="switchTab('dictation')">
        Dictation <span class="tab-count">{len(dictations)}</span>
    </div>
    <div class="tab" data-tab="settings" onclick="switchTab('settings')">
        Settings
    </div>
</div>

<div class="scroll-area">

<div class="content active" id="meetings-content">
    {meetings_html}
</div>

<div class="content" id="dictation-content">
    {dictations_html}
</div>

<div class="content" id="settings-content">

    <!-- 1. Meeting Type Presets — most frequently changed -->
    <div class="settings-section">
        <h3>Meeting Type Presets</h3>
        <div class="section-desc">Pick the default below — you can choose a different one each time from the Start Meeting submenu.</div>

        <div class="preset-group-label">Built-in</div>
        <div id="builtin-list"></div>

        <div class="preset-group-label" style="margin-top: 14px;">Your Custom Presets</div>
        <div id="custom-list"></div>

        <div id="add-row" style="margin-top: 8px;">
            <button class="btn" onclick="startNewPreset()">+ Add Custom Preset</button>
            <span class="field-hint" style="margin-left: 10px;">Or use a starter:</span>
            <span id="starter-buttons"></span>
        </div>

        <div id="preset-editor" style="display: none; margin-top: 14px; padding: 14px; background: var(--bg); border: 1px solid #444; border-radius: 8px;">
            <div class="field">
                <label class="field-label">Preset Name</label>
                <input type="text" id="editor-label" placeholder="e.g. Board Meeting, Vendor Call">
            </div>
            <div class="field">
                <label class="field-label">What should the AI focus on?</label>
                <textarea id="editor-focus" placeholder="e.g. budget figures, vendor commitments, regulatory mentions, and any deadlines with owners"></textarea>
                <div class="field-hint">Plain English — describe what matters most. The AI will lean into this when writing the summary.</div>
            </div>
            <div class="field-row">
                <button class="btn" onclick="savePresetEdit()" id="editor-save-btn">Save</button>
                <button class="btn btn-ghost" onclick="cancelPresetEdit()">Cancel</button>
                <span id="editor-status" class="field-hint"></span>
            </div>
        </div>
    </div>

    <!-- 2. LLM — provider/key/model -->
    <div class="settings-section">
        <h3>LLM (for Meeting Summaries)</h3>
        <div class="section-desc">Which cloud model writes your meeting notes.</div>

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
            <div class="field-hint">Ollama, LM Studio, llama.cpp server, etc. — MyWhisper auto-detects the API style.</div>
        </div>

        <div class="field" id="api-key-row">
            <label class="field-label" id="api-key-label">API Key <span id="key-status" class="pill-status"></span></label>
            <div class="field-row">
                <input type="password" id="api-key-input" placeholder="Paste your key…">
                <button class="btn" onclick="saveApiKey()">Save</button>
            </div>
            <div class="field-hint" id="api-key-hint">Stored securely in macOS Keychain — never written to disk in plain text.</div>
        </div>

        <div class="field">
            <label class="field-label">
                Model
                <span id="model-status" class="field-hint" style="margin-left: 6px;"></span>
            </label>
            <div class="field-row">
                <select id="model-select" onchange="onModelPicked()" style="flex: 1;">
                    <option value="">Loading models…</option>
                </select>
                <button class="btn btn-ghost" onclick="refreshModels()" title="Reload list from provider">↻</button>
            </div>
            <div class="field-hint">List comes straight from the provider. Pick "Other…" to type a model name manually.</div>
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

    <!-- 3. Vocabulary — occasionally edited -->
    <div class="settings-section">
        <h3>Vocabulary</h3>
        <div class="section-desc">Custom terms — one per line. Whisper uses these as a hint so it gets names and abbreviations right.</div>
        <textarea id="vocab-text" placeholder="# One term per line — company names, initials, jargon."></textarea>
        <div class="field-row" style="margin-top: 8px;">
            <button class="btn" onclick="saveVocab()">Save Vocabulary</button>
            <span id="vocab-status" class="field-hint"></span>
        </div>
    </div>

    <!-- 4. Microphone & Visualization — set once usually -->
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

    <!-- 5. System Audio (for Meeting Recording) -->
    <div class="settings-section">
        <h3>System Audio (for Meeting Recording)</h3>
        <div class="section-desc">macOS gates capture of the other side of your Zoom/Teams calls behind Screen Recording permission. Grant it once and meeting recordings include everyone's audio, not just your microphone.</div>

        <div class="field">
            <label class="field-label">
                Screen Recording <span id="sr-status" class="pill-status"></span>
            </label>
            <div class="field-row">
                <button class="btn" onclick="send('request_screen_recording', null)">Request / Test</button>
                <button class="btn btn-ghost" onclick="send('open_screen_recording_settings', null)">Open System Settings</button>
            </div>
            <div class="field-hint" id="sr-hint"></div>
        </div>
    </div>

    <!-- 6. System — set once -->
    <div class="settings-section">
        <h3>System</h3>
        <label class="toggle">
            <input type="checkbox" id="autostart-toggle" onchange="send('set_autostart', this.checked)">
            <span class="toggle-label">Start MyWhisper automatically at login</span>
        </label>
    </div>

    <!-- 6. Data Folder — advanced, rarely changed -->
    <div class="settings-section">
        <h3>Data Folder</h3>
        <div class="section-desc">Where recordings, transcripts, and your config are stored. Set once.</div>
        <div class="field-row">
            <div class="folder-display" id="folder-display"></div>
            <button class="btn" onclick="pickFolder()">Change…</button>
        </div>
        <div class="field-hint">Changing the folder copies your existing files to the new spot.</div>
    </div>

</div>

</div>

<script>
const initialState = {state_json};

function $(id) {{ return document.getElementById(id); }}

function send(action, value) {{
    if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.bridge) {{
        window.webkit.messageHandlers.bridge.postMessage({{ action: action, value: value }});
    }}
}}

function switchTab(name) {{
    document.querySelectorAll('.tab').forEach(t => {{
        t.classList.toggle('active', t.dataset.tab === name);
    }});
    document.querySelectorAll('.content').forEach(c => {{
        c.classList.toggle('active', c.id === name + '-content');
    }});
}}

function toggleMeeting(i) {{
    $('meeting-' + i).classList.toggle('open');
    $('chev-' + i).classList.toggle('open');
}}

function copyText(i, event) {{
    event.stopPropagation();
    const text = $('dict-' + i).innerText;
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    const btn = event.target;
    btn.textContent = 'Copied!';
    btn.classList.add('copied');
    setTimeout(() => {{ btn.textContent = 'Copy'; btn.classList.remove('copied'); }}, 1500);
}}

function copyMeeting(i, event) {{
    event.stopPropagation();   // don't toggle the card open/closed
    const text = $('meeting-' + i).innerText;
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    const btn = event.target;
    btn.textContent = 'Copied!';
    btn.classList.add('copied');
    setTimeout(() => {{ btn.textContent = 'Copy'; btn.classList.remove('copied'); }}, 1500);
}}

function saveApiKey() {{
    const v = $('api-key-input').value.trim();
    if (v && !v.includes('•')) send('set_api_key', v);
    $('api-key-input').value = '';
}}

function saveCustomUrl() {{
    send('set_custom_url', $('custom-url-input').value.trim());
}}

function saveModel() {{
    send('set_model', $('model-input').value.trim());
}}

function saveVocab() {{
    send('save_vocab', $('vocab-text').value);
    const s = $('vocab-status');
    s.textContent = 'Saved.';
    setTimeout(() => s.textContent = '', 2000);
}}

function pickFolder() {{ send('pick_folder', null); }}

function testLLM() {{
    const r = $('test-result');
    r.className = '';
    r.textContent = 'Testing…';
    send('test_llm', null);
}}

window.onTestResult = function(payload) {{
    const r = $('test-result');
    if (payload.ok) {{
        r.className = 'ok';
        r.textContent = '✓ Connected to ' + payload.message;
    }} else {{
        r.className = 'err';
        r.textContent = '✗ ' + payload.message;
    }}
}};

window.onState = function(state) {{ renderState(state); }};

function renderState(s) {{
    $('folder-display').textContent = s.data_dir;

    // Provider dropdown
    const psel = $('provider-select');
    psel.innerHTML = '';
    s.llm_providers.forEach(p => {{
        const o = document.createElement('option');
        o.value = p.id;
        o.textContent = p.label;
        if (p.id === s.llm_provider) o.selected = true;
        psel.appendChild(o);
    }});

    // Provider-specific rows: URL shows only for providers that use one;
    // the key row shows whenever the provider has a key_name (required
    // OR optional).
    $('api-key-row').style.display = s.llm_needs_key ? 'block' : 'none';
    $('custom-url-row').style.display = s.llm_needs_url ? 'block' : 'none';

    // URL status
    if (s.llm_needs_url) {{
        $('custom-url-input').value = s.custom_llm_url || '';
        const urlPill = $('url-status');
        if (s.custom_llm_url) {{
            urlPill.className = 'pill-status ok';
            urlPill.textContent = 'Set';
        }} else {{
            urlPill.className = 'pill-status missing';
            urlPill.textContent = 'Not set';
        }}
    }}

    // API key field label & status pill
    const keyLabel = $('api-key-label');
    const pill = $('key-status');
    const keyHint = $('api-key-hint');
    if (s.llm_key_optional) {{
        keyLabel.firstChild.textContent = 'Auth Token (optional) ';
        keyHint.textContent = 'Most local LLM servers do not need this. Set only if your server requires Bearer authentication.';
        if (s.api_key_set) {{
            pill.className = 'pill-status ok';
            pill.textContent = 'Set';
            $('api-key-input').placeholder = s.api_key_masked || 'Saved';
        }} else {{
            pill.className = '';
            pill.textContent = '';
            $('api-key-input').placeholder = 'No auth needed for most servers';
        }}
    }} else {{
        keyLabel.firstChild.textContent = 'API Key ';
        keyHint.textContent = 'Stored securely in macOS Keychain — never written to disk in plain text.';
        if (s.api_key_set) {{
            pill.className = 'pill-status ok';
            pill.textContent = 'Set';
            $('api-key-input').placeholder = s.api_key_masked || 'Saved — paste a new key to replace';
        }} else {{
            pill.className = 'pill-status missing';
            pill.textContent = 'Not set';
            $('api-key-input').placeholder = 'Paste your key…';
        }}
    }}

    // Model dropdown is populated asynchronously by refreshModels();
    // keep the manual text field in sync with the current saved model.
    $('model-input').value = s.llm_model || '';
    syncModelDropdown(s.llm_model || '');

    // Mic dropdown
    const msel = $('mic-select');
    msel.innerHTML = '';
    const def = document.createElement('option');
    def.value = '';
    def.textContent = 'System Default';
    if (!s.mic) def.selected = true;
    msel.appendChild(def);
    s.mic_devices.forEach(name => {{
        const o = document.createElement('option');
        o.value = name;
        o.textContent = name;
        if (name === s.mic) o.selected = true;
        msel.appendChild(o);
    }});

    $('viz-select').value = s.visualization;
    $('autostart-toggle').checked = s.autostart;

    // Screen Recording permission status
    const srPill = $('sr-status');
    const srHint = $('sr-hint');
    if (s.screen_recording_granted) {{
        srPill.className = 'pill-status ok';
        srPill.textContent = 'Granted';
        srHint.textContent = 'Both sides of your calls will be captured. ✓';
    }} else {{
        srPill.className = 'pill-status missing';
        srPill.textContent = 'Not granted';
        srHint.textContent = 'Click Request/Test to trigger the macOS prompt. If you previously denied, that prompt won\\'t show again — use Open System Settings to flip it on manually (under Screen Recording → python3.12).';
    }}
    $('vocab-text').value = s.vocabulary;

    // Built-in presets — picker only (no edit/delete)
    const bi = $('builtin-list');
    bi.innerHTML = '';
    s.builtin_presets.forEach(p => renderPresetRow(bi, p, s.meeting_preset, false));

    // Custom presets — picker + edit + delete
    const cu = $('custom-list');
    cu.innerHTML = '';
    if (!s.custom_presets || s.custom_presets.length === 0) {{
        const empty = document.createElement('div');
        empty.className = 'field-hint';
        empty.style.padding = '8px 0';
        empty.textContent = 'No custom presets yet. Add one below.';
        cu.appendChild(empty);
    }} else {{
        s.custom_presets.forEach(p => renderPresetRow(cu, p, s.meeting_preset, true));
    }}

    // Starter quick-add buttons
    const sb = $('starter-buttons');
    sb.innerHTML = '';
    (s.starter_presets || []).forEach(starter => {{
        const btn = document.createElement('button');
        btn.className = 'starter-btn';
        btn.textContent = '+ ' + starter.label;
        btn.onclick = () => send('add_custom_preset', {{
            label: starter.label, focus: starter.focus
        }});
        sb.appendChild(btn);
    }});
}}

function renderPresetRow(container, preset, selectedId, editable) {{
    const row = document.createElement('div');
    row.className = 'preset-row' + (preset.id === selectedId ? ' selected' : '');

    const pick = document.createElement('div');
    pick.className = 'preset-pick';
    pick.innerHTML = '<div class="preset-radio"></div>' +
                     '<div class="preset-label">' + escapeHtml(preset.label) + '</div>';
    pick.onclick = () => send('set_preset', preset.id);
    row.appendChild(pick);

    if (editable) {{
        const actions = document.createElement('div');
        actions.className = 'preset-actions';
        const editBtn = document.createElement('button');
        editBtn.className = 'icon-btn';
        editBtn.textContent = 'Edit';
        editBtn.onclick = (e) => {{ e.stopPropagation(); openEditPreset(preset); }};
        const delBtn = document.createElement('button');
        delBtn.className = 'icon-btn delete';
        delBtn.textContent = 'Delete';
        delBtn.onclick = (e) => {{
            e.stopPropagation();
            if (confirm('Delete preset "' + preset.label + '"?')) {{
                send('delete_custom_preset', {{ id: preset.id }});
            }}
        }};
        actions.appendChild(editBtn);
        actions.appendChild(delBtn);
        row.appendChild(actions);
    }}
    container.appendChild(row);
}}

function escapeHtml(s) {{
    return String(s || '').replace(/[&<>\"']/g, c => ({{
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }}[c]));
}}

let _editingId = null;

function startNewPreset() {{
    _editingId = null;
    $('editor-label').value = '';
    $('editor-focus').value = '';
    $('editor-save-btn').textContent = 'Add Preset';
    $('preset-editor').style.display = 'block';
    $('editor-label').focus();
}}

function openEditPreset(preset) {{
    _editingId = preset.id;
    $('editor-label').value = preset.label;
    $('editor-focus').value = preset.focus;
    $('editor-save-btn').textContent = 'Save Changes';
    $('preset-editor').style.display = 'block';
    $('editor-label').focus();
}}

function cancelPresetEdit() {{
    _editingId = null;
    $('preset-editor').style.display = 'none';
}}

function savePresetEdit() {{
    const label = $('editor-label').value;
    const focus = $('editor-focus').value;
    if (_editingId) {{
        send('update_custom_preset', {{ id: _editingId, label: label, focus: focus }});
    }} else {{
        send('add_custom_preset', {{ label: label, focus: focus }});
    }}
    _editingId = null;
    $('preset-editor').style.display = 'none';
}}

// --- Model dropdown -----------------------------------------------------

let _modelList = [];      // last-fetched list of id/label objects
let _lastProvider = '';   // refetch when this changes

function syncModelDropdown(currentId) {{
    const sel = $('model-select');
    sel.innerHTML = '';
    if (_modelList.length === 0) {{
        const opt = document.createElement('option');
        opt.value = currentId || '';
        opt.textContent = currentId ? currentId + ' (saved)' : 'Loading models…';
        sel.appendChild(opt);
    }} else {{
        let matched = false;
        _modelList.forEach(m => {{
            const opt = document.createElement('option');
            opt.value = m.id;
            opt.textContent = m.label;
            if (m.id === currentId) {{ opt.selected = true; matched = true; }}
            sel.appendChild(opt);
        }});
        // If the saved model isn't in the fetched list, show it as a
        // disabled item at the top so the user can see what's saved.
        if (currentId && !matched) {{
            const opt = document.createElement('option');
            opt.value = currentId;
            opt.textContent = currentId + ' (saved, not in current list)';
            opt.selected = true;
            sel.insertBefore(opt, sel.firstChild);
        }}
        const other = document.createElement('option');
        other.value = '__other__';
        other.textContent = 'Other… (type a model name)';
        sel.appendChild(other);
    }}
    // Show manual row if Other is selected
    $('model-manual-row').style.display =
        (sel.value === '__other__') ? 'block' : 'none';
}}

function refreshModels() {{
    $('model-status').textContent = 'Loading…';
    send('fetch_models', null);
}}

window.onModelsLoaded = function(payload) {{
    if (payload.ok) {{
        _modelList = payload.models || [];
        $('model-status').textContent = _modelList.length + ' available';
        const currentModel = $('model-input').value;
        // If nothing is saved yet, or the saved one isn't in this server's
        // list, auto-pick the first model so Test Connection just works.
        const matched = _modelList.some(m => m.id === currentModel);
        if (_modelList.length > 0 && (!currentModel || !matched)) {{
            const auto = _modelList[0].id;
            $('model-input').value = auto;
            send('set_model', auto);
            syncModelDropdown(auto);
        }} else {{
            syncModelDropdown(currentModel);
        }}
    }} else {{
        _modelList = [];
        $('model-status').textContent = '⚠ ' + (payload.error || 'load failed');
        syncModelDropdown($('model-input').value);
    }}
}};

function onModelPicked() {{
    const sel = $('model-select');
    const v = sel.value;
    if (v === '__other__') {{
        $('model-manual-row').style.display = 'block';
        $('model-input').focus();
        return;
    }}
    $('model-manual-row').style.display = 'none';
    if (v) {{
        $('model-input').value = v;
        send('set_model', v);
    }}
}}

renderState(initialState);

// Fetch models in the background on first open. Also refetch whenever
// the provider dropdown changes (provider is in the state pushed back
// after set_provider).
refreshModels();

(function() {{
    _lastProvider = initialState.llm_provider;
    const origOnState = window.onState;
    window.onState = function(state) {{
        if (state.llm_provider !== _lastProvider) {{
            _lastProvider = state.llm_provider;
            _modelList = [];   // clear stale list before refetching
            refreshModels();
        }}
        origOnState(state);
    }};
}})();
</script>
</body>
</html>"""


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
    _panel.setMinSize_(NSMakeSize(480, 480))

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
