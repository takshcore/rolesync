"""
RoleSync – Module 2: hotkey_ingestor.py
─────────────────────────────────────────────────────────────────────────────
Global hotkey listener that captures highlighted text and pipes it to the
LLM Reasoner pipeline.

Hotkey:  Ctrl + Shift + J
Flow:
  1. User highlights a job description in ANY application.
  2. User presses Ctrl + Shift + J.
  3. This module temporarily simulates Ctrl+C to populate the clipboard.
  4. Reads clipboard via pyperclip.
  5. Sanitises the raw text (regex-based noise removal).
  6. Emits the clean payload via a callback / Qt signal bridge.
  7. Restores the original clipboard content after capture.

Design notes
─────────────
* Runs in a dedicated daemon thread so it never blocks the Qt event loop.
* Exposes `HotkeyIngestor` with a single `on_capture` callback hook.
* The `QtBridge` class converts the thread-safe callback into a Qt signal
  that `desktop_app.ChatPanel` can consume directly.
"""

from __future__ import annotations

import re
import sys
import time
import threading
from dataclasses import dataclass, field
from typing import Callable

import keyboard
import pyperclip
from loguru import logger


# ── Logging setup ─────────────────────────────────────────────────────────────
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> │ <level>{level:<8}</level> │ {message}",
    level="DEBUG",
)
logger.add(
    "logs/ingestor.log",
    rotation="5 MB",
    retention="14 days",
    level="INFO",
    encoding="utf-8",
)


# ══════════════════════════════════════════════════════════════════════════════
# Sanitisation pipeline
# ══════════════════════════════════════════════════════════════════════════════

# Compile once at import time for speed
_RE_HTML_TAG       = re.compile(r"<[^>]+>")
_RE_URL            = re.compile(r"https?://\S+|www\.\S+")
_RE_EMAIL          = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
_RE_MULTI_NEWLINE  = re.compile(r"\n{3,}")
_RE_MULTI_SPACE    = re.compile(r"[ \t]{2,}")
_RE_BULLETS        = re.compile(r"^[\s]*[•·▪▸‣◦→\-–—]\s+", re.MULTILINE)
_RE_NON_PRINTABLE  = re.compile(r"[^\x09\x0A\x0D\x20-\x7E\u00A0-\uFFFF]")
_RE_COOKIE_NOTICE  = re.compile(
    r"(accept (all )?cookies?|privacy policy|cookie settings|gdpr)[^\n]*\n?",
    re.IGNORECASE,
)
_RE_APPLY_BUTTON   = re.compile(
    r"^(apply now|easy apply|quick apply|save job|share|report)[^\n]*\n?",
    re.IGNORECASE | re.MULTILINE,
)
_RE_SOCIAL_NOISE   = re.compile(
    r"^(follow us|connect with us|linkedin|twitter|facebook|instagram)[^\n]*\n?",
    re.IGNORECASE | re.MULTILINE,
)


def sanitise(raw: str) -> str:
    """
    Multi-stage text cleaning pipeline.

    Returns a clean, normalised job description string ready for the LLM.
    Returns an empty string if the payload is below the minimum length threshold.
    """
    if not raw or not raw.strip():
        return ""

    text = raw

    # 1. Strip HTML tags (pasted from browser address bar or rich-text apps)
    text = _RE_HTML_TAG.sub(" ", text)

    # 2. Remove non-printable / control characters (keep tabs, newlines)
    text = _RE_NON_PRINTABLE.sub("", text)

    # 3. Strip cookie / GDPR banners
    text = _RE_COOKIE_NOTICE.sub("", text)

    # 4. Strip UI noise (apply buttons, social links)
    text = _RE_APPLY_BUTTON.sub("", text)
    text = _RE_SOCIAL_NOISE.sub("", text)

    # 5. Optionally anonymise emails & URLs (keep structure, remove PII)
    text = _RE_URL.sub("[URL]", text)
    text = _RE_EMAIL.sub("[EMAIL]", text)

    # 6. Normalise bullet characters → standard hyphen
    text = _RE_BULLETS.sub("- ", text)

    # 7. Collapse excessive whitespace
    text = _RE_MULTI_SPACE.sub(" ", text)
    text = _RE_MULTI_NEWLINE.sub("\n\n", text)

    # 8. Strip leading/trailing whitespace per line
    lines = [line.rstrip() for line in text.splitlines()]
    text  = "\n".join(lines).strip()

    # 9. Minimum-length guard (< 80 chars is likely a UI element, not a JD)
    if len(text) < 80:
        logger.warning(f"Sanitised payload too short ({len(text)} chars) – discarding.")
        return ""

    logger.debug(f"Sanitised payload: {len(text)} chars retained.")
    return text


