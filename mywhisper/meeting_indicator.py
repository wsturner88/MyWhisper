"""Floating recording / processing indicator.

Two modes for the same panel:
  - **recording**: pulsing red dot, REC label, live elapsed timer,
    big red "Stop" pill on the right. Clicking the panel stops the
    meeting.
  - **processing**: amber dot, "WORKING" label, stage text from the
    summarizer ("Summarizing part 2 of 5 — 247 chars"). The Stop pill
    is hidden — the meeting is already done; we're just waiting for
    the LLM. After 60s with no progress update we switch to a stall
    warning so the user knows something's wrong.
"""

import logging
import math
import time

log = logging.getLogger("mywhisper")

STALL_AFTER = 60.0  # seconds without a progress update == probably stuck


class MeetingIndicator:
    def __init__(self, on_stop):
        self.on_stop = on_stop  # called when the panel is clicked in REC mode
        self._window = None
        self._view = None
        self._start_time = None
        self._mode = "recording"
        self._status = ""
        self._last_progress = 0.0
        self._disabled = False

    # ------------------------------------------------------------------
    def _build(self):
        import objc
        from AppKit import (
            NSPanel, NSColor, NSView, NSScreen, NSBackingStoreBuffered,
            NSFont, NSFontAttributeName, NSForegroundColorAttributeName,
            NSBezierPath, NSCursor,
        )
        from Foundation import NSMakeRect, NSString

        WIDTH, HEIGHT = 380, 56
        STYLE_MASK = 1 << 7   # NSWindowStyleMaskNonactivatingPanel

        rect = NSMakeRect(0, 0, WIDTH, HEIGHT)
        window = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, STYLE_MASK, NSBackingStoreBuffered, False)
        window.setOpaque_(False)
        window.setBackgroundColor_(NSColor.clearColor())
        window.setLevel_(25)
        window.setIgnoresMouseEvents_(False)
        window.setHasShadow_(True)
        window.setBecomesKeyOnlyIfNeeded_(True)
        window.setHidesOnDeactivate_(False)

        outer = self  # for callbacks reaching back to the host

        class IndicatorView(NSView):
            def initWithFrame_(self, frame):
                v = objc.super(IndicatorView, self).initWithFrame_(frame)
                if v is not None:
                    v._timer_text = "00:00"
                    v._pulse = 1.0
                    v._mode = "recording"
                    v._status = ""
                    v._stalled = False
                return v

            # ---- Public setters --------------------------------------
            def setTimer_(self, text):
                self._timer_text = text
                self.setNeedsDisplay_(True)

            def setPulse_(self, p):
                self._pulse = float(p)
                self.setNeedsDisplay_(True)

            def setMode_(self, mode):
                self._mode = mode
                self.setNeedsDisplay_(True)

            def setStatus_(self, status):
                self._status = status
                self.setNeedsDisplay_(True)

            def setStalled_(self, stalled):
                self._stalled = bool(stalled)
                self.setNeedsDisplay_(True)

            # ---- Mouse handling --------------------------------------
            def acceptsFirstMouse_(self, event):
                return True

            def mouseDown_(self, event):
                # Only the recording mode is click-to-stop. In processing
                # mode the meeting is already over; clicks do nothing.
                if self._mode != "recording":
                    return
                try:
                    if outer.on_stop:
                        outer.on_stop()
                except Exception:
                    log.exception("meeting indicator: click handler failed")

            def resetCursorRects(self):
                if self._mode == "recording":
                    self.addCursorRect_cursor_(
                        self.bounds(), NSCursor.pointingHandCursor())

            # ---- Drawing ---------------------------------------------
            def drawRect_(self, dirty):
                try:
                    bounds = self.bounds()
                    w = float(bounds.size.width)
                    h = float(bounds.size.height)
                    cy = h / 2.0

                    # Background — slightly different border per mode.
                    if self._mode == "recording":
                        accent = (0.91, 0.27, 0.38)        # red
                    elif self._stalled:
                        accent = (0.91, 0.27, 0.38)        # red — stalled
                    else:
                        accent = (0.95, 0.71, 0.20)        # amber — working

                    bg = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                        bounds, 14.0, 14.0)
                    NSColor.colorWithCalibratedRed_green_blue_alpha_(
                        0.10, 0.10, 0.16, 0.96).setFill()
                    bg.fill()
                    NSColor.colorWithCalibratedRed_green_blue_alpha_(
                        accent[0], accent[1], accent[2], 0.55).setStroke()
                    bg.setLineWidth_(1.0)
                    bg.stroke()

                    # ---- Pulsing dot ----
                    dot_r = 6.0
                    cx_dot = 18.0
                    alpha = 0.45 + 0.55 * self._pulse
                    NSColor.colorWithCalibratedRed_green_blue_alpha_(
                        accent[0], accent[1], accent[2], alpha).setFill()
                    NSBezierPath.bezierPathWithOvalInRect_(
                        NSMakeRect(cx_dot - dot_r, cy - dot_r,
                                   dot_r * 2, dot_r * 2)).fill()

                    # ---- Status label ----
                    if self._mode == "recording":
                        label_text = "REC"
                    elif self._stalled:
                        label_text = "STALLED?"
                    else:
                        label_text = "WORKING"

                    label_font = NSFont.boldSystemFontOfSize_(11.0)
                    label_attrs = {
                        NSFontAttributeName: label_font,
                        NSForegroundColorAttributeName:
                            NSColor.colorWithCalibratedRed_green_blue_alpha_(
                                accent[0], accent[1], accent[2], 1.0),
                    }
                    NSString.stringWithString_(label_text).drawAtPoint_withAttributes_(
                        (36.0, cy - 7.0), label_attrs)

                    # ---- Main text (timer or status) ----
                    if self._mode == "recording":
                        try:
                            big_font = NSFont.monospacedDigitSystemFontOfSize_weight_(
                                14.0, 0.0)
                        except Exception:
                            big_font = NSFont.systemFontOfSize_(14.0)
                        text = self._timer_text
                        text_x = 88.0
                    else:
                        big_font = NSFont.systemFontOfSize_(12.0)
                        text = self._status or "starting…"
                        # Truncate so it doesn't overflow
                        max_w = w - 110
                        if len(text) > 50:
                            text = text[:48] + "…"
                        text_x = 96.0

                    text_attrs = {
                        NSFontAttributeName: big_font,
                        NSForegroundColorAttributeName: NSColor.whiteColor(),
                    }
                    NSString.stringWithString_(text).drawAtPoint_withAttributes_(
                        (text_x, cy - 8.0), text_attrs)

                    # ---- Stop pill (only in recording mode) ----
                    if self._mode == "recording":
                        pill_w = 78.0
                        pill_h = 30.0
                        pill_x = w - pill_w - 10.0
                        pill_y = (h - pill_h) / 2.0
                        pill = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                            NSMakeRect(pill_x, pill_y, pill_w, pill_h),
                            pill_h / 2.0, pill_h / 2.0)
                        NSColor.colorWithCalibratedRed_green_blue_alpha_(
                            0.91, 0.27, 0.38, 1.0).setFill()
                        pill.fill()

                        sq_size = 9.0
                        sq_x = pill_x + 14.0
                        sq_y = cy - sq_size / 2.0
                        NSColor.whiteColor().setFill()
                        NSBezierPath.bezierPathWithRect_(
                            NSMakeRect(sq_x, sq_y, sq_size, sq_size)).fill()

                        stop_font = NSFont.boldSystemFontOfSize_(13.0)
                        stop_attrs = {
                            NSFontAttributeName: stop_font,
                            NSForegroundColorAttributeName: NSColor.whiteColor(),
                        }
                        NSString.stringWithString_("Stop").drawAtPoint_withAttributes_(
                            (sq_x + sq_size + 8.0, cy - 8.0), stop_attrs)
                except Exception:
                    log.exception("meeting indicator: drawRect failed")

        view = IndicatorView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, HEIGHT))
        window.setContentView_(view)

        screen = NSScreen.mainScreen().frame()
        window.setFrameOrigin_((
            (screen.size.width - WIDTH) / 2.0,
            screen.size.height - HEIGHT - 60,
        ))

        self._window = window
        self._view = view

    # ------------------------------------------------------------------
    def show(self):
        if self._disabled:
            return
        try:
            if self._window is None:
                self._build()
            self._start_time = time.monotonic()
            self._mode = "recording"
            self._status = ""
            self._view.setMode_("recording")
            self._view.setStatus_("")
            self._view.setStalled_(False)
            self._view.setTimer_("00:00")
            self._view.setPulse_(1.0)
            self._window.orderFrontRegardless()
            log.info("meeting indicator: shown (recording)")
        except Exception:
            log.exception("meeting indicator: show failed")
            self._disabled = True

    def set_processing(self, stage="starting…", chars=0):
        """Switch the indicator to 'processing' mode and update the
        stage text. Safe to call repeatedly — that's the point: each
        call refreshes the displayed progress."""
        if self._disabled or self._window is None:
            return
        try:
            self._mode = "processing"
            text = stage if chars <= 0 else f"{stage} — {chars} chars"
            self._status = text
            self._last_progress = time.monotonic()
            self._view.setMode_("processing")
            self._view.setStatus_(text)
            self._view.setStalled_(False)
        except Exception:
            log.exception("meeting indicator: set_processing failed")

    def hide(self):
        try:
            if self._window is not None:
                self._window.orderOut_(None)
        except Exception:
            pass
        self._start_time = None
        self._mode = "recording"
        self._status = ""

    def tick(self):
        if self._disabled or self._window is None:
            return
        try:
            now = time.monotonic()
            if self._mode == "recording":
                if self._start_time is None:
                    return
                elapsed = int(now - self._start_time)
                if elapsed >= 3600:
                    text = (f"{elapsed // 3600:d}:"
                            f"{(elapsed % 3600) // 60:02d}:"
                            f"{elapsed % 60:02d}")
                else:
                    text = f"{elapsed // 60:02d}:{elapsed % 60:02d}"
                self._view.setTimer_(text)
                pulse = 0.5 + 0.5 * math.sin(now * 2.0 * math.pi)
                self._view.setPulse_(pulse)
            else:
                # Processing mode — pulse amber, watch for stall
                pulse = 0.55 + 0.45 * math.sin(now * 1.4 * math.pi)
                self._view.setPulse_(pulse)
                stalled = (now - self._last_progress) > STALL_AFTER
                self._view.setStalled_(stalled)
        except Exception:
            pass
