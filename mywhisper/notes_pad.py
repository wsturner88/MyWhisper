"""Floating notes pad shown while a meeting is recording.

A small panel below the recording indicator with a free-form text area.
The user can jot names, jargon, correction hints, important moments —
anything that adds context the LLM otherwise couldn't infer.

When the meeting ends, get_text() returns whatever was typed. The caller
(app.py) feeds it into the summarization prompt as extra context.
"""

import logging

log = logging.getLogger("mywhisper")


class NotesPad:
    def __init__(self):
        self._window = None
        self._text_view = None
        self._disabled = False

    # ------------------------------------------------------------------
    def _build(self):
        import objc
        from AppKit import (
            NSPanel, NSColor, NSView, NSScreen, NSBackingStoreBuffered,
            NSTextView, NSScrollView, NSFont, NSTextField,
        )
        from Foundation import NSMakeRect, NSMakeSize

        WIDTH, HEIGHT = 360, 220
        STYLE_MASK = (1 << 7)   # NSWindowStyleMaskNonactivatingPanel

        rect = NSMakeRect(0, 0, WIDTH, HEIGHT)
        window = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, STYLE_MASK, NSBackingStoreBuffered, False)
        window.setOpaque_(False)
        window.setBackgroundColor_(NSColor.clearColor())
        window.setLevel_(25)
        window.setIgnoresMouseEvents_(False)
        window.setHasShadow_(True)
        window.setBecomesKeyOnlyIfNeeded_(False)
        window.setHidesOnDeactivate_(False)

        # Rounded container with cream/notepad background
        container = NSView.alloc().initWithFrame_(rect)
        container.setWantsLayer_(True)
        layer = container.layer()
        layer.setBackgroundColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(
            0.98, 0.97, 0.88, 0.97).CGColor())
        layer.setCornerRadius_(12.0)
        layer.setBorderWidth_(1.0)
        layer.setBorderColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(
            0.7, 0.6, 0.4, 0.5).CGColor())

        # Small label at the top
        TOP_BAR = 26
        label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(12, HEIGHT - TOP_BAR + 2, WIDTH - 24, 18))
        label.setStringValue_("Meeting Notes (added to summary)")
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setFont_(NSFont.boldSystemFontOfSize_(11.0))
        label.setTextColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(
            0.45, 0.35, 0.20, 1.0))
        container.addSubview_(label)

        # Text view inside a scroll view
        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(10, 10, WIDTH - 20, HEIGHT - TOP_BAR - 10))
        scroll.setBorderType_(0)  # NSNoBorder
        scroll.setHasVerticalScroller_(True)
        scroll.setHasHorizontalScroller_(False)
        scroll.setAutohidesScrollers_(True)
        scroll.setDrawsBackground_(False)

        text_view = NSTextView.alloc().initWithFrame_(
            NSMakeRect(0, 0, WIDTH - 20, HEIGHT - TOP_BAR - 10))
        text_view.setFont_(NSFont.systemFontOfSize_(13.0))
        text_view.setTextColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(
            0.15, 0.12, 0.08, 1.0))
        text_view.setBackgroundColor_(NSColor.clearColor())
        text_view.setDrawsBackground_(False)
        text_view.setRichText_(False)
        text_view.setAutomaticQuoteSubstitutionEnabled_(False)
        text_view.setAutomaticDashSubstitutionEnabled_(False)
        text_view.setVerticallyResizable_(True)
        text_view.setHorizontallyResizable_(False)
        text_view.setAutoresizingMask_(2)  # NSViewWidthSizable

        scroll.setDocumentView_(text_view)
        container.addSubview_(scroll)

        # Park to the right of where the recording indicator usually sits.
        screen = NSScreen.mainScreen().frame()
        window.setFrameOrigin_((
            (screen.size.width - WIDTH) / 2.0 + 320,  # right of indicator
            screen.size.height - HEIGHT - 60,
        ))
        window.setContentView_(container)

        self._window = window
        self._text_view = text_view

    # ------------------------------------------------------------------
    def show(self):
        if self._disabled:
            return
        try:
            if self._window is None:
                self._build()
            # Clear text from any previous meeting
            self._text_view.setString_("")
            self._window.orderFrontRegardless()
            log.info("notes pad: shown")
        except Exception:
            log.exception("notes pad: show failed")
            self._disabled = True

    def hide(self):
        try:
            if self._window is not None:
                self._window.orderOut_(None)
        except Exception:
            pass

    def get_text(self):
        """Return the user's typed notes — empty string if nothing typed
        or pad never shown."""
        try:
            if self._text_view is None:
                return ""
            return str(self._text_view.string() or "").strip()
        except Exception:
            log.exception("notes pad: get_text failed")
            return ""
