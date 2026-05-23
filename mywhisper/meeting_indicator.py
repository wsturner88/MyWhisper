"""Floating recording indicator shown while a meeting is being captured.

A small rounded panel with a pulsing red dot, REC label, a live elapsed
timer, and a Stop button. Sits near the top center of the screen so the
user always sees that recording is active.

Click the Stop button to end the meeting without going to the menu bar.
"""

import logging
import math
import time

log = logging.getLogger("mywhisper")


class MeetingIndicator:
    def __init__(self, on_stop):
        self.on_stop = on_stop  # called when the Stop button is clicked
        self._window = None
        self._view = None
        self._button = None
        self._target = None       # NSObject delegate for the button action
        self._start_time = None
        self._disabled = False

    # ------------------------------------------------------------------
    def _build(self):
        import objc
        from AppKit import (
            NSWindow, NSColor, NSView, NSScreen, NSBackingStoreBuffered,
            NSButton, NSFont, NSFontAttributeName,
            NSForegroundColorAttributeName, NSBezierPath, NSObject,
        )
        from Foundation import NSMakeRect, NSString

        WIDTH, HEIGHT = 280, 56

        # The whole window
        rect = NSMakeRect(0, 0, WIDTH, HEIGHT)
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, 0, NSBackingStoreBuffered, False)
        window.setOpaque_(False)
        window.setBackgroundColor_(NSColor.clearColor())
        window.setLevel_(25)
        window.setIgnoresMouseEvents_(False)  # we need clicks for Stop
        window.setHasShadow_(True)

        # Rounded background container
        container = NSView.alloc().initWithFrame_(rect)
        container.setWantsLayer_(True)
        layer = container.layer()
        layer.setBackgroundColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(
            0.10, 0.10, 0.16, 0.96).CGColor())
        layer.setCornerRadius_(14.0)
        layer.setBorderWidth_(1.0)
        layer.setBorderColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(
            0.91, 0.27, 0.38, 0.55).CGColor())

        # Custom view that paints the red dot, REC label, and timer.
        class IndicatorView(NSView):
            def initWithFrame_(self, frame):
                v = objc.super(IndicatorView, self).initWithFrame_(frame)
                if v is not None:
                    v._timer_text = "00:00"
                    v._pulse = 1.0
                return v

            def setTimer_(self, text):
                self._timer_text = text
                self.setNeedsDisplay_(True)

            def setPulse_(self, p):
                self._pulse = float(p)
                self.setNeedsDisplay_(True)

            def drawRect_(self, dirty):
                try:
                    bounds = self.bounds()
                    h = float(bounds.size.height)
                    cy = h / 2.0

                    # Pulsing red dot
                    dot_r = 6.0
                    cx_dot = 20.0
                    alpha = 0.45 + 0.55 * self._pulse
                    NSColor.colorWithCalibratedRed_green_blue_alpha_(
                        0.91, 0.27, 0.38, alpha).setFill()
                    NSBezierPath.bezierPathWithOvalInRect_(
                        NSMakeRect(cx_dot - dot_r, cy - dot_r,
                                   dot_r * 2, dot_r * 2)).fill()

                    # "REC" label
                    rec_font = NSFont.boldSystemFontOfSize_(11.0)
                    rec_attrs = {
                        NSFontAttributeName: rec_font,
                        NSForegroundColorAttributeName:
                            NSColor.colorWithCalibratedRed_green_blue_alpha_(
                                0.91, 0.27, 0.38, 1.0),
                    }
                    NSString.stringWithString_("REC").drawAtPoint_withAttributes_(
                        (38.0, cy - 7.0), rec_attrs)

                    # Timer
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
                        (76.0, cy - 9.0), timer_attrs)
                except Exception:
                    pass

        view = IndicatorView.alloc().initWithFrame_(
            NSMakeRect(0, 0, WIDTH - 70, HEIGHT))
        container.addSubview_(view)

        # Stop button — NSObject wrapper so the button has somewhere to send
        # its action message.
        class _StopTarget(NSObject):
            def initWithCallback_(self, cb):
                me = objc.super(_StopTarget, self).init()
                if me is not None:
                    me._cb = cb
                return me

            def fire_(self, sender):
                try:
                    if self._cb:
                        self._cb()
                except Exception:
                    log.exception("meeting indicator: stop callback failed")

        target = _StopTarget.alloc().initWithCallback_(self.on_stop)

        BTN_W, BTN_H = 56, 24
        button = NSButton.alloc().initWithFrame_(NSMakeRect(
            WIDTH - BTN_W - 12, (HEIGHT - BTN_H) / 2.0, BTN_W, BTN_H))
        button.setTitle_("Stop")
        button.setBezelStyle_(1)  # NSBezelStyleRounded
        button.setTarget_(target)
        button.setAction_(b"fire:")
        container.addSubview_(button)

        # Park it near the top of the main screen.
        screen = NSScreen.mainScreen().frame()
        window.setFrameOrigin_((
            (screen.size.width - WIDTH) / 2.0,
            screen.size.height - HEIGHT - 60,
        ))
        window.setContentView_(container)

        self._window = window
        self._view = view
        self._button = button
        self._target = target

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
        """Called periodically while a meeting is recording."""
        if self._disabled or self._window is None or self._start_time is None:
            return
        try:
            elapsed = int(time.monotonic() - self._start_time)
            if elapsed >= 3600:
                text = f"{elapsed // 3600:d}:{(elapsed % 3600) // 60:02d}:{elapsed % 60:02d}"
            else:
                text = f"{elapsed // 60:02d}:{elapsed % 60:02d}"
            self._view.setTimer_(text)
            # ~1s pulse cycle on the red dot
            pulse = 0.5 + 0.5 * math.sin(time.monotonic() * 2.0 * math.pi)
            self._view.setPulse_(pulse)
        except Exception:
            pass