# ══════════════════════════════════════════════════════════════════════════════
# Clipboard helpers
# ══════════════════════════════════════════════════════════════════════════════

def _read_clipboard() -> str:
    """Return current clipboard text, or empty string on failure."""
    try:
        return pyperclip.paste() or ""
    except Exception as exc:
        logger.error(f"Clipboard read error: {exc}")
        return ""


def _write_clipboard(text: str) -> None:
    """Write text back to the clipboard, silently ignoring failures."""
    try:
        pyperclip.copy(text)
    except Exception as exc:
        logger.warning(f"Clipboard write error: {exc}")


def _capture_selection() -> str:
    """
    Simulate Ctrl+C to copy the user's current selection, wait briefly for
    the OS to populate the clipboard, then return the new clipboard content.

    The original clipboard is preserved and restored after capture.
    """
    original = _read_clipboard()

    # Clear clipboard so we can detect whether Ctrl+C produced new content
    _write_clipboard("")
    time.sleep(0.05)

    # Trigger OS-level copy without consuming the hotkey event
    keyboard.send("ctrl+c")
    time.sleep(0.18)   # allow OS clipboard pipeline to settle

    captured = _read_clipboard()

    # Restore original clipboard asynchronously (non-blocking)
    threading.Timer(0.5, _write_clipboard, args=(original,)).start()

    return captured


