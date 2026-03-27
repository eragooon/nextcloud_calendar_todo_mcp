#!/usr/bin/env python3
"""
MCP Server — Nextcloud Calendar & Tasks via CalDAV.

Exposes tools over HTTPS (Streamable-HTTP transport):
  • create_event        → writes a VEVENT into the personal calendar
  • create_family_event → writes a VEVENT into the family calendar
  • create_todo         → writes a VTODO  into the tasks calendar
  • delete_event(uid)   → removes a Claude-created event
  • delete_todo(uid)    → removes a Claude-created todo
  • update_event(uid)   → patches a Claude-created event
  • update_todo(uid)    → patches a Claude-created todo

All Claude-created entries are tagged with X-CLAUDE-CREATED:TRUE.
delete/update refuse to touch entries without that tag.

Authentication: OAuth 2.0 Authorization Code + PKCE  (see auth.py).
Transport:      HTTP Streamable-HTTP (served directly via Tailscale Funnel).
"""

import os
import sys
import traceback
import uuid
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

import caldav
from dotenv import load_dotenv
from icalendar import Alarm, Calendar, Event, Todo, vText
from loguru import logger
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from auth import SimpleOAuthProvider

load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────────────
_LOG_FMT = "{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}"

logger.remove()
logger.add(sys.stderr, level="INFO", format=_LOG_FMT)

_log_file = "/var/log/nextcloud-mcp.log"
try:
    logger.add(
        _log_file,
        level="DEBUG",
        format=_LOG_FMT,
        rotation="10 MB",
        retention="14 days",
        compression="gz",
    )
    logger.debug(f"File logging active: {_log_file}")
except (PermissionError, OSError):
    _log_file = "/tmp/nextcloud-mcp.log"
    logger.add(
        _log_file,
        level="DEBUG",
        format=_LOG_FMT,
        rotation="10 MB",
        retention="14 days",
        compression="gz",
    )
    logger.warning(f"/var/log not writable – logging to fallback: {_log_file}")

# ── Config ─────────────────────────────────────────────────────────────────────
NEXTCLOUD_URL: str = os.environ["NEXTCLOUD_URL"].rstrip("/")
NEXTCLOUD_USER: str = os.environ["NEXTCLOUD_USER"]
NEXTCLOUD_APP_PASSWORD: str = os.environ["NEXTCLOUD_APP_PASSWORD"]
CALENDAR_NAME: str = os.environ["CALENDAR_NAME"]
FAMILY_CALENDAR_NAME: str = os.environ.get("FAMILY_CALENDAR_NAME", "Familie")
TASKS_CALENDAR_NAME: str = os.environ["TASKS_CALENDAR_NAME"]
OAUTH_CLIENT_ID: str = os.environ["OAUTH_CLIENT_ID"]
OAUTH_CLIENT_SECRET: str = os.environ["OAUTH_CLIENT_SECRET"]
OAUTH_REDIRECT_URI: str = os.environ.get(
    "OAUTH_REDIRECT_URI", "https://claude.ai/api/mcp/auth_callback"
)
HOST: str = os.environ.get("HOST", "127.0.0.1")
PORT: int = int(os.environ.get("PORT", "8000"))
RESOURCE_SERVER_URL: str = os.environ.get(
    "RESOURCE_SERVER_URL", f"http://{HOST}:{PORT}"
).rstrip("/")

# ── FastMCP instance ───────────────────────────────────────────────────────────
_oauth_provider = SimpleOAuthProvider(
    OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET, OAUTH_REDIRECT_URI, RESOURCE_SERVER_URL
)

_external_host = urlparse(RESOURCE_SERVER_URL).netloc
_transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", _external_host],
    allowed_origins=["http://127.0.0.1:*", "http://localhost:*", f"https://{_external_host}"],
)

mcp = FastMCP(
    "calendar-todo",
    host=HOST,
    port=PORT,
    stateless_http=True,
    json_response=True,
    auth_server_provider=_oauth_provider,
    transport_security=_transport_security,
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(RESOURCE_SERVER_URL),
        resource_server_url=AnyHttpUrl(RESOURCE_SERVER_URL),
        required_scopes=["mcp"],
        client_registration_options=ClientRegistrationOptions(enabled=False),
    ),
)

