"""Inspect and request macOS Screen Recording permission.

macOS gates system-audio capture (via ScreenCaptureKit) behind the same
permission as Screen Recording — that's just how Apple grouped it.
We use Quartz's CGPreflightScreenCaptureAccess (read) and
CGRequestScreenCaptureAccess (prompt) so we can show the user a clean
status and offer a Test button instead of silently failing during a
meeting.
"""

import logging
import subprocess

log = logging.getLogger("mywhisper")


def has_permission():
    """Return True if Screen Recording permission is granted."""
    try:
        import Quartz
        return bool(Quartz.CGPreflightScreenCaptureAccess())
    except Exception:
        log.exception("screen recording: preflight failed")
        return False


def request_permission():
    """Trigger the macOS permission prompt for Screen Recording.

    If the user has never been prompted, this shows the dialog. If they
    previously denied, macOS instead opens System Settings to the right
    pane (since the prompt can only be shown once).
    Non-blocking — the user's decision is reflected in has_permission()
    on subsequent calls.
    """
    try:
        import Quartz
        Quartz.CGRequestScreenCaptureAccess()
        log.info("screen recording: request_access invoked")
    except Exception:
        log.exception("screen recording: request_access failed")


def open_settings():
    """Open the Screen Recording pane in System Settings."""
    try:
        subprocess.run(
            ["open",
             "x-apple.systempreferences:com.apple.preference.security?"
             "Privacy_ScreenCapture"],
            check=False,
        )
    except Exception:
        log.exception("screen recording: open settings failed")


def status_label():
    return "Granted" if has_permission() else "Not granted"
