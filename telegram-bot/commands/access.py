"""/access — manage who can run what, entirely from Telegram.

One message, edited in place at every step, so the chat does not fill up with
dead menus:

    /access
      -> the user list, one button each
        -> a user:  [👤 Guest] [🛡️ Admin] [⛔ Block]
                    [⚙️ Per-command…] [🗑️ Forget] [⬅️ Back]
          -> per-command: one button per loaded command, tap to toggle

Owner-only, and not merely by convention: MIN_ROLE below gates both the command
and its buttons, and permissions.set_role refuses to touch the owner's own id.
There is no path through this menu that demotes you or promotes anyone to owner
-- that comes from ALLOWED_CHAT_ID in .env and nowhere else.
"""
import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

import permissions

COMMAND = "access"
DESCRIPTION = "Manage who can use the bot, and what they can run"
MIN_ROLE = "owner"
CALLBACK_PREFIX = "acl"

ROLE_ICON = {
    permissions.ADMIN: "🛡️",
    permissions.GUEST: "👤",
    permissions.BLOCKED: "⛔",
}


def _button(label: str, *parts: str) -> InlineKeyboardButton:
    """Build a button, keeping callback_data inside Telegram's 64-byte limit.

    Worst case here is "acl:cmd:<19-digit id>:<command>" -- comfortable, but the
    command name is the one unbounded piece, so it is worth not forgetting.
    """
    return InlineKeyboardButton(label, callback_data=":".join((CALLBACK_PREFIX,) + parts))


def _commands(context):
    return context.bot_data.get("commands", [])


# --------------------------------------------------------------------------- #
# views -- each returns (html_text, keyboard)
# --------------------------------------------------------------------------- #

def _user_list(context):
    users = permissions.all_users()
    if not users:
        return (
            "🔑 <b>Access</b>\n\nNobody else has access yet.\n\n"
            "When a stranger messages the bot you'll get a request here with "
            "Allow/Block buttons — that's how people get added.",
            InlineKeyboardMarkup([[_button("✅ Done", "close")]]),
        )

    rows = []
    for chat_id, entry in sorted(users.items(), key=lambda kv: kv[1].get("name", "")):
        role = entry.get("role", permissions.GUEST)
        name = entry.get("name") or entry.get("username") or chat_id
        extra = len(entry.get("grants") or {})
        suffix = f" ·{extra}✱" if extra else ""
        rows.append([_button(f"{ROLE_ICON.get(role, '•')} {name}{suffix}", "u", chat_id)])
    rows.append([_button("✅ Done", "close")])

    return (
        f"🔑 <b>Access</b>\n\n{len(users)} user(s) besides you. Tap one to change what they can do.",
        InlineKeyboardMarkup(rows),
    )