# ══════════════════════════════════════════════════════════════════════════════
# Data model
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class IngestedPayload:
    """Structured output of a successful hotkey capture event."""
    raw:       str
    clean:     str
    char_count:int = field(init=False)
    word_count:int = field(init=False)
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.char_count = len(self.clean)
        self.word_count = len(self.clean.split())

    def summary(self) -> str:
        return (
            f"📋 Captured {self.word_count} words / {self.char_count} chars "
            f"at {time.strftime('%H:%M:%S', time.localtime(self.timestamp))}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Optional Qt bridge (zero-import if PyQt6 not available in this process)
# ══════════════════════════════════════════════════════════════════════════════

try:
    from PyQt6.QtCore import QObject, pyqtSignal

    class QtBridge(QObject):
        """
        Thread-safe bridge between the keyboard listener thread and Qt slots.

        Usage:
            bridge = QtBridge()
            bridge.payload_ready.connect(chat_panel.on_job_ingested)
            ingestor = HotkeyIngestor(on_capture=bridge.emit_payload)
            ingestor.start()
        """
        payload_ready = pyqtSignal(object)   # emits IngestedPayload

        def emit_payload(self, payload: IngestedPayload) -> None:
            self.payload_ready.emit(payload)

    _QT_AVAILABLE = True

except ImportError:
    _QT_AVAILABLE = False
    logger.warning("PyQt6 not found – QtBridge unavailable; using raw callbacks only.")


# ══════════════════════════════════════════════════════════════════════════════
# Core ingestor
# ══════════════════════════════════════════════════════════════════════════════

class HotkeyIngestor:
    """
    Registers a global hotkey (default: Ctrl + Shift + J) and fires
    `on_capture(payload: IngestedPayload)` whenever a job description is
    successfully captured.

    Parameters
    ──────────
    hotkey      Keyboard shortcut string in `keyboard` library notation.
    on_capture  Callable invoked with an IngestedPayload on each successful
                capture.  Called from the keyboard listener thread – use the
                QtBridge if you need Qt thread safety.
    cooldown    Minimum seconds between successive captures (debounce).

    Example
    ───────
        def handle(payload):
            print(payload.summary())
            # forward to llm_reasoner …

        ingestor = HotkeyIngestor(on_capture=handle)
        ingestor.start()      # non-blocking – runs in background thread
        # …
        ingestor.stop()
    """

    _HOTKEY_DEFAULT = "ctrl+shift+j"

    def __init__(
        self,
        hotkey:     str = _HOTKEY_DEFAULT,
        on_capture: Callable[[IngestedPayload], None] | None = None,
        cooldown:   float = 1.5,
    ) -> None:
        self._hotkey      = hotkey
        self._on_capture  = on_capture
        self._cooldown    = cooldown
        self._last_fire   = 0.0
        self._running     = False
        self._lock        = threading.Lock()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Register the hotkey listener (non-blocking; runs in background)."""
        if self._running:
            logger.warning("HotkeyIngestor already running.")
            return
        self._running = True
        keyboard.add_hotkey(self._hotkey, self._fire, suppress=True)
        logger.info(f"HotkeyIngestor listening on [{self._hotkey.upper()}]")

    def stop(self) -> None:
        """Remove the hotkey listener."""
        keyboard.remove_hotkey(self._hotkey)
        self._running = False
        logger.info("HotkeyIngestor stopped.")

    def set_callback(self, fn: Callable[[IngestedPayload], None]) -> None:
        """Replace the capture callback at runtime."""
        with self._lock:
            self._on_capture = fn

    # ── Internal ──────────────────────────────────────────────────────────────

    def _fire(self) -> None:
        """Called by the keyboard library on each hotkey press."""
        now = time.time()
        if now - self._last_fire < self._cooldown:
            logger.debug("Hotkey debounced – too soon after last capture.")
            return
        self._last_fire = now

        # Run the potentially slow clipboard operation in a thread
        # so we never stall the keyboard hook
        threading.Thread(target=self._do_capture, daemon=True).start()

    def _do_capture(self) -> None:
        """Background thread: capture → sanitise → dispatch."""
        logger.info("Hotkey fired – capturing clipboard selection…")

        raw = _capture_selection()
        if not raw.strip():
            logger.warning("Clipboard empty after capture – nothing to process.")
            return

        clean = sanitise(raw)
        if not clean:
            logger.warning("Payload discarded after sanitisation.")
            return

        payload = IngestedPayload(raw=raw, clean=clean)
        logger.success(payload.summary())

        with self._lock:
            cb = self._on_capture

        if cb:
            try:
                cb(payload)
            except Exception as exc:
                logger.error(f"on_capture callback raised: {exc}")
        else:
            logger.warning("No on_capture callback set – payload dropped.")


# ══════════════════════════════════════════════════════════════════════════════
# Convenience factory
# ══════════════════════════════════════════════════════════════════════════════

def make_ingestor_with_qt_bridge(
    chat_push_fn: Callable[[str, str], None],
    reasoner_fn:  Callable[[IngestedPayload], None] | None = None,
    hotkey: str = HotkeyIngestor._HOTKEY_DEFAULT,
) -> tuple["HotkeyIngestor", "QtBridge | None"]:
    """
    Factory used by the main orchestrator to wire everything up:

        ingestor, bridge = make_ingestor_with_qt_bridge(
            chat_push_fn = app.chat.push_msg,
            reasoner_fn  = llm_reasoner.process,
        )
        ingestor.start()

    Returns (ingestor, bridge) where bridge may be None if PyQt6 is absent.
    """
    if _QT_AVAILABLE:
        bridge = QtBridge()

        def _on_payload(payload: IngestedPayload) -> None:
            # Notify the chat panel immediately
            chat_push_fn(
                f"📋 <b>Job description captured</b> — "
                f"{payload.word_count} words.<br>"
                f"<i>Running RAG + LLM pipeline…</i>",
                "system",
            )
            # Forward to reasoner (may be slow – runs in its own thread)
            if reasoner_fn:
                threading.Thread(
                    target=reasoner_fn, args=(payload,), daemon=True
                ).start()

        bridge.emit_payload = _on_payload          # type: ignore[method-assign]
        ingestor = HotkeyIngestor(hotkey=hotkey, on_capture=_on_payload)
        return ingestor, bridge

    else:
        # Fallback: direct callback, no Qt
        ingestor = HotkeyIngestor(
            hotkey=hotkey,
            on_capture=lambda p: (
                print(p.summary()),
                reasoner_fn(p) if reasoner_fn else None,
            ),
        )
        return ingestor, None


# ══════════════════════════════════════════════════════════════════════════════
# CLI smoke-test harness
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import pathlib
    pathlib.Path("logs").mkdir(exist_ok=True)

    print("=" * 60)
    print("  RoleSync – Hotkey Ingestor  (smoke-test mode)")
    print("  Highlight text anywhere, then press Ctrl + Shift + J")
    print("  Press Ctrl + C here to quit.")
    print("=" * 60)

    def _demo_callback(payload: IngestedPayload) -> None:
        print(f"\n{'─'*60}")
        print(payload.summary())
        print(f"{'─'*60}")
        print("CLEAN PREVIEW (first 400 chars):")
        print(payload.clean[:400])
        print(f"{'─'*60}\n")

    ingestor = HotkeyIngestor(on_capture=_demo_callback)
    ingestor.start()

    try:
        keyboard.wait()   # blocks until Ctrl+C
    except KeyboardInterrupt:
        ingestor.stop()
        print("\nIngestor shut down.")