# ── CalDAV helpers ─────────────────────────────────────────────────────────────
def _caldav_client() -> caldav.DAVClient:
    return caldav.DAVClient(
        url=f"{NEXTCLOUD_URL}/remote.php/dav",
        username=NEXTCLOUD_USER,
        password=NEXTCLOUD_APP_PASSWORD,
    )


def _get_calendar(client: caldav.DAVClient, name: str) -> caldav.Calendar:
    """Find a calendar by display name; raise ValueError if missing."""
    principal = client.principal()
    for cal in principal.calendars():
        if cal.get_display_name() == name:
            return cal
    names = ", ".join(
        repr(c.get_display_name()) for c in principal.calendars() if c.get_display_name()
    )
    raise ValueError(f"Calendar '{name}' not found. Available: [{names}]")


def _base_calendar() -> Calendar:
    cal = Calendar()
    cal.add("prodid", "-//calendar-todo-mcp//calendar-todo-mcp//EN")
    cal.add("version", "2.0")
    return cal


def _parse_dt(value: str) -> date | datetime:
    if "T" in value:
        return datetime.fromisoformat(value)
    return date.fromisoformat(value)


_CLAUDE_PROP = "x-claude-created"
_CLAUDE_VALUE = "TRUE"


def _add_alarms(component: Event, title: str, dtstart: date | datetime) -> None:
    """Attach VALARM reminders: timed events get 1d+1h, all-day events get 1d."""
    triggers = [timedelta(days=-1)]
    if isinstance(dtstart, datetime):
        triggers.append(timedelta(hours=-1))
    for delta in triggers:
        alarm = Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", title)
        alarm.add("trigger", delta)
        component.add_component(alarm)


def _set_or_add(component, key: str, value) -> None:
    """Update a property: delete first so component.add() applies correct icalendar type encoding."""
    if key in component:
        del component[key]
    component.add(key, value)


def _assert_claude_created(component) -> None:
    """Raise ValueError if X-CLAUDE-CREATED is not TRUE."""
    val = component.get(_CLAUDE_PROP)
    if val is None or str(val).upper() != _CLAUDE_VALUE:
        raise ValueError(
            "Dieser Eintrag wurde nicht von Claude erstellt und kann nicht bearbeitet werden."
        )


# ── CalDAV lookup ──────────────────────────────────────────────────────────────
def _find_event_by_uid(uid: str) -> tuple[caldav.CalendarObjectResource, object]:
    """Search for a VEVENT by UID across personal and family calendars."""
    client = _caldav_client()
    for cal_name in [CALENDAR_NAME, FAMILY_CALENDAR_NAME]:
        try:
            cal = _get_calendar(client, cal_name)
        except ValueError:
            continue
        for obj in cal.search(uid=uid, event=True):
            ical = Calendar.from_ical(obj.data)
            for component in ical.walk():
                if component.name == "VEVENT" and str(component.get("uid", "")) == uid:
                    return obj, component
    raise ValueError(
        f"Kein Event mit UID '{uid}' in '{CALENDAR_NAME}' oder '{FAMILY_CALENDAR_NAME}' gefunden."
    )


def _find_todo_by_uid(uid: str) -> tuple[caldav.CalendarObjectResource, object]:
    """Search for a VTODO by UID in the tasks calendar."""
    client = _caldav_client()
    try:
        cal = _get_calendar(client, TASKS_CALENDAR_NAME)
    except ValueError:
        raise ValueError(f"Aufgaben-Kalender '{TASKS_CALENDAR_NAME}' nicht gefunden.")
    for obj in cal.search(uid=uid, todo=True):
        ical = Calendar.from_ical(obj.data)
        for component in ical.walk():
            if component.name == "VTODO" and str(component.get("uid", "")) == uid:
                return obj, component
    raise ValueError(f"Kein Todo mit UID '{uid}' im Kalender '{TASKS_CALENDAR_NAME}' gefunden.")


def _save_component(obj: caldav.CalendarObjectResource, component) -> None:
    """Rebuild the iCal wrapper around a patched component and save it."""
    wrapper = _base_calendar()
    wrapper.add_component(component)
    obj.data = wrapper.to_ical().decode()
    obj.save()