def _user_panel(chat_id: str, context):
    entry = permissions.user(chat_id)
    if entry is None:
        return _user_list(context)

    role = entry.get("role", permissions.GUEST)
    name = html.escape(entry.get("name") or str(chat_id))
    username = entry.get("username")
    handle = f" (@{html.escape(username)})" if username else ""
    grants = entry.get("grants") or {}

    lines = [
        f"{ROLE_ICON.get(role, '•')} <b>{name}</b>{handle}",
        f"chat id <code>{chat_id}</code>",
        f"role: <b>{role}</b>",
    ]
    if entry.get("added"):
        lines.append(f"added {html.escape(entry['added'])}")
    if grants:
        detail = ", ".join(
            f"{'✅' if allowed else '🔒'} /{command}" for command, allowed in sorted(grants.items())
        )
        lines.append(f"\n✱ set individually: {detail}")

    role_row = [
        _button(f"{'· ' if role == candidate else ''}{ROLE_ICON[candidate]} {candidate.title()}",
                "role", chat_id, candidate)
        for candidate in permissions.ASSIGNABLE
    ]
    rows = [role_row]
    if role == permissions.BLOCKED:
        lines.append("\n<i>Blocked. Nothing they send reaches the bot, whatever else is set.</i>")
    else:
        rows.append([_button("⚙️ Per-command…", "cmds", chat_id)])
    rows.append([_button("🗑️ Forget", "forget", chat_id), _button("⬅️ Back", "list")])

    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _command_panel(chat_id: str, context):
    entry = permissions.user(chat_id)
    if entry is None:
        return _user_list(context)

    name = html.escape(entry.get("name") or str(chat_id))
    rows, any_override = [], False
    for command in _commands(context):
        # Owner-only commands are not offered at all. permissions.may_run
        # refuses to let a grant override one anyway; leaving the button out
        # means you never press it and wonder why nothing happened.
        if command["min_role"] == permissions.OWNER:
            continue
        allowed = permissions.may_run(chat_id, command["name"], command["min_role"])
        override = permissions.grant_of(chat_id, command["name"]) is not None
        any_override = any_override or override
        label = f"{'✅' if allowed else '🔒'} /{command['name']}{' ✱' if override else ''}"
        rows.append([_button(label, "cmd", chat_id, command["name"])])

    rows.append([_button("↩️ Reset to role defaults", "reset", chat_id)])
    rows.append([_button("⬅️ Back", "u", chat_id)])

    text = [
        f"⚙️ <b>{name}</b> · {entry.get('role', permissions.GUEST)}",
        "\nTap a command to turn it on or off for this person only.",
    ]
    if any_override:
        text.append("<i>✱ set for them specifically, overriding their role.</i>")
    return "\n".join(text), InlineKeyboardMarkup(rows)


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #

async def handler(update, context):
    text, keyboard = _user_list(context)
    await update.effective_message.reply_html(text, reply_markup=keyboard)


async def callback(update, context):
    query = update.callback_query
    owner = update.effective_chat.id
    parts = query.data.split(":")
    action = parts[1]
    target = parts[2] if len(parts) > 2 else None

    if action == "close":
        await query.answer()
        await query.edit_message_text("🔑 Access — done.")
        return

    if action == "list":
        view = _user_list(context)

    elif action == "u":
        view = _user_panel(target, context)

    elif action == "role":
        role = parts[3]
        permissions.set_role(target, role, by=owner)
        await query.answer(f"Set to {role}")
        view = _user_panel(target, context)

    elif action == "cmds":
        view = _command_panel(target, context)

    elif action == "cmd":
        command = parts[3]
        entry = next((c for c in _commands(context) if c["name"] == command), None)
        if entry is None:
            await query.answer("That command is no longer loaded.", show_alert=True)
            view = _command_panel(target, context)
        elif entry["min_role"] == permissions.OWNER:
            await query.answer("Owner-only commands can't be handed out.", show_alert=True)
            view = _command_panel(target, context)
        else:
            desired = not permissions.may_run(target, command, entry["min_role"])
            # If the flip lands back on what the role would have given anyway,
            # clear the override instead of storing a redundant one. Keeps the
            # JSON honest about what was actually decided per-person.
            role = permissions.role_of(target) or permissions.GUEST
            by_role = permissions.RANK[role] >= permissions.RANK[entry["min_role"]]
            permissions.set_grant(
                target, command, None if desired == by_role else desired, by=owner
            )
            await query.answer(f"/{command} {'on' if desired else 'off'}")
            view = _command_panel(target, context)

    elif action == "reset":
        permissions.clear_grants(target, by=owner)
        await query.answer("Back to role defaults")
        view = _command_panel(target, context)

    elif action == "forget":
        permissions.forget(target, by=owner)
        await query.answer("Removed")
        view = _user_list(context)

    else:
        await query.answer("Unknown action.", show_alert=True)
        return

    if action not in ("role", "cmd", "reset", "forget"):
        await query.answer()

    text, keyboard = view
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    except BadRequest as exc:
        # Tapping the role someone already has redraws an identical message,
        # which Telegram rejects. The state is already correct, so there is
        # nothing to do but ignore it.
        if "not modified" not in str(exc).lower():
            raise
