"""Floating recording indicator shown while a meeting is being captured.

A small rounded panel with a pulsing red dot, REC label, a live elapsed
timer, and a Stop region. Sits near the top center of the screen so the
user always sees that recording is active.

The whole panel is clickable — click anywhere (or specifically on the
"Stop" pill) to end the meeting without going to the menu bar.
"""

import logging
import math
import time

log = logging.getLogger("mywhisper")


class MeetingIndicator:
    def __init__(self, on_stop):
        self.on_stop = on_stop  # called when the panel is clicked
        self._window = None
        self._view = None
        self._start_time = None
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

        WIDTH, HEIGHT = 300, 56
        # NSWindowStyleMaskNonactivatingPanel | NSWindowStyleMaskBorderless
        # so it shows above other apps but doesn't steal focus, and
        # accepts mouse events on its content.
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

        on_stop = self.on_stop  # capture for the inner class

        class IndicatorView(NSView):
            def initWithFrame_(self, frame):
                v = objc.super(IndicatorView, self).initWithFrame_(frame)
                if v is not None:
                    v._timer_text = "00:00"
                    v._pulse = 1.0
                    v._stop_hover = False
                return v

            # Public setters --------------------------------------------
            def setTimer_(self, text):
                self._timer_text = text
                self.setNeedsDisplay_(True)

            def setPulse_(self, p):
                self._pulse = float(p)
                self.setNeedsDisplay_(True)

            # Mouse handling --------------------------------------------
            def acceptsFirstMouse_(self, event):
                # Receive clicks even when the window is not key.
                return True

            def mouseDown_(self, event):
                try:
                    if on_stop:
                        on_stop()
                except Exception:
                    log.exception("meeting indicator: click handler failed")

            def resetCursorRects(self):
                self.addCursorRect_cursor_(
                    self.bounds(), NSCursor.pointingHandCursor())

            # Drawing ---------------------------------------------------
            def drawRect_(self, dirty):
                try:
                    bounds = self.bounds()
                    w = float(bounds.size.width)
                    h = float(bounds.size.height)
                    cy = h / 2.0

                    # Rounded background (drawn here too so the rounded
                    # shape is consistent inside the view).
                    bg = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                        bounds, 14.0, 14.0)
                    NSColor.colorWithCalibratedRed_green_blue_alpha_(
                        0.10, 0.10, 0.16, 0.96).setFill()
                    bg.fill()

                    # Subtle red border
                    NSColor.colorWithCalibratedRed_green_blue_alpha_(
                        0.91, 0.27, 0.38, 0.55).setStroke()
                    bg.setLineWidth_(1.0)
                    bg.stroke()

                    # ---- Left side: pulsing dot ----
                    dot_r = 6.0
                    cx_dot = 18.0
                    alpha = 0.45 + 0.55 * self._pulse
                    NSColor.colorWithCalibratedRed_green_blue_alpha_(
                        0.91, 0.27, 0.38, alpha).setFill()
                    NSBezierPath.bezierPathWithOvalInRect_(
                        NSMakeRect(cx_dot - dot_r, cy - dot_r,
                                   dot_r * 2, dot_r * 2)).fill()

                    # ---- "REC" label ----
                    rec_font = NSFont.boldSystemFontOfSize_(11.0)
                    rec_attrs = {
                        NSFontAttributeName: rec_font,
                        NSForegroundColorAttributeName:
                            NSColor.colorWithCalibratedRed_green_blue_alpha_(
                                0.91, 0.27, 0.38, 1.0),
                    }
                    NSString.stringWithString_("REC").drawAtPoint_withAttributes_(
                        (36.0, cy - 7.0), rec_attrs)

                    # ---- Timer ----
                    try:
                        timer_font = NSFont.monospacedDigitSystemFontOfSize_weight_(
                            14.0, 0.0)
                    except Exception:
                        timer_font = NSFont.systemFontOfSize_(14.0)
                    timer_attrs = {
                        NSFontAttributeName: timer_font,
                        NSForegroundColorAttributeName: NSColor.whiteColor(),
                    }
                    NSString.stringWithString_(self._timer_text).drawAtPoint_withAttributes_(
                        (74.0, cy - 9.0), timer_attrs)

                    # ---- Right side: Stop pill ----
                    pill_w = 78.0
                    pill_h = 30.0
                    pill_x = w - pill_w - 10.0
                    pill_y = (h - pill_h) / 2.0
                    pill_rect = NSMakeRect(pill_x, pill_y, pill_w, pill_h)

                    pill = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                        pill_rect, pill_h / 2.0, pill_h / 2.0)
                    NSColor.colorWithCalibratedRed_green_blue_alpha_(
                        0.91, 0.27, 0.38, 1.0).setFill()
                    pill.fill()

                    # Stop label + square icon
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

        # Park near the top center of the main screen.
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
            self._view.setTimer_("00:00")
            self._view.setPulse_(1.0)
            self._window.orderFrontRegardless()
            log.info("meeting indicator: shown")
        except Exception:
            log.exception("meeting indicator: show failed")
            self._disabled = True

    def hide(self):
        try:
            if self._window is not None:
                self._window.orderOut_(None)
        except Exception:
            pass
        self._start_time = None

    def tick(self):
        if self._disabled or self._window is None or self._start_time is None:
            return
        try:
            elapsed = int(time.monotonic() - self._start_time)
            if elapsed >= 3600:
                text = f"{elapsed // 3600:d}:{(elapsed % 3600) // 60:02d}:{elapsed % 60:02d}"
            else:
                text = f"{elapsed // 60:02d}:{elapsed % 60:02d}"
            self._view.setTimer_(text)
            pulse = 0.5 + 0.5 * math.sin(time.monotonic() * 2.0 * math.pi)
            self._view.setPulse_(pulse)
        except Exception:
            pass
