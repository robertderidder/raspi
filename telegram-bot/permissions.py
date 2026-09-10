"""Who is allowed to run what.

Four roles, ranked. Everything below `owner` lives in `allowed_chats.json`
next to this file; the owner does not.

    blocked  <  guest  <  admin  <  owner

    owner    You. Read from ALLOWED_CHAT_ID in .env, checked before the state
             file is ever consulted, and never written to it. Delete, corrupt
             or truncate allowed_chats.json and you still have full access --
             everyone else simply loses theirs. That is the point: there is no
             sequence of button presses, and no bad state on disk, that can
             lock you out of your own Pi or promote someone above you.
    admin    A trusted second person. Everything except changing permissions.
    guest    A roommate. Commands that declare MIN_ROLE = "guest".
    blocked  A denied stranger. Dropped silently, with no further prompts.

On top of the roles sit per-user, per-command **grants** -- an explicit
True/False for one command that overrides the role in either direction. That
is how a guest gets /update without being promoted to admin, or an admin loses
one command without being demoted.

Resolution order, in `may_run`:

    owner -> unknown -> blocked -> grant -> role rank vs the command's MIN_ROLE

`bot.py` treats "unknown" specially: it is not a refusal, it starts the access
request flow.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime

log = logging.getLogger("permissions")

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "allowed_chats.json")
AUDIT_PATH = os.path.join(HERE, "scripts", "logs", "access.log")

OWNER = "owner"
ADMIN = "admin"
GUEST = "guest"
BLOCKED = "blocked"

# Comparable ranks. A command's MIN_ROLE is met when the caller's rank is >=.
RANK = {BLOCKED: 0, GUEST: 1, ADMIN: 2, OWNER: 3}

# Roles the owner can hand out from /access. `owner` is deliberately absent:
# it comes from .env and nothing in the UI can grant it.
ASSIGNABLE = (GUEST, ADMIN, BLOCKED)

# What a command module gets if it does not declare MIN_ROLE. Deliberately
# `admin`, not `guest`: dropping a new file into commands/ should not silently
# hand it to the roommate. Opening a command up is then a visible one-line
# decision in that file.
DEFAULT_MIN_ROLE = ADMIN

_OWNER_ID: int = 0
_users: dict[str, dict] = {}


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #

def init(owner_id: int) -> None:
    """Set the owner and load the state file. Call once, from bot.py."""
    global _OWNER_ID
    _OWNER_ID = int(owner_id)
    _load()


def _load() -> None:
    """Read allowed_chats.json into memory.

    A missing file is the normal first-run case. A corrupt one is not, but it
    must not take the bot down with it -- losing the guest list is recoverable
    from Telegram in a few taps, a bot that will not start is not.
    """
    global _users
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        _users = {}
        return
    except (OSError, ValueError) as exc:
        log.error("Could not read %s (%s) -- starting with no users", STATE_PATH, exc)
        _users = {}
        return

    _users = {str(k): v for k, v in (data.get("users") or {}).items()}
    # The owner is structural. If an id matching the owner ever ends up in the
    # file, it is stale or hand-edited -- drop it rather than let a stored
    # `blocked` row shadow the real owner.
    _users.pop(str(_OWNER_ID), None)
    log.info("Loaded %d known user(s) from %s", len(_users), STATE_PATH)


def _save() -> None:
    """Write the state file atomically.

    tempfile in the same directory + os.replace, so an SD card losing power
    mid-write leaves either the old file or the new one, never half of either.

    No locking: python-telegram-bot runs this bot as a single process on one
    event loop, so two writes can never interleave. If that ever stops being
    true, this is the function that needs a lock.
    """
    payload = {"version": 1, "users": _users}
    directory = os.path.dirname(STATE_PATH)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, prefix=".allowed_chats.", suffix=".tmp",
        delete=False,
    )
    try:
        with handle as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(handle.name, STATE_PATH)
    except OSError as exc:
        log.error("Could not write %s: %s", STATE_PATH, exc)
        try:
            os.unlink(handle.name)
        except OSError:
            pass


def _audit(line: str) -> None:
    """Append one line to scripts/logs/access.log.

    Every permission change goes here. When someone can run something you did
    not expect, this file is the answer -- the JSON only shows the end state.
    """
    try:
        os.makedirs(os.path.dirname(AUDIT_PATH), exist_ok=True)
        with open(AUDIT_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().astimezone().isoformat(timespec='seconds')} | {line}\n")
    except OSError as exc:
        log.warning("Could not write the access log: %s", exc)


# --------------------------------------------------------------------------- #
# queries
# --------------------------------------------------------------------------- #

def owner_id() -> int:
    return _OWNER_ID


def is_owner(chat_id: int | str) -> bool:
    return str(chat_id) == str(_OWNER_ID)


def is_known(chat_id: int | str) -> bool:
    """True if this id has ever been decided on -- approved, or blocked."""
    return is_owner(chat_id) or str(chat_id) in _users


def role_of(chat_id: int | str) -> str | None:
    """The caller's role, or None if they are a stranger."""
    if is_owner(chat_id):
        return OWNER
    user = _users.get(str(chat_id))
    return user.get("role", GUEST) if user else None


