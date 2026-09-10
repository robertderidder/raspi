"""Drive a long-running script from a Telegram chat.

Setup: create a bot with @BotFather to get a BOT_TOKEN, send it any message, then
read your CHAT_ID from https://api.telegram.org/bot<BOT_TOKEN>/getUpdates.
Put both in `.env file (gitignored) or set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.

    from telegram_bot import notify, send_photo, ask_yes_no, ask_int, watch

    with watch("new_method.py"):              # pings you on finish or crash
        for block in range(10):
            train()
            send_photo(fig, caption=f"block {block}")
            if not ask_yes_no("Run another block?", timeout=600, default=True):
                break
            epochs = ask_int("How many epochs?", minimum=1, maximum=100, default=3)

The ask_* functions block until you answer in Telegram. All of them take a
`timeout` (seconds) and a `default` that is returned if you don't answer in time,
so an unattended run keeps moving instead of hanging forever.

Only one process at a time may poll a given bot: Telegram hands each update to
whoever calls getUpdates first, so two scripts sharing a token will steal each
other's answers. Use a separate bot per concurrent run.
"""

from __future__ import annotations

import io
import os
import time
from contextlib import contextmanager
from datetime import datetime
from datetime import time as clock
from pathlib import Path
from typing import Callable, Sequence, TypeVar

import requests

__all__ = [
    "TelegramError",
    "configure",
    "in_quiet_hours",
    "notify",
    "send_message",
    "edit_message",
    "send_photo",
    "send_document",
    "ask_yes_no",
    "ask_choice",
    "ask_value",
    "ask_text",
    "ask_int",
    "ask_float",
    "watch",
]

T = TypeVar("T")

API_ROOT = "https://api.telegram.org"
MAX_TEXT_LEN = 4096      # Telegram hard limit for sendMessage
MAX_CAPTION_LEN = 1024   # Telegram hard limit for photo/document captions
_POLL_TIMEOUT = 25       # seconds per long-poll request
_UNSET = object()


class TelegramError(RuntimeError):
    """The Telegram API rejected a request, or was unreachable."""


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #

def _load_credentials() -> tuple[str | None, str | None]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        return token, str(chat_id)
    try:
        from tokens import BOT_TOKEN, CHAT_ID

        return str(BOT_TOKEN), str(CHAT_ID)
    except Exception:
        return token, chat_id  # may be (None, None); reported on first use


BOT_TOKEN, CHAT_ID = _load_credentials()

# Deliver without a notification sound during these hours. `silent=None` (the
# default everywhere) resolves to in_quiet_hours(); pass silent=True/False to
# override. Set QUIET_HOURS = None to always buzz.
QUIET_HOURS: tuple[clock, clock] | None = (clock(23, 59), clock(8, 0))

# The window is meaningless without a zone: this box runs UTC, so a naive
# datetime.now() would put "23:59-08:00" at 01:59-10:00 Dutch time and buzz you
# at midnight. Set TELEGRAM_TZ to travel.
QUIET_TZ = os.environ.get("TELEGRAM_TZ", "Europe/Amsterdam")

_session = requests.Session()
_offset = 0  # next unconsumed getUpdates id, shared by every ask_* call


def configure(
    bot_token: str | None = None,
    chat_id: str | int | None = None,
    *,
    quiet_hours: tuple[str, str] | None = _UNSET,  # type: ignore[assignment]
    timezone: str | None = None,
) -> None:
    """Override the settings found at import time.

    `quiet_hours=("23:00", "07:30")` shifts the window, `quiet_hours=None`
    disables it. `timezone` is any IANA name, e.g. "America/New_York".
    """
    global BOT_TOKEN, CHAT_ID, QUIET_HOURS, QUIET_TZ
    if bot_token is not None:
        BOT_TOKEN = str(bot_token)
    if chat_id is not None:
        CHAT_ID = str(chat_id)
    if timezone is not None:
        QUIET_TZ = timezone
    if quiet_hours is not _UNSET:
        if quiet_hours is None:
            QUIET_HOURS = None
        else:
            start, end = quiet_hours
            QUIET_HOURS = (clock.fromisoformat(start), clock.fromisoformat(end))