# ── Error helpers ──────────────────────────────────────────────────────────────
def _classify_error(err_text: str) -> str:
    t = err_text.lower()
    if any(k in t for k in ("encoding", "codec", "unicode", "charmap", "ascii")):
        return (
            "[Sonderzeichen] Der Titel enthält nicht-ASCII-Zeichen. "
            "Bitte nur einfache ASCII-Zeichen verwenden (kein –, —, Umlaute etc.)."
        )
    if any(k in t for k in ("timeout", "timed out", "connectionerror", "connection refused",
                             "httperror", "remotedisconnected", "socket")):
        return (
            "[Verbindung] Nextcloud ist nicht erreichbar. "
            "Bitte prüfen ob der Server läuft und die URL korrekt ist."
        )
    if any(k in t for k in ("memoryerror", "out of memory", "ram")):
        return (
            "[Speicher] Server hat zu wenig RAM. "
            "Bitte den MCP-Server neu starten."
        )
    if any(k in t for k in ("permission", "auth", "unauthorized", "403", "401",
                             "forbidden", "invalid credentials")):
        return (
            "[Auth] Authentifizierung fehlgeschlagen. "
            "Bitte das CalDAV App-Passwort prüfen oder erneuern."
        )
    return "[Unbekannt] Nicht klassifizierbarer Fehler – siehe Traceback unten."


def _error_response(context: str, e: Exception) -> str:
    tb = traceback.format_exc()
    logger.exception(f"{context} failed: {e}")
    hint = _classify_error(str(e) + tb)
    return (
        f"ERROR in {context}: {hint}\n\n"
        f"Fehlertyp: {type(e).__name__}: {e}\n\n"
        f"Traceback:\n{tb}"
    )


# ── Internal event factory ─────────────────────────────────────────────────────
def _create_event_in_calendar(
    cal_name: str,
    title: str,
    dtstart: str,
    dtend: str,
    description: str,
) -> str:
    """Shared implementation for create_event and create_family_event."""
    uid = str(uuid.uuid4())
    cal = _base_calendar()
    event = Event()
    event.add("uid", uid)
    event.add("dtstamp", datetime.now(timezone.utc))
    event.add("summary", title)
    dt_start = _parse_dt(dtstart)
    event.add("dtstart", dt_start)
    event.add("dtend", _parse_dt(dtend))
    if description:
        event.add("description", description)
    event.add(_CLAUDE_PROP, vText(_CLAUDE_VALUE))
    _add_alarms(event, title, dt_start)
    cal.add_component(event)

    client = _caldav_client()
    _get_calendar(client, cal_name).save_event(cal.to_ical().decode())
    logger.info(f"create_event OK cal={cal_name!r} uid={uid!r} title={title!r}")
    return f"Event '{title}' created successfully (UID: {uid})"


# ── Tools ──────────────────────────────────────────────────────────────────────
@mcp.tool()
def create_event(
    title: str,
    dtstart: str,
    dtend: str,
    description: str = "",
) -> str:
    """Create a calendar event in the personal Nextcloud Calendar.

    Args:
        title:       Event title (SUMMARY field).
        dtstart:     Start datetime in ISO 8601.
                     Timed:   "2026-03-26T14:00:00" or "2026-03-26T14:00:00+01:00"
                     All-day: "2026-03-26"
        dtend:       End datetime in ISO 8601 (same format as dtstart).
                     For all-day events use the exclusive next day:
                     dtstart="2026-03-26" -> dtend="2026-03-27"
        description: Optional free-text description.

    Returns:
        Confirmation message with the generated event UID.
    """
    try:
        return _create_event_in_calendar(CALENDAR_NAME, title, dtstart, dtend, description)
    except Exception as e:
        return _error_response("create_event", e)


@mcp.tool()
def create_family_event(
    title: str,
    dtstart: str,
    dtend: str,
    description: str = "",
) -> str:
    """Create a calendar event in the family Nextcloud Calendar.

    Args:
        title:       Event title (SUMMARY field).
        dtstart:     Start datetime in ISO 8601.
        dtend:       End datetime in ISO 8601.
        description: Optional free-text description.

    Returns:
        Confirmation message with the generated event UID.
    """
    try:
        return _create_event_in_calendar(FAMILY_CALENDAR_NAME, title, dtstart, dtend, description)
    except Exception as e:
        return _error_response("create_family_event", e)