def rank_of(chat_id: int | str) -> int:
    role = role_of(chat_id)
    return RANK.get(role, -1) if role else -1


def user(chat_id: int | str) -> dict | None:
    return _users.get(str(chat_id))


def all_users() -> dict[str, dict]:
    """Every known non-owner, newest last. Read-only view for /access."""
    return dict(_users)


def grant_of(chat_id: int | str, command: str) -> bool | None:
    """The per-command override for this user, or None if they have none."""
    entry = _users.get(str(chat_id))
    if not entry:
        return None
    value = (entry.get("grants") or {}).get(command)
    return value if isinstance(value, bool) else None


def may_run(chat_id: int | str, command: str, min_role: str = DEFAULT_MIN_ROLE) -> bool:
    """The whole policy, in resolution order."""
    if is_owner(chat_id):
        return True
    role = role_of(chat_id)
    if role is None or role == BLOCKED:
        return False
    # An owner-only command is owner-only, full stop. Without this, a grant
    # would outrank the role check below and /access -- the command that hands
    # out roles -- could be given away to a guest, who could then promote
    # themselves. Grants tune what a role already means; they are not a way up.
    if min_role == OWNER:
        return False
    override = grant_of(chat_id, command)
    if override is not None:
        return override
    return RANK[role] >= RANK.get(min_role, RANK[ADMIN])


def describe(chat_id: int | str) -> str:
    """A short human label, for /access buttons and log lines."""
    if is_owner(chat_id):
        return "you (owner)"
    entry = _users.get(str(chat_id))
    if not entry:
        return str(chat_id)
    name = entry.get("name") or entry.get("username") or str(chat_id)
    return f"{name} ({entry.get('role', GUEST)})"


# --------------------------------------------------------------------------- #
# mutations -- all of these are reachable only from an owner-guarded handler
# --------------------------------------------------------------------------- #

def set_role(chat_id: int | str, role: str, *, by: int, name: str = "",
             username: str = "") -> None:
    """Create or update a user's role. Refuses to touch the owner."""
    if role not in ASSIGNABLE:
        raise ValueError(f"Cannot assign role {role!r}")
    key = str(chat_id)
    if is_owner(key):
        raise ValueError("The owner's role is set by .env and cannot be changed here")

    entry = _users.get(key)
    if entry is None:
        entry = {
            "role": role,
            "name": name,
            "username": username,
            "added": datetime.now().astimezone().isoformat(timespec="seconds"),
            "added_by": int(by),
            "grants": {},
        }
        _users[key] = entry
        _audit(f"add {key} ({name or '?'}@{username or '?'}) as {role} by {by}")
    else:
        was = entry.get("role")
        entry["role"] = role
        if name:
            entry["name"] = name
        if username:
            entry["username"] = username
        _audit(f"role {key} {was} -> {role} by {by}")
    _save()


def set_grant(chat_id: int | str, command: str, allowed: bool | None, *, by: int) -> None:
    """Override one command for one user. `allowed=None` clears the override."""
    entry = _users.get(str(chat_id))
    if entry is None:
        return
    grants = entry.setdefault("grants", {})
    if allowed is None:
        grants.pop(command, None)
        _audit(f"grant {chat_id} /{command} cleared by {by}")
    else:
        grants[command] = bool(allowed)
        _audit(f"grant {chat_id} /{command} = {bool(allowed)} by {by}")
    _save()


def clear_grants(chat_id: int | str, *, by: int) -> None:
    """Drop every override, putting the user back on their role's defaults."""
    entry = _users.get(str(chat_id))
    if entry is None:
        return
    entry["grants"] = {}
    _audit(f"grants {chat_id} reset to role defaults by {by}")
    _save()


def forget(chat_id: int | str, *, by: int) -> None:
    """Remove a user entirely.

    Not the same as blocking: a forgotten id is a stranger again, so their next
    message raises a fresh access request. Block instead to make them go away.
    """
    if _users.pop(str(chat_id), None) is not None:
        _audit(f"forget {chat_id} by {by}")
        _save()