def _local_now() -> datetime:
    """Wall-clock time in QUIET_TZ, falling back to system local time."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(QUIET_TZ))
    except Exception as exc:  # missing tzdata, bad zone name
        print(f"[telegram] no timezone data for {QUIET_TZ!r} ({exc}); using system clock")
        return datetime.now()


def in_quiet_hours(now: datetime | None = None) -> bool:
    """True if notifications should currently be delivered silently."""
    if QUIET_HOURS is None:
        return False
    start, end = QUIET_HOURS
    current = (now or _local_now()).time()
    if start <= end:
        return start <= current < end
    return current >= start or current < end  # window wraps past midnight


def _resolve_silent(silent: bool | None) -> bool:
    return in_quiet_hours() if silent is None else silent


def _truncate(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------- #
# low-level API access
# --------------------------------------------------------------------------- #

def _call(
    method: str,
    params: dict | None = None,
    files: dict | None = None,
    *,
    timeout: float = 15,
    retries: int = 3,
):
    """POST to the bot API and return the `result` field.

    Retries transient failures (network errors, 5xx, and 429 rate limits, which
    carry Telegram's own retry_after hint). Client errors raise immediately --
    a malformed request will never succeed on retry.
    """
    if not BOT_TOKEN or not CHAT_ID:
        raise TelegramError(
            "No credentials: set BOT_TOKEN/CHAT_ID in tokens.py, export "
            "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID, or call telegram.configure()."
        )

    url = f"{API_ROOT}/bot{BOT_TOKEN}/{method}"
    last_error: object = None

    for attempt in range(retries):
        try:
            if files is not None:
                response = _session.post(url, data=params, files=files, timeout=timeout)
            else:
                response = _session.post(url, json=params, timeout=timeout)
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(min(2**attempt, 10))
            continue

        try:
            body = response.json()
        except ValueError:
            body = {}

        if response.ok and body.get("ok"):
            return body.get("result")

        description = body.get("description", response.text[:200])
        if response.status_code == 429:
            wait = body.get("parameters", {}).get("retry_after", 5)
            last_error = f"rate limited ({description})"
            time.sleep(wait)
            continue
        if response.status_code >= 500:
            last_error = f"{response.status_code} {description}"
            time.sleep(min(2**attempt, 10))
            continue
        raise TelegramError(f"{method} failed: {response.status_code} {description}")

    raise TelegramError(f"{method} failed after {retries} attempts: {last_error}")


def _authorised(update: dict) -> bool:
    """True if the update came from the configured chat.

    Anyone can message a public bot; without this an outsider could answer a
    prompt and steer the run.
    """
    payload = update.get("callback_query", {}).get("message") or update.get("message", {})
    return str(payload.get("chat", {}).get("id")) == str(CHAT_ID)


def _drain() -> None:
    """Discard pending updates so stale clicks can't answer the next question."""
    global _offset
    try:
        updates = _call("getUpdates", {"offset": _offset, "timeout": 0}, timeout=15)
    except TelegramError as exc:
        print(f"[telegram] could not flush old updates: {exc}")
        return
    if updates:
        _offset = updates[-1]["update_id"] + 1


def _wait_for(match: Callable[[dict], tuple | None], deadline: float | None):
    """Long-poll until `match(update)` returns a 1-tuple; None on timeout.

    `match` returns None to ignore an update and keep waiting, which is how the
    ask_* helpers re-prompt after unparseable input. The result is boxed in a
    tuple so that falsy answers (0, "", False) are distinguishable from "no
    answer yet".
    """
    global _offset

    while True:
        poll = _POLL_TIMEOUT
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            poll = max(0, min(_POLL_TIMEOUT, int(remaining)))

        try:
            updates = _call(
                "getUpdates",
                {"offset": _offset, "timeout": poll},
                timeout=poll + 15,
            )
        except TelegramError as exc:
            print(f"[telegram] poll failed, retrying: {exc}")
            time.sleep(3)
            continue

        for update in updates or []:
            _offset = update["update_id"] + 1
            if not _authorised(update):
                continue
            result = match(update)
            if result is not None:
                return result


def _ack(callback: dict, text: str | None = None) -> None:
    """Stop the spinner on the sender's button press (must happen within ~10s)."""
    try:
        _call(
            "answerCallbackQuery",
            {"callback_query_id": callback["id"], "text": text or ""},
            retries=1,
        )
    except TelegramError:
        pass  # cosmetic only


# --------------------------------------------------------------------------- #
# sending
# --------------------------------------------------------------------------- #

def send_message(
    text: str,
    *,
    parse_mode: str | None = None,
    silent: bool | None = None,
    reply_markup: dict | None = None,
    reply_to: int | None = None,
) -> int:
    """Send `text`, returning its message_id (usable with edit_message).

    `parse_mode` is None (plain text) by default: with "Markdown" or "HTML" an
    unbalanced `_`, `*` or `<` anywhere in the text -- a stray underscore in a
    run name, a traceback -- makes Telegram reject the whole message.
    `silent=None` delivers without a sound during QUIET_HOURS; True/False force it.
    """
    params: dict = {
        "chat_id": CHAT_ID,
        "text": _truncate(text, MAX_TEXT_LEN),
        "disable_notification": _resolve_silent(silent),
    }
    if parse_mode:
        params["parse_mode"] = parse_mode
    if reply_markup:
        params["reply_markup"] = reply_markup
    if reply_to:
        params["reply_to_message_id"] = reply_to
    return _call("sendMessage", params)["message_id"]


def edit_message(message_id: int, text: str, *, parse_mode: str | None = None) -> None:
    """Rewrite an already-sent message -- a progress line that updates in place
    beats a hundred new messages."""
    params: dict = {
        "chat_id": CHAT_ID,
        "message_id": message_id,
        "text": _truncate(text, MAX_TEXT_LEN),
    }
    if parse_mode:
        params["parse_mode"] = parse_mode
    _call("editMessageText", params)


def _image_bytes(image, dpi: int) -> tuple[bytes, str]:
    """Normalise a path / bytes / matplotlib Figure into (data, filename)."""
    if hasattr(image, "savefig"):  # matplotlib Figure
        buffer = io.BytesIO()
        image.savefig(buffer, format="png", dpi=dpi, bbox_inches="tight")
        return buffer.getvalue(), "figure.png"
    if isinstance(image, (bytes, bytearray)):
        return bytes(image), "image.png"
    path = Path(image)
    if not path.is_file():
        raise TelegramError(f"No such image: {path}")
    return path.read_bytes(), path.name


def send_photo(
    image,
    *,
    caption: str | None = None,
    parse_mode: str | None = None,
    silent: bool | None = None,
    as_document: bool = False,
    dpi: int = 150,
) -> int:
    """Send a local path, raw PNG bytes, or a matplotlib Figure.

    `as_document=True` skips Telegram's lossy re-compression, which otherwise
    smears thin lines and text in plots. Photos are capped at 10 MB by the API.
    """
    data, filename = _image_bytes(image, dpi)
    params: dict = {"chat_id": CHAT_ID, "disable_notification": _resolve_silent(silent)}
    if caption:
        params["caption"] = _truncate(caption, MAX_CAPTION_LEN)
    if parse_mode:
        params["parse_mode"] = parse_mode

    if as_document:
        return _call("sendDocument", params, files={"document": (filename, data)})["message_id"]
    return _call("sendPhoto", params, files={"photo": (filename, data)})["message_id"]


def send_document(path, *, caption: str | None = None, silent: bool | None = None) -> int:
    """Send any file (config, .npz, log) as an attachment."""
    path = Path(path)
    if not path.is_file():
        raise TelegramError(f"No such file: {path}")
    params: dict = {"chat_id": CHAT_ID, "disable_notification": _resolve_silent(silent)}
    if caption:
        params["caption"] = _truncate(caption, MAX_CAPTION_LEN)
    files = {"document": (path.name, path.read_bytes())}
    return _call("sendDocument", params, files=files)["message_id"]


def notify(text: str, **kwargs) -> bool:
    """Fire-and-forget send: prints a warning instead of raising.

    Use this where a failed notification must not take the run down with it --
    end-of-run pings, and especially inside `except` blocks, where a raising
    notifier would replace your real traceback with a TelegramError.
    """
    try:
        send_message(text, **kwargs)
        return True
    except Exception as exc:
        print(f"[telegram] notification not sent: {exc}")
        return False


# --------------------------------------------------------------------------- #
# asking
# --------------------------------------------------------------------------- #

def ask_yes_no(
    question: str,
    *,
    yes_label: str = "✅ Yes",
    no_label: str = "❌ No",
    timeout: float | None = None,
    default: T | None = None,
    parse_mode: str | None = None,
    silent: bool | None = None,
) -> bool | T | None:
    """Ask `question` with Yes/No buttons and block until one is pressed.

    Returns True/False, or `default` if `timeout` seconds pass with no answer.
    The message is edited afterwards to record the choice, so the buttons can't
    be pressed twice.

    Note that `silent` defaults to quiet hours like everything else, so an
    overnight question with timeout=None will wait silently and forever. Give it
    a timeout and a default, or pass silent=False to insist on being woken.
    """
    _drain()
    markup = {
        "inline_keyboard": [
            [
                {"text": yes_label, "callback_data": "yes"},
                {"text": no_label, "callback_data": "no"},
            ]
        ]
    }
    message_id = send_message(question, parse_mode=parse_mode, reply_markup=markup, silent=silent)
    print(f"[telegram] waiting for yes/no: {question!r}")

    def match(update: dict):
        callback = update.get("callback_query")
        if not callback or callback["message"]["message_id"] != message_id:
            return None
        choice = callback["data"]
        _ack(callback, f"Selected: {choice}")
        return (choice == "yes",)

    deadline = None if timeout is None else time.monotonic() + timeout
    result = _wait_for(match, deadline)

    if result is None:
        _safe_edit(message_id, f"{question}\n\n⏳ No answer in {timeout:.0f}s — using default: {default}")
        print(f"[telegram] timed out after {timeout}s, using default: {default}")
        return default

    answer = result[0]
    _safe_edit(message_id, f"{question}\n\n→ {yes_label if answer else no_label}")
    return answer


def ask_choice(
    question: str,
    options: Sequence[T] | dict,
    *,
    timeout: float | None = None,
    default: object = _UNSET,
    columns: int = 2,
    parse_mode: str | None = None,
    silent: bool | None = None,
) -> T:
    """Ask `question` with one button per option; returns the chosen option.

    `options` is a sequence (label == value) or a {label: value} dict. Buttons
    carry the option's index rather than its value, so values may be any Python
    object and are not subject to Telegram's 64-byte callback_data limit.
    Returns `default` on timeout, or None if no default was given.
    """
    _drain()
    if isinstance(options, dict):
        labels, values = list(options.keys()), list(options.values())
    else:
        labels, values = [str(o) for o in options], list(options)
    if not labels:
        raise ValueError("ask_choice needs at least one option")

    buttons = [
        {"text": _truncate(label, 60), "callback_data": str(index)}
        for index, label in enumerate(labels)
    ]
    keyboard = [buttons[i : i + columns] for i in range(0, len(buttons), columns)]
    message_id = send_message(
        question,
        parse_mode=parse_mode,
        reply_markup={"inline_keyboard": keyboard},
        silent=silent,
    )
    print(f"[telegram] waiting for choice: {question!r}")

    def match(update: dict):
        callback = update.get("callback_query")
        if not callback or callback["message"]["message_id"] != message_id:
            return None
        index = int(callback["data"])
        _ack(callback, f"Selected: {labels[index]}")
        return (index,)

    deadline = None if timeout is None else time.monotonic() + timeout
    result = _wait_for(match, deadline)

    if result is None:
        fallback = None if default is _UNSET else default
        _safe_edit(message_id, f"{question}\n\n⏳ No answer — using default: {fallback}")
        return fallback

    index = result[0]
    _safe_edit(message_id, f"{question}\n\n→ {labels[index]}")
    return values[index]


def ask_value(
    prompt: str,
    parse: Callable[[str], T] = str,
    *,
    timeout: float | None = None,
    default: object = _UNSET,
    attempts: int | None = None,
    hint: str | None = None,
    cancel_words: Sequence[str] = ("/cancel", "/stop"),
    silent: bool | None = None,
) -> T:
    """Ask for a typed reply and run it through `parse`; the general ask.

    `parse` converts the reply text and raises ValueError (message shown to you)
    on bad input -- so a typo re-prompts rather than crashing the run. The other
    ask_* input helpers are thin wrappers over this.

        ask_value("Learning rate?", float)
        ask_value("Config name?", lambda s: s if s.endswith(".yaml")
                                   else (_ for _ in ()).throw(ValueError("need .yaml")))

    Returns `default` on timeout, on /cancel, or once `attempts` replies have
    all failed to parse; with no default, those cases raise TimeoutError,
    KeyboardInterrupt and ValueError respectively.
    """
    _drain()
    text = prompt if not hint else f"{prompt}\n\n({hint})"
    send_message(f"{text}\n\nReply with a message, or /cancel.", silent=silent)
    print(f"[telegram] waiting for input: {prompt!r}")

    tries = 0

    def match(update: dict):
        nonlocal tries
        message = update.get("message")
        if not message or "text" not in message:
            return None
        reply = message["text"].strip()

        if reply.lower() in cancel_words:
            return ("__cancelled__",)

        try:
            return ("__ok__", parse(reply))
        except Exception as exc:
            tries += 1
            if attempts is not None and tries >= attempts:
                return ("__exhausted__", exc)
            notify(f"⚠️ {exc}\n\nTry again, or /cancel.")
            return None  # keep waiting for a better answer

    deadline = None if timeout is None else time.monotonic() + timeout
    result = _wait_for(match, deadline)

    if result is None:
        if default is not _UNSET:
            notify(f"⏳ No answer in {timeout:.0f}s — using default: {default}")
            return default
        raise TimeoutError(f"No reply to {prompt!r} within {timeout}s")

    if result[0] == "__ok__":
        return result[1]
    if result[0] == "__cancelled__":
        if default is not _UNSET:
            notify(f"Cancelled — using default: {default}")
            return default
        raise KeyboardInterrupt(f"Cancelled at prompt: {prompt!r}")

    # attempts exhausted
    if default is not _UNSET:
        notify(f"❌ {attempts} invalid replies — using default: {default}")
        return default
    raise ValueError(f"No valid reply to {prompt!r} after {attempts} attempts: {result[1]}")


def ask_text(prompt: str, *, allowed: Sequence[str] | None = None, **kwargs) -> str:
    """Ask for free text, optionally restricted to `allowed` values."""

    def parse(text: str) -> str:
        if allowed is not None and text not in allowed:
            raise ValueError(f"Pick one of: {', '.join(allowed)}")
        return text

    return ask_value(prompt, parse, **kwargs)


def ask_int(
    prompt: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    **kwargs,
) -> int:
    """Ask for a whole number, re-prompting until it parses and is in range."""

    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise ValueError(f"{text!r} is not a whole number.")
        if minimum is not None and value < minimum:
            raise ValueError(f"Must be at least {minimum} (got {value}).")
        if maximum is not None and value > maximum:
            raise ValueError(f"Must be at most {maximum} (got {value}).")
        return value

    bounds = [f"≥ {minimum}" if minimum is not None else "", f"≤ {maximum}" if maximum is not None else ""]
    hint = kwargs.pop("hint", None) or (", ".join(b for b in bounds if b) or None)
    return ask_value(prompt, parse, hint=hint, **kwargs)


def ask_float(
    prompt: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    **kwargs,
) -> float:
    """Ask for a decimal number, re-prompting until it parses and is in range."""

    def parse(text: str) -> float:
        try:
            value = float(text)
        except ValueError:
            raise ValueError(f"{text!r} is not a number.")
        if minimum is not None and value < minimum:
            raise ValueError(f"Must be at least {minimum} (got {value}).")
        if maximum is not None and value > maximum:
            raise ValueError(f"Must be at most {maximum} (got {value}).")
        return value

    return ask_value(prompt, parse, **kwargs)


def _safe_edit(message_id: int, text: str) -> None:
    """Edit a message, ignoring failures -- these edits are cosmetic."""
    try:
        edit_message(message_id, text)
    except TelegramError as exc:
        print(f"[telegram] could not edit message: {exc}")


# --------------------------------------------------------------------------- #
# convenience
# --------------------------------------------------------------------------- #

@contextmanager
def watch(label: str, *, notify_start: bool = False, reraise: bool = True):
    """Ping you when the wrapped block finishes or crashes, with its duration.

        with watch("new_method.py"):
            main()

    Exceptions are reported and then re-raised (`reraise=False` to swallow).
    """
    if notify_start:
        notify(f"▶️ {label} started")
    started = time.monotonic()
    try:
        yield
    except BaseException as exc:
        elapsed = time.monotonic() - started
        notify(f"❌ {label} failed after {_duration(elapsed)}\n\n{type(exc).__name__}: {exc}")
        if reraise:
            raise
    else:
        notify(f"✅ {label} finished in {_duration(time.monotonic() - started)}")


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds // 60:.0f}m {seconds % 60:.0f}s"
    return f"{seconds // 3600:.0f}h {(seconds % 3600) // 60:.0f}m"


# --------------------------------------------------------------------------- #
# demo -- `python telegram_bot.py` exercises every public function once
# --------------------------------------------------------------------------- #

_CYAN, _DIM, _BOLD, _OFF = "\033[36m", "\033[2m", "\033[1m", "\033[0m"


def _step(n: int, total: int, label: str) -> None:
    print(f"{_CYAN}{_BOLD}[{n}/{total}]{_OFF} {label}")


DISPLAY_RANGE_DB = -45  # demo B-mode display floor


def _bmode(seed: int = 0, size: int = 256):
    """A fake B-mode frame: speckle + anechoic cysts, log-compressed to dB.

    Gaussian scatterers convolved with an anisotropic PSF (tighter axially than
    laterally, as a real probe is), so it reads like a scan rather than noise.
    Normalising to the 99.9th percentile rather than the max stops the two point
    targets from dragging the whole speckle field down into the mud.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    scatterers = rng.normal(size=(size, size))
    n_blobs = np.random.uniform(3, 6)
    cy = np.random.uniform(0, size, int(n_blobs))
    cx = np.random.uniform(0, size, int(n_blobs))
    radius = np.random.uniform(10, 30, int(n_blobs))
    rows, cols = np.mgrid[0:size, 0:size]
    for cy, cx, radius in zip(cy, cx, radius):
        scatterers[(rows - cy) ** 2 + (cols - cx) ** 2 < radius**2] *= 0.10  # cysts
    for cy, cx in [(60, 195), (205, 65)]:
        scatterers[cy - 2 : cy + 2, cx - 2 : cx + 2] *= 9  # point targets

    axial = np.exp(-0.5 * ((np.arange(size) - size // 2) / 0.8) ** 2)
    lateral = np.exp(-0.5 * ((np.arange(size) - size // 2) / 1.8) ** 2)
    psf = np.outer(axial, lateral)
    psf /= psf.sum()

    envelope = np.abs(
        np.fft.ifft2(np.fft.fft2(scatterers) * np.fft.fft2(np.fft.ifftshift(psf)))
    )
    envelope /= np.percentile(envelope, 99.9) * 1.6
    return 20 * np.log10(envelope + 1e-4)


def _figure(frames: list, cmap: str, title: str):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.style.use("dark_background")
    fig, axes = plt.subplots(1, len(frames), figsize=(3.2 * len(frames), 3.6), squeeze=False)
    for ax, (frame, label) in zip(axes.flat, frames):
        ax.imshow(frame, cmap=cmap, vmin=DISPLAY_RANGE_DB, vmax=0, aspect="equal")
        ax.set_title(label, fontsize=9, color="#7dd3fc")
        ax.set_xticks([])
        ax.set_yticks([])
    fig.suptitle(title, fontsize=11, color="white")
    fig.tight_layout()
    return fig


def _demo() -> None:
    """Run every public function once, in order. Safe to run repeatedly."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    total = 9
    local = _local_now()
    quiet = in_quiet_hours()
    print(f"\n{_BOLD}telegram_bot.py demo{_OFF}")
    print(f"{_DIM}  chat        {CHAT_ID}")
    print(f"  local time  {local.strftime('%H:%M %Z')}")
    print(f"  quiet hours {QUIET_HOURS[0]}-{QUIET_HOURS[1]} -> silent={quiet}{_OFF}\n")

    with watch("telegram_bot demo", notify_start=True):
        _step(1, total, "send_message")
        send_message(
            "🛰️  telegram_bot.py\n\n"
            "Everything below is Python driving this chat.\n"
            f"Local time {local.strftime('%H:%M %Z')} — "
            f"quiet hours are {'ON, so this arrived silently' if quiet else 'OFF, so this buzzed'}."
        )

        _step(2, total, "edit_message (progress bar, in place)")
        bar_id = send_message("Reconstructing…")
        for pct in range(0, 101, 10):
            filled = pct * 24 // 100
            edit_message(
                bar_id,
                f"🌀 Diffusion posterior sampling\n\n"
                f"{'█' * filled}{'░' * (24 - filled)}  {pct:3d}%\n"
                f"step {pct * 5:3d}/500   loss {2.4 * (1 - pct / 130):.3f}",
            )
            time.sleep(0.3)
        edit_message(bar_id, "✅ Converged in 500 steps — final loss 0.412")

        _step(3, total, "send_photo (matplotlib Figure, no temp file)")
        fig = _figure([(_bmode(0), "posterior sample")], "gray", "Reconstruction")
        send_photo(fig, caption="B-mode phantom: speckle, 3 cysts, 2 point targets")
        plt.close(fig)

        _step(4, total, "ask_choice (buttons -> re-render)")
        cmap = ask_choice(
            "Pick a colormap and I'll re-render:",
            ["gray", "viridis", "magma", "inferno"],
            timeout=60,
            default="magma",
        )
        fig = _figure([(_bmode(0), cmap)], cmap, f"Re-rendered with '{cmap}'")
        send_photo(fig, caption=f"🎨 cmap='{cmap}', as you ordered")
        plt.close(fig)

        _step(5, total, "ask_int (validated, re-prompts on typos)")
        n = ask_int(
            "How many posterior samples? (1-4)",
            minimum=1,
            maximum=4,
            timeout=60,
            default=3,
        )
        fig = _figure(
            [(_bmode(s), f"sample {s + 1}") for s in range(n)], cmap, f"{n} posterior samples"
        )
        send_photo(fig, caption=f"🎲 {n} sample{'s' if n > 1 else ''} from the same posterior")
        plt.close(fig)

        _step(6, total, "ask_float + ask_text + ask_value (the general one)")
        speed = ask_float(
            "Sound speed in m/s? (1400-1700)",
            minimum=1400,
            maximum=1700,
            timeout=60,
            default=1540.0,
        )
        mode = ask_text(
            "Which dataset? cardiac / carotid",
            allowed=["cardiac", "carotid"],
            timeout=60,
            default="cardiac",
        )
        name = ask_value(
            "Name this run (must end in .yaml):",
            lambda s: s if s.endswith(".yaml") else _reject("needs a .yaml suffix"),
            timeout=60,
            default="demo_run.yaml",
        )
        send_message(f"⚙️  c={speed} m/s · dataset={mode} · config={name}")

        _step(7, total, "ask_yes_no")
        keep = ask_yes_no(
            "Save these results?", timeout=60, default=True,
            yes_label="💾 Save", no_label="🗑️ Discard",
        )

        _step(8, total, "send_document")
        import tempfile

        summary = (
            "telegram_bot.py demo\n"
            "====================\n"
            f"time        {local.strftime('%Y-%m-%d %H:%M %Z')}\n"
            f"quiet hours {QUIET_HOURS[0]}-{QUIET_HOURS[1]} ({QUIET_TZ}) -> silent={quiet}\n"
            f"colormap    {cmap}\n"
            f"samples     {n}\n"
            f"sound speed {speed} m/s\n"
            f"dataset     {mode}\n"
            f"config      {name}\n"
            f"saved       {keep}\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".txt", prefix="demo_", delete=False) as fh:
            fh.write(summary)
            path = fh.name
        send_document(path, caption="📄 Run summary")
        Path(path).unlink(missing_ok=True)

        _step(9, total, "notify (never raises) + watch (this block)")
        notify("👋 That's every function. `watch` reports the total below.")

    print(f"\n{_BOLD}Done.{_OFF} {_DIM}Nine functions, one chat.{_OFF}\n")


def _reject(message: str):
    """Raise from inside a lambda -- ask_value shows the text and re-prompts."""
    raise ValueError(message)


if __name__ == "__main__":
    try:
        _demo()
    except KeyboardInterrupt:
        print("\ncancelled")