@mcp.tool()
def create_todo(
    title: str,
    due: str | None = None,
    description: str = "",
) -> str:
    """Create a task/todo in Nextcloud Tasks.

    Args:
        title:       Task title (SUMMARY field).
        due:         Optional due date or datetime in ISO 8601.
        description: Optional free-text description.

    Returns:
        Confirmation message with the generated task UID.
    """
    try:
        uid = str(uuid.uuid4())
        cal = _base_calendar()
        todo = Todo()
        todo.add("uid", uid)
        todo.add("dtstamp", datetime.now(timezone.utc))
        todo.add("summary", title)
        todo.add("status", "NEEDS-ACTION")
        if due is not None:
            todo.add("due", _parse_dt(due))
        if description:
            todo.add("description", description)
        todo.add(_CLAUDE_PROP, vText(_CLAUDE_VALUE))
        cal.add_component(todo)

        client = _caldav_client()
        _get_calendar(client, TASKS_CALENDAR_NAME).save_todo(cal.to_ical().decode())
        logger.info(f"create_todo OK uid={uid!r} title={title!r}")
        return f"Todo '{title}' created successfully (UID: {uid})"
    except Exception as e:
        return _error_response("create_todo", e)


@mcp.tool()
def delete_event(uid: str) -> str:
    """Delete a Claude-created calendar event by UID.

    Only events created by Claude (tagged X-CLAUDE-CREATED) can be deleted.

    Args:
        uid: The UID of the event to delete (returned by create_event / create_family_event).

    Returns:
        Confirmation message.
    """
    try:
        obj, component = _find_event_by_uid(uid)
        _assert_claude_created(component)
        obj.delete()
        logger.info(f"delete_event OK uid={uid!r}")
        return f"Event with UID '{uid}' deleted successfully."
    except Exception as e:
        return _error_response("delete_event", e)


@mcp.tool()
def delete_todo(uid: str) -> str:
    """Delete a Claude-created todo by UID.

    Only todos created by Claude (tagged X-CLAUDE-CREATED) can be deleted.

    Args:
        uid: The UID of the todo to delete (returned by create_todo).

    Returns:
        Confirmation message.
    """
    try:
        obj, component = _find_todo_by_uid(uid)
        _assert_claude_created(component)
        obj.delete()
        logger.info(f"delete_todo OK uid={uid!r}")
        return f"Todo with UID '{uid}' deleted successfully."
    except Exception as e:
        return _error_response("delete_todo", e)


@mcp.tool()
def update_event(
    uid: str,
    title: str | None = None,
    dtstart: str | None = None,
    dtend: str | None = None,
    description: str | None = None,
) -> str:
    """Update a Claude-created calendar event. Only provided fields are changed.

    Only events created by Claude (tagged X-CLAUDE-CREATED) can be updated.

    Args:
        uid:         UID of the event to update.
        title:       New event title, or None to leave unchanged.
        dtstart:     New start datetime in ISO 8601, or None to leave unchanged.
        dtend:       New end datetime in ISO 8601, or None to leave unchanged.
        description: New description, or None to leave unchanged.

    Returns:
        Confirmation message.
    """
    try:
        obj, component = _find_event_by_uid(uid)
        _assert_claude_created(component)

        if title is not None:
            _set_or_add(component, "summary", vText(title))
        if dtstart is not None:
            _set_or_add(component, "dtstart", _parse_dt(dtstart))
        if dtend is not None:
            _set_or_add(component, "dtend", _parse_dt(dtend))
        if description is not None:
            _set_or_add(component, "description", vText(description))

        _save_component(obj, component)
        logger.info(f"update_event OK uid={uid!r}")
        return f"Event with UID '{uid}' updated successfully."
    except Exception as e:
        return _error_response("update_event", e)


@mcp.tool()
def update_todo(
    uid: str,
    title: str | None = None,
    due: str | None = None,
    description: str | None = None,
) -> str:
    """Update a Claude-created todo. Only provided fields are changed.

    Only todos created by Claude (tagged X-CLAUDE-CREATED) can be updated.

    Args:
        uid:         UID of the todo to update.
        title:       New todo title, or None to leave unchanged.
        due:         New due date/datetime in ISO 8601, or None to leave unchanged.
        description: New description, or None to leave unchanged.

    Returns:
        Confirmation message.
    """
    try:
        obj, component = _find_todo_by_uid(uid)
        _assert_claude_created(component)

        if title is not None:
            _set_or_add(component, "summary", vText(title))
        if due is not None:
            _set_or_add(component, "due", _parse_dt(due))
        if description is not None:
            _set_or_add(component, "description", vText(description))

        _save_component(obj, component)
        logger.info(f"update_todo OK uid={uid!r}")
        return f"Todo with UID '{uid}' updated successfully."
    except Exception as e:
        return _error_response("update_todo", e)


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mcp.run(transport="streamable-http")