"""Floating audio-level indicators shown while dictating.

Two styles, selectable via the Settings menu: a clean oscilloscope-style
mirrored waveform (default), and a retro analog VU meter with a swinging
needle.

Every public method is fail-safe: if any windowing call fails, the
indicator quietly disables itself rather than affecting recording.
"""

import logging
import math

_POINTS = 60

log = logging.getLogger("mywhisper")


def _make_window(width, height, bg_color_fn):
    """Build a borderless floating rounded panel + a transparent content view."""
    from AppKit import (NSWindow, NSColor, NSView, NSScreen,
                        NSBackingStoreBuffered)
    from Foundation import NSMakeRect
    rect = NSMakeRect(0, 0, width, height)
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, 0, NSBackingStoreBuffered, False)
    window.setOpaque_(False)
    window.setBackgroundColor_(NSColor.clearColor())
    window.setLevel_(25)
    window.setIgnoresMouseEvents_(True)
    window.setHasShadow_(True)
    container = NSView.alloc().initWithFrame_(rect)
    container.setWantsLayer_(True)
    layer = container.layer()
    layer.setBackgroundColor_(bg_color_fn(NSColor).CGColor())
    layer.setCornerRadius_(20.0)
    window.setContentView_(container)
    screen = NSScreen.mainScreen().frame()
    window.setFrameOrigin_(((screen.size.width - width) / 2.0, 150.0))
    return window, container


class Waveform:
    """Oscilloscope-style mirrored waveform — thin curved lines."""

    def __init__(self):
        self._history = [0.0] * _POINTS
        self._window = None
        self._view = None
        self._disabled = False

    def _build(self):
        import objc
        from AppKit import NSColor, NSView, NSBezierPath

        class WaveView(NSView):
            def initWithFrame_(self, frame):
                self_ = objc.super(WaveView, self).initWithFrame_(frame)
                if self_ is not None:
                    self_._levels = [0.0] * _POINTS
                return self_

            def setLevels_(self, levels):
                self._levels = list(levels)
                self.setNeedsDisplay_(True)

            def drawRect_(self, dirty):
                try:
                    bounds = self.bounds()
                    w = float(bounds.size.width)
                    h = float(bounds.size.height)
                    cy = h / 2.0

                    NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.20).setStroke()
                    center = NSBezierPath.bezierPath()
                    center.setLineWidth_(1.0)
                    center.moveToPoint_((8.0, cy))
                    center.lineToPoint_((w - 8.0, cy))
                    center.stroke()

                    if not self._levels:
                        return

                    NSColor.whiteColor().setStroke()
                    n = len(self._levels)
                    max_amp = h * 0.42
                    left, right = 8.0, w - 8.0
                    span = right - left

                    for sign in (1, -1):
                        pts = []
                        for i in range(n):
                            x = left + (i / (n - 1)) * span
                            v = max(0.0, min(1.0, self._levels[i]))
                            pts.append((x, cy + sign * v * max_amp))

                        p = NSBezierPath.bezierPath()
                        p.setLineWidth_(1.6)
                        p.setLineCapStyle_(1)
                        p.setLineJoinStyle_(1)
                        p.moveToPoint_(pts[0])
                        m = len(pts)
                        for i in range(m - 1):
                            p0 = pts[i - 1] if i > 0 else pts[i]
                            p1 = pts[i]
                            p2 = pts[i + 1]
                            p3 = pts[i + 2] if i + 2 < m else pts[i + 1]
                            cp1 = (p1[0] + (p2[0] - p0[0]) / 6.0,
                                   p1[1] + (p2[1] - p0[1]) / 6.0)
                            cp2 = (p2[0] - (p3[0] - p1[0]) / 6.0,
                                   p2[1] - (p3[1] - p1[1]) / 6.0)
                            p.curveToPoint_controlPoint1_controlPoint2_(p2, cp1, cp2)
                        p.stroke()
                except Exception:
                    pass

        self._window, container = _make_window(
            320, 80,
            lambda C: C.colorWithCalibratedWhite_alpha_(0.10, 0.93))
        view = WaveView.alloc().initWithFrame_(container.bounds())
        container.addSubview_(view)
        self._view = view

    def show(self):
        if self._disabled:
            return
        try:
            if self._window is None:
                self._build()
            self._history = [0.0] * _POINTS
            self._view.setLevels_(self._history)
            self._window.orderFrontRegardless()
        except Exception:
            self._disabled = True

    def hide(self):
        try:
            if self._window is not None:
                self._window.orderOut_(None)
        except Exception:
            pass

    def update(self, level):
        if self._disabled or self._window is None:
            return
        try:
            raw = max(0.0, min(1.0, level * 11.0))
            last = self._history[-1] if self._history else 0.0
            smoothed = 0.5 * raw + 0.5 * last
            self._history.append(smoothed)
            self._history = self._history[-_POINTS:]
            self._view.setLevels_(self._history)
        except Exception:
            pass


class VuMeter:
    """Retro analog VU meter — needle swinging on a curved scale."""

    def __init__(self):
        self._displayed = 0.0
        self._window = None
        self._view = None
        self._disabled = False

    def _build(self):
        import objc
        from AppKit import (NSColor, NSView, NSBezierPath, NSFont,
                            NSFontAttributeName, NSForegroundColorAttributeName)
        from Foundation import NSMakeRect, NSString

        draw_count = [0]

        class VuView(NSView):
            def initWithFrame_(self, frame):
                self_ = objc.super(VuView, self).initWithFrame_(frame)
                if self_ is not None:
                    self_._lvl = 0.0
                return self_

            def setLvl_(self, level):
                self._lvl = float(level)
                self.setNeedsDisplay_(True)

            def drawRect_(self, dirty):
                step = "enter"
                try:
                    bounds = self.bounds()
                    w = float(bounds.size.width)
                    h = float(bounds.size.height)

                    cx = w / 2.0
                    pivot_y = 14.0
                    r = h - 30.0

                    ANGLE_LO, ANGLE_HI = 150.0, 30.0   # at -20 dB and +3 dB

                    def angle_for_lvl(lvl):
                        return ANGLE_LO + (ANGLE_HI - ANGLE_LO) * lvl

                    def angle_for_db(db):
                        return angle_for_lvl((db - (-20)) / (3 - (-20)))

                    ink = NSColor.colorWithCalibratedWhite_alpha_(0.05, 1.0)
                    red = NSColor.colorWithCalibratedRed_green_blue_alpha_(
                        0.62, 0.10, 0.10, 1.0)

                    step = "arc"
                    ink.setStroke()
                    arc = NSBezierPath.bezierPath()
                    arc.setLineWidth_(1.4)
                    arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
                        (cx, pivot_y), r, ANGLE_HI, ANGLE_LO)
                    arc.stroke()

                    step = "red zone"
                    red.setStroke()
                    rz = NSBezierPath.bezierPath()
                    rz.setLineWidth_(5.0)
                    rz.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
                        (cx, pivot_y), r + 5.0, ANGLE_HI, angle_for_db(0))
                    rz.stroke()

                    step = "ticks"
                    font = NSFont.systemFontOfSize_(9.0)
                    label_attrs = {
                        NSFontAttributeName: font,
                        NSForegroundColorAttributeName: ink,
                    }
                    ticks = [-20, -10, -7, -5, -3, -2, -1, 0, 1, 2, 3]
                    ink.setStroke()
                    for db in ticks:
                        a = math.radians(angle_for_db(db))
                        x_in = cx + (r - 7.0) * math.cos(a)
                        y_in = pivot_y + (r - 7.0) * math.sin(a)
                        x_out = cx + r * math.cos(a)
                        y_out = pivot_y + r * math.sin(a)
                        tk = NSBezierPath.bezierPath()
                        tk.setLineWidth_(1.0)
                        tk.moveToPoint_((x_in, y_in))
                        tk.lineToPoint_((x_out, y_out))
                        tk.stroke()

                        label_text = str(db) if db <= 0 else f"+{db}"
                        x_lbl = cx + (r - 18.0) * math.cos(a)
                        y_lbl = pivot_y + (r - 18.0) * math.sin(a)
                        tw = len(label_text) * 5.5
                        NSString.stringWithString_(label_text).drawAtPoint_withAttributes_(
                            (x_lbl - tw / 2.0, y_lbl - 5.0), label_attrs)

                    step = "needle"
                    lvl = max(0.0, min(1.0, self._lvl))
                    a = math.radians(angle_for_lvl(lvl))
                    tip_x = cx + (r - 4.0) * math.cos(a)
                    tip_y = pivot_y + (r - 4.0) * math.sin(a)
                    back_x = cx - 6.0 * math.cos(a)
                    back_y = pivot_y - 6.0 * math.sin(a)
                    ink.setStroke()
                    nd = NSBezierPath.bezierPath()
                    nd.setLineWidth_(1.8)
                    nd.setLineCapStyle_(1)
                    nd.moveToPoint_((back_x, back_y))
                    nd.lineToPoint_((tip_x, tip_y))
                    nd.stroke()

                    step = "pivot"
                    pr = 5.0
                    ink.setFill()
                    NSBezierPath.bezierPathWithOvalInRect_(
                        NSMakeRect(cx - pr, pivot_y - pr, pr * 2, pr * 2)).fill()

                    step = "VU labels"
                    vu_font = NSFont.systemFontOfSize_(13.0)
                    vu_attrs = {
                        NSFontAttributeName: vu_font,
                        NSForegroundColorAttributeName: ink,
                    }
                    vu_str = NSString.stringWithString_("VU")
                    vu_str.drawAtPoint_withAttributes_((16.0, h - 22.0), vu_attrs)
                    vu_str.drawAtPoint_withAttributes_((w - 34.0, h - 22.0), vu_attrs)

                    if draw_count[0] < 3:
                        draw_count[0] += 1
                        log.info("vu draw: complete (%dx%d)", int(w), int(h))
                except Exception as e:
                    if draw_count[0] < 5:
                        draw_count[0] += 1
                        log.exception("vu draw failed at step %r: %s", step, e)

        self._window, container = _make_window(
            320, 140,
            lambda C: C.colorWithCalibratedRed_green_blue_alpha_(0.95, 0.92, 0.78, 0.97))
        view = VuView.alloc().initWithFrame_(container.bounds())
        container.addSubview_(view)
        self._view = view

    def show(self):
        if self._disabled:
            return
        try:
            if self._window is None:
                self._build()
            self._displayed = 0.0
            self._view.setLvl_(0.0)
            self._window.orderFrontRegardless()
        except Exception:
            self._disabled = True

    def hide(self):
        try:
            if self._window is not None:
                self._window.orderOut_(None)
        except Exception:
            pass

    def update(self, level):
        if self._disabled or self._window is None:
            return
        try:
            raw = max(0.0, min(1.0, level * 11.0))
            # ballistic-needle smoothing — slower than the waveform for that swing
            self._displayed += (raw - self._displayed) * 0.25
            self._view.setLvl_(self._displayed)
        except Exception:
            pass


class Indicator:
    """Routes show/hide/update to whichever style is currently selected."""

    KINDS = ("waveform", "vu_meter")

    def __init__(self, kind="waveform"):
        self.kind = kind if kind in self.KINDS else "waveform"
        self._waveform = Waveform()
        self._vu = VuMeter()

    def _active(self):
        return self._vu if self.kind == "vu_meter" else self._waveform

    def set_kind(self, kind):
        if kind not in self.KINDS or kind == self.kind:
            return
        self._waveform.hide()
        self._vu.hide()
        self.kind = kind

    def show(self):
        if self.kind == "waveform":
            self._vu.hide()
        else:
            self._waveform.hide()
        self._active().show()

    def hide(self):
        self._waveform.hide()
        self._vu.hide()

    def update(self, level):
        self._active().update(level)
