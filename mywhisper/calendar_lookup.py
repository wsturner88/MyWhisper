"""Look up macOS Calendar events around a given time.

Uses EventKit, which reads ALL calendar accounts configured on the Mac —
Apple, Google, Outlook (added via System Settings → Internet Accounts or
synced through the Outlook app), Exchange, etc.

Read-only. Never modifies the calendar.
"""

import logging
from datetime import datetime, timedelta

import objc
from Foundation import NSDate

log = logging.getLogger("mywhisper")

# EventKit isn't auto-imported by PyObjC — load the bundle manually.
objc.loadBundle(
    "EventKit", globals(),
    bundle_path="/System/Library/Frameworks/EventKit.framework",
)

EKEventStore = objc.lookUpClass("EKEventStore")

# PyObjC doesn't ship metadata for EventKit's completion-handler methods,
# so it can't figure out how to bridge our Python callback to an
# Objective-C block. Register the signatures explicitly. Each block takes
# (BOOL granted, NSError *error) and returns void.
_COMPLETION_META = {
    "arguments": {
        2: {
            "callable": {
                "retval": {"type": b"v"},
                "arguments": {
                    0: {"type": b"^v"},          # block trampoline
                    1: {"type": objc._C_NSBOOL}, # BOOL granted
                    2: {"type": b"@"},           # NSError* error
                },
            }
        }
    }
}
_COMPLETION_META_3ARG = {
    "arguments": {
        3: _COMPLETION_META["arguments"][2],
    }
}

try:
    objc.registerMetaDataForSelector(
        b"EKEventStore",
        b"requestFullAccessToEventsWithCompletion:",
        _COMPLETION_META,
    )
except Exception:
    pass  # selector may not exist on older macOS

try:
    objc.registerMetaDataForSelector(
        b"EKEventStore",
        b"requestAccessToEntityType:completion:",
        _COMPLETION_META_3ARG,
    )
except Exception:
    pass

EKEntityTypeEvent = 0
# Authorization status integers (EKAuthorizationStatus)
#   0 NotDetermined, 1 Restricted, 2 Denied,
#   3 Authorized / WriteOnly,  4 FullAccess (macOS 14+)
GRANTED_STATUSES = (3, 4)

_store = None


def _get_store():
    """One persistent EKEventStore for the whole app session."""
    global _store
    if _store is None:
        _store = EKEventStore.alloc().init()
    return _store


def authorization_status():
    return EKEventStore.authorizationStatusForEntityType_(EKEntityTypeEvent)


def has_access():
    return authorization_status() in GRANTED_STATUSES


def status_label():
    s = authorization_status()
    return {
        0: "Not asked yet",
        1: "Restricted by system",
        2: "Denied",
        3: "Granted",
        4: "Granted (full)",
    }.get(s, f"Unknown ({s})")


def request_access(on_decision=None):
    """Ask macOS for calendar access (shows the system prompt if needed).

    Non-blocking. The completion runs on a background thread; if the
    caller cares about the result, pass on_decision(granted: bool).
    """
    store = _get_store()

    def _completion(granted, error):
        log.info("calendar permission: granted=%s error=%s", granted, error)
        if on_decision:
            try:
                on_decision(bool(granted))
            except Exception:
                log.exception("calendar permission callback failed")

    try:
        # macOS 14+
        store.requestFullAccessToEventsWithCompletion_(_completion)
    except Exception:
        # Older macOS
        store.requestAccessToEntityType_completion_(
            EKEntityTypeEvent, _completion)


def find_meeting_near(when=None, window_minutes=30):
    """Find the calendar event nearest to `when` (default: now).

    Returns a dict (title, attendees, organizer, notes, start, end) or
    None if no event is found, no permission, or any other failure. This
    is a soft lookup — never raises.
    """
    try:
        if not has_access():
            log.info("calendar lookup skipped: no access (%s)", status_label())
            return None
        when = when or datetime.now()
        start = when - timedelta(minutes=window_minutes)
        end = when + timedelta(minutes=window_minutes)
        store = _get_store()
        start_ns = NSDate.dateWithTimeIntervalSince1970_(start.timestamp())
        end_ns = NSDate.dateWithTimeIntervalSince1970_(end.timestamp())
        predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
            start_ns, end_ns, None)
        events = list(store.eventsMatchingPredicate_(predicate) or [])
        # All-day items (birthdays, holidays) are not meetings — without
        # this, a recording on someone's birthday gets titled "X's
        # Birthday" no matter what the meeting actually was.
        events = [e for e in events if not e.isAllDay()]
        if not events:
            log.info("calendar lookup: no events in ±%dmin window", window_minutes)
            return None
        # Pick the event whose start time is closest to `when`.
        target_ts = when.timestamp()

        def diff(evt):
            return abs(evt.startDate().timeIntervalSince1970() - target_ts)

        chosen = min(events, key=diff)
        info = _extract(chosen)
        log.info("calendar lookup: matched %r (start=%s, %d attendees)",
                 info.get("title"), info.get("start"),
                 len(info.get("attendees") or []))
        return info
    except Exception:
        log.exception("calendar lookup failed")
        return None


def _extract(event):
    """Convert an EKEvent into a plain dict."""
    title = str(event.title() or "").strip()
    notes = str(event.notes() or "").strip()

    attendees = []
    try:
        raw_attendees = event.attendees() or []
        for att in raw_attendees:
            name = str(att.name() or "").strip()
            email = ""
            try:
                url = att.URL()
                if url is not None:
                    spec = str(url.resourceSpecifier() or "").strip()
                    email = spec
            except Exception:
                pass
            if not name and email:
                name = email
            if name:
                attendees.append({"name": name, "email": email})
    except Exception:
        pass

    organizer = None
    try:
        org = event.organizer()
        if org is not None:
            org_name = str(org.name() or "").strip()
            if org_name:
                organizer = org_name
    except Exception:
        pass

    start = datetime.fromtimestamp(event.startDate().timeIntervalSince1970())
    end = datetime.fromtimestamp(event.endDate().timeIntervalSince1970())

    return {
        "title": title,
        "notes": notes,
        "attendees": attendees,
        "organizer": organizer,
        "start": start,
        "end": end,
    }


def context_block(meeting_info):
    """Format calendar info as a Markdown block for the LLM prompt.

    Returns '' if meeting_info is None or empty.
    """
    if not meeting_info:
        return ""
    lines = ["### Calendar context for this meeting"]
    if meeting_info.get("title"):
        lines.append(f"- **Calendar title:** {meeting_info['title']}")
    if meeting_info.get("organizer"):
        lines.append(f"- **Organizer:** {meeting_info['organizer']}")
    attendees = meeting_info.get("attendees") or []
    if attendees:
        names = [a["name"] for a in attendees if a.get("name")]
        if names:
            lines.append(f"- **Attendees:** {', '.join(names)}")
    if meeting_info.get("notes"):
        notes = meeting_info["notes"].strip()
        if len(notes) > 800:
            notes = notes[:800] + "..."
        lines.append(f"- **Agenda / notes from invite:**\n  {notes}")
    return "\n".join(lines) + "\n"
