"""
lifecycle_watchdog.py — Module 7: The State Machine
=====================================================
RoleSync Autonomous Job Application Agent
------------------------------------------
This module is the **persistence and scheduling backbone** of RoleSync.
It owns three distinct concerns that together form a closed-loop lifecycle:

1. **Persistence** (`JobDatabase`): An SQLite-backed record store that tracks
   every job from first discovery through archival.  Every field a downstream
   module might need (paths, deadlines, submission results) lives here so the
   rest of the system stays stateless.

2. **Notifications** (`NotificationService`): Desktop alerts that fire when a
   pending application is within 48 hours of its deadline, using `plyer` for
   cross-platform OS notifications with a graceful stdout fallback.

3. **Archival** (`ArchiveService`): Copies all artefacts associated with a
   submitted job (résumé, JD, chat log) into a tidy per-job vault folder and
   writes a `summary.json` manifest.

The three services are wired together by `Watchdog`, a lightweight daemon that
runs `schedule`-library jobs every N minutes in a background thread, and
exposed through `LifecycleManager`, the single facade object the orchestrator
imports.

Usage (CLI):
    python lifecycle_watchdog.py status
    python lifecycle_watchdog.py add <company> <role> <url> <deadline>
    python lifecycle_watchdog.py daemon
    python lifecycle_watchdog.py archive <job_id>

Dependencies:
    - plyer  (pip install plyer)        — cross-platform desktop notifications
    - schedule (pip install schedule)   — human-friendly job scheduling
    Both are soft dependencies; the module degrades gracefully without them.
"""

# ══════════════════════════════════════════════════════════════════════════════
# Standard-library imports — no third-party risk in core data structures
# ══════════════════════════════════════════════════════════════════════════════
import json          # serialise/deserialise submit_result blobs and summary.json
import logging       # structured logging so the daemon's heartbeat is auditable
import pathlib       # Path objects are safer than raw strings for file operations
import shutil        # high-level file copy/move used by ArchiveService
import sqlite3       # built-in relational store; no extra install needed
import subprocess    # used for Windows WinRT toast notification fallback
import sys           # sys.argv for the CLI interface and sys.exit on fatal errors
import threading     # daemon thread so Watchdog doesn't block the caller
import time          # time.sleep in the daemon loop keeps CPU usage near zero
from datetime import datetime, timezone  # deadline arithmetic needs tz-aware datetimes
from typing import Optional               # type annotations

# ══════════════════════════════════════════════════════════════════════════════
# Optional third-party imports — wrapped so the module loads even if absent
# ══════════════════════════════════════════════════════════════════════════════
try:
    import schedule as _schedule          # human-readable job scheduler (e.g. every 5 minutes)
    _SCHEDULE_AVAILABLE = True            # flag checked in Watchdog.start()
except ImportError:                       # schedule not installed; Watchdog will warn and no-op
    _SCHEDULE_AVAILABLE = False

try:
    from plyer import notification as _plyer_notify  # cross-platform desktop notification API
    _PLYER_AVAILABLE = True                           # flag checked in NotificationService
except ImportError:                                   # plyer absent; will fall back to print()
    _PLYER_AVAILABLE = False

# ══════════════════════════════════════════════════════════════════════════════
# Module-level constants — single source of truth for paths and magic numbers
# ══════════════════════════════════════════════════════════════════════════════

# Root vault directory where the SQLite DB and all archives live
_BASE_DIR: pathlib.Path = pathlib.Path(__file__).resolve().parent
VAULT_DIR: pathlib.Path = _BASE_DIR / "vault"

# Canonical path to the SQLite database file
DEFAULT_DB_PATH: pathlib.Path = VAULT_DIR / "rolesync.db"

# Hours threshold below which a pending deadline triggers a desktop notification
DEADLINE_ALERT_HOURS: int = 48

# Notification display duration in seconds (15 s is long enough to read without blocking work)
NOTIFICATION_TIMEOUT_SECONDS: int = 15

# ══════════════════════════════════════════════════════════════════════════════
# Logging configuration — single logger for the whole module
# ══════════════════════════════════════════════════════════════════════════════

# Create a named logger so callers can filter RoleSync messages from other libs
logger: logging.Logger = logging.getLogger("rolesync.lifecycle")

# Only attach a handler if none has been configured by the host application yet
# (avoids duplicate log lines when this module is imported inside the orchestrator)
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)  # write to stdout so CI pipelines capture it
    _handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s — %(message)s",  # human-readable timestamp
            datefmt="%Y-%m-%d %H:%M:%S",  # ISO-ish; avoids locale-dependent formats
        )
    )
    logger.addHandler(_handler)          # attach the configured handler
    logger.setLevel(logging.DEBUG)       # DEBUG during development; raise to INFO in production


# ══════════════════════════════════════════════════════════════════════════════
# SQL SCHEMA — defined once here so JobDatabase.__init__ is always idempotent
# ══════════════════════════════════════════════════════════════════════════════

# This DDL creates the jobs table if it does not exist; safe to run on every
# startup because of the IF NOT EXISTS guard.  Schema changes require a
# migration script (not handled here) rather than altering this string.
_CREATE_JOBS_TABLE_SQL: str = """
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,  -- surrogate key; never reused after DELETE
    company         TEXT NOT NULL,                      -- hiring company name, used in folder paths
    role            TEXT NOT NULL,                      -- job title, paired with company for display
    job_url         TEXT,                               -- source URL; NULL if discovered offline
    deadline        TEXT,                               -- ISO date string OR "Not specified"
    status          TEXT DEFAULT 'pending',             -- lifecycle state: pending|submitted|archived|expired
    resume_path     TEXT,                               -- abs path to .docx in active_workspace/
    chat_log_path   TEXT,                               -- abs path to agent's conversation log
    jd_path         TEXT,                               -- abs path to raw job description text file
    submit_result   TEXT,                               -- JSON blob from auto_submit.SubmitResult
    created_at      TEXT DEFAULT (datetime('now')),     -- UTC creation timestamp (SQLite datetime)
    deadline_notified INTEGER DEFAULT 0,                -- 0=not yet sent, 1=48-hour alert dispatched
    archived_at     TEXT                                -- UTC timestamp set when job enters archive
);
"""

# ══════════════════════════════════════════════════════════════════════════════
# Helper: Deadline Parsing & Time Calculation
# ══════════════════════════════════════════════════════════════════════════════
def parse_deadline_info(deadline_str: str) -> tuple[Optional[datetime], str, bool]:
    """
    Parses deadline string and returns (parsed_datetime, friendly_status, is_urgent).
    Supports multiple date formats: %Y-%m-%d, %Y/%m/%d, %d-%m-%Y, %b %d, %Y, etc.
    """
    if not deadline_str or deadline_str.strip().lower() in ("not specified", "none", "", "asap", "rolling"):
        return None, "Not specified", False

    clean_str = deadline_str.strip()
    date_formats = [
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%b %d, %Y",
        "%b %d %Y",
        "%B %d, %Y",
        "%B %d %Y",
        "%Y-%m-%dT%H:%M:%S",
    ]
    parsed_dt = None
    for fmt in date_formats:
        try:
            parsed_dt = datetime.strptime(clean_str, fmt).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            continue

    if not parsed_dt:
        return None, clean_str, False

    now_utc = datetime.now(timezone.utc)
    delta = parsed_dt - now_utc
    hours_left = delta.total_seconds() / 3600
    days_left = delta.total_seconds() / 86400

    if hours_left <= 0:
        return parsed_dt, "Expired", True
    elif hours_left < 24:
        return parsed_dt, f"{int(max(1, hours_left))}h left", True
    elif hours_left < 48:
        return parsed_dt, f"{int(hours_left)}h left", True
    else:
        return parsed_dt, f"{int(days_left)} days left", False


# ══════════════════════════════════════════════════════════════════════════════
#  CLASS: JobDatabase
#  Responsibility: All SQLite reads and writes.  No business logic here — just
#  raw data access behind a clean Python interface.
# ══════════════════════════════════════════════════════════════════════════════

class JobDatabase:
    """
    SQLite-backed persistence layer for RoleSync job records.

    Each instance holds a single connection to the database file.  The
    connection is kept open for the lifetime of the object so we avoid the
    overhead of reconnecting on every query (the daemon calls this many times
    per minute).  ``check_same_thread=False`` is safe here because all writes
    are funnelled through the Watchdog's single scheduler thread.

    Parameters
    ----------
    db_path : pathlib.Path | str
        Filesystem path to the SQLite database file.  The parent directory
        is created automatically if it does not exist, so callers never need
        to mkdir manually.

    Raises
    ------
    sqlite3.OperationalError
        Propagated if the database file cannot be created or opened (e.g.
        permission error on the vault directory).
    """

    def __init__(self, db_path: pathlib.Path | str = DEFAULT_DB_PATH) -> None:
        """
        Initialise the database connection and ensure the schema exists.

        Parameters
        ----------
        db_path : pathlib.Path | str
            Path to the SQLite file.  Defaults to ``f:/agent/vault/rolesync.db``.
        """
        self._db_path: pathlib.Path = pathlib.Path(db_path)  # normalise to Path regardless of input type

        # Create the vault directory tree so sqlite3.connect() doesn't fail
        # with "unable to open database file" on a fresh install
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        # Open (or create) the SQLite file; check_same_thread=False because the
        # Watchdog daemon thread will also use this connection
        self._conn: sqlite3.Connection = sqlite3.connect(
            str(self._db_path),   # sqlite3 wants a str, not a Path
            check_same_thread=False,  # allow cross-thread use; writer serialises via threading.Lock
        )

        # row_factory makes cursor.fetchall() return dict-like Row objects instead
        # of plain tuples, which lets callers use row["company"] instead of row[1]
        self._conn.row_factory = sqlite3.Row

        # Ensure the jobs table exists; idempotent thanks to IF NOT EXISTS
        self._conn.execute(_CREATE_JOBS_TABLE_SQL)
        self._conn.commit()  # flush DDL to disk immediately

        # Serialise concurrent writes from main thread + watchdog thread
        self._lock: threading.Lock = threading.Lock()

        logger.debug("JobDatabase initialised at %s", self._db_path)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        """
        Convert a ``sqlite3.Row`` to a plain Python dict.

        Parameters
        ----------
        row : sqlite3.Row
            A row returned by any cursor fetch operation.

        Returns
        -------
        dict
            Key-value pairs matching the column names of the jobs table.
        """
        # dict(row) works because sqlite3.Row supports the mapping protocol
        return dict(row)

    # ── Write operations ──────────────────────────────────────────────────────

    def insert_job(
        self,
        company: str,
        role: str,
        job_url: str | None = None,
        deadline: str | None = "Not specified",
        resume_path: str | None = None,
        jd_path: str | None = None,
    ) -> int:
        """
        Insert a new job record and return its auto-assigned primary key.

        Called by ``LifecycleManager.register_job()`` immediately after the
        orchestrator discovers a new job posting and assigns a résumé path.

        Parameters
        ----------
        company : str
            Name of the hiring company (e.g. ``"Acme Corp"``).
        role : str
            Job title / role name (e.g. ``"Senior Python Engineer"``).
        job_url : str | None
            URL of the original job posting; ``None`` for offline sources.
        deadline : str | None
            ISO date string (``"2026-11-30"``) or ``"Not specified"``.
        resume_path : str | None
            Absolute path to the tailored résumé ``.docx`` file.
        jd_path : str | None
            Absolute path to the raw job description text file.

        Returns
        -------
        int
            The ``id`` of the newly inserted row (SQLite AUTOINCREMENT).

        Raises
        ------
        sqlite3.Error
            Propagated if the INSERT fails (e.g. disk full, constraint error).
        """
        sql = """
            INSERT INTO jobs (company, role, job_url, deadline, resume_path, jd_path)
            VALUES (?, ?, ?, ?, ?, ?)
        """  # parameterised query prevents SQL injection even from internal callers

        with self._lock:                             # serialise against watchdog thread writes
            cursor = self._conn.execute(            # execute the INSERT
                sql,
                (company, role, job_url, deadline, resume_path, jd_path),
            )
            self._conn.commit()                      # flush to disk before returning the id
            new_id: int = cursor.lastrowid           # SQLite sets lastrowid after INSERT
            logger.info("Inserted job id=%d  %s @ %s", new_id, role, company)
            return new_id                            # caller needs this to reference the row later

    def update_status(self, job_id: int, status: str) -> None:
        """
        Update the ``status`` column for a single job row.

        Valid status values are ``'pending'``, ``'submitted'``, ``'archived'``,
        and ``'expired'``.  No validation is enforced here — the caller is
        responsible for passing a valid value (a CHECK constraint could be
        added in a future migration if strict enforcement is needed).

        Parameters
        ----------
        job_id : int
            Primary key of the job to update.
        status : str
            New lifecycle status string.

        Raises
        ------
        sqlite3.Error
            Propagated on DB write failure.
        """
        sql = "UPDATE jobs SET status = ? WHERE id = ?"  # targeted single-column update
        with self._lock:                     # guard concurrent access
            self._conn.execute(sql, (status, job_id))
            self._conn.commit()              # flush immediately so other threads see the change
            logger.debug("Job id=%d status → %s", job_id, status)

    def mark_submitted(self, job_id: int, submit_result_json: str) -> None:
        """
        Record a successful form submission against a job row.

        Sets ``status='submitted'`` and stores the JSON blob returned by
        ``auto_submit.SubmitResult`` so the submission details are auditable
        without re-opening any external system.

        Parameters
        ----------
        job_id : int
            Primary key of the submitted job.
        submit_result_json : str
            JSON-serialised ``SubmitResult`` object from the auto_submit module.

        Raises
        ------
        sqlite3.Error
            Propagated on DB write failure.
        """
        sql = """
            UPDATE jobs
            SET status = 'submitted',    -- move out of pending so watchdog won't re-notify
                submit_result = ?        -- store the full submission receipt for auditability
            WHERE id = ?
        """
        with self._lock:                      # serialise write
            self._conn.execute(sql, (submit_result_json, job_id))
            self._conn.commit()               # commit before returning to caller
            logger.info("Job id=%d marked submitted", job_id)

    def mark_notified(self, job_id: int) -> None:
        """
        Mark that the 48-hour deadline notification has been sent for a job.

        Prevents duplicate desktop alerts on subsequent watchdog ticks.

        Parameters
        ----------
        job_id : int
            Primary key of the job that was notified.

        Raises
        ------
        sqlite3.Error
            Propagated on DB write failure.
        """
        sql = "UPDATE jobs SET deadline_notified = 1 WHERE id = ?"  # flip the flag to suppress repeats
        with self._lock:
            self._conn.execute(sql, (job_id,))
            self._conn.commit()
            logger.debug("Job id=%d marked deadline_notified", job_id)

    def mark_archived(self, job_id: int, archive_path: str) -> None:
        """
        Transition a job to the ``'archived'`` state and record the archive path.

        Called by ``ArchiveService`` after all files have been copied to the
        vault directory.  The ``archived_at`` timestamp is recorded so users
        can see when each job was closed out.

        Parameters
        ----------
        job_id : int
            Primary key of the archived job.
        archive_path : str
            Absolute path to the archive directory created by ``ArchiveService``.

        Raises
        ------
        sqlite3.Error
            Propagated on DB write failure.
        """
        sql = """
            UPDATE jobs
            SET status      = 'archived',               -- final non-terminal state for submitted jobs
                archived_at = datetime('now'),           -- UTC timestamp from SQLite for consistency
                chat_log_path = ?                        -- store archive path in chat_log_path field for display
            WHERE id = ?
        """
        # NOTE: We repurpose chat_log_path to carry the archive_path because the schema
        # lacks a dedicated archive_path column.  A future migration should add one.
        with self._lock:
            self._conn.execute(sql, (archive_path, job_id))
            self._conn.commit()
            logger.info("Job id=%d archived to %s", job_id, archive_path)

    def update_deadline(self, job_id: int, deadline: str) -> None:
        """Update deadline and reset deadline_notified flag so new deadline can alert."""
        sql = "UPDATE jobs SET deadline = ?, deadline_notified = 0 WHERE id = ?"
        with self._lock:
            self._conn.execute(sql, (deadline, job_id))
            self._conn.commit()
            logger.info("Job id=%d deadline updated → %s", job_id, deadline)

    def update_deadline_by_company_role(self, company: str, role: str, deadline: str) -> None:
        """Update deadline matching company and role, resetting notified flag."""
        sql = "UPDATE jobs SET deadline = ?, deadline_notified = 0 WHERE company = ? AND role = ?"
        with self._lock:
            self._conn.execute(sql, (deadline, company, role))
            self._conn.commit()
            logger.info("Job %s @ %s deadline updated → %s", role, company, deadline)

    # ── Read operations ───────────────────────────────────────────────────────

    def get_pending_deadlines(self) -> list[dict]:
        """
        Retrieve jobs that are pending, have a parseable deadline, and have not
        yet had their 48-hour alert sent.

        The ``deadline NOT IN ('Not specified', '')`` filter excludes jobs with
        no known deadline so the watchdog doesn't attempt to parse placeholder
        strings.  Actual date validation happens in ``Watchdog._check_deadlines``.

        Returns
        -------
        list[dict]
            Each element is a full row dict (all columns) for a qualifying job.
        """
        sql = """
            SELECT *
            FROM jobs
            WHERE status           = 'pending'           -- only active, unsubmitted applications
              AND deadline         NOT IN ('Not specified', '', 'None')  -- skip unparseable placeholders
              AND deadline         IS NOT NULL            -- guard against NULL inserted via Python None
              AND deadline_notified = 0                  -- skip if alert already dispatched
        """
        with self._lock:                        # read lock prevents dirty reads during concurrent writes
            rows = self._conn.execute(sql).fetchall()
        return [self._row_to_dict(r) for r in rows]  # convert Row objects so callers use plain dicts

    def get_jobs_to_archive(self) -> list[dict]:
        """
        Retrieve submitted jobs that have not yet been archived.

        ``archived_at IS NULL`` is the trigger condition: once
        ``mark_archived()`` sets that column, the job will never appear here
        again.

        Returns
        -------
        list[dict]
            Each element is a full row dict for a qualifying submitted job.
        """
        sql = """
            SELECT *
            FROM jobs
            WHERE status      = 'submitted'   -- only fully-submitted applications
              AND archived_at IS NULL          -- not yet copied to vault folder
        """
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def all_jobs(self) -> list[dict]:
        """
        Return every row in the jobs table, ordered newest-first.

        Used by ``LifecycleManager.print_status_table()`` and the CLI
        ``status`` subcommand to give the user a full picture of the pipeline.

        Returns
        -------
        list[dict]
            Complete list of all job rows as plain dicts.
        """
        sql = "SELECT * FROM jobs ORDER BY id DESC"  # newest first so the most relevant jobs appear at top
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_job_by_id(self, job_id: int) -> dict | None:
        """
        Fetch a single job row by primary key.

        Parameters
        ----------
        job_id : int
            Primary key of the desired row.

        Returns
        -------
        dict | None
            Row dict if found, ``None`` if no such id exists.
        """
        sql = "SELECT * FROM jobs WHERE id = ?"  # exact PK lookup; always O(1) on SQLite
        with self._lock:
            row = self._conn.execute(sql, (job_id,)).fetchone()
        return self._row_to_dict(row) if row else None  # guard against missing id

    def close(self) -> None:
        """
        Close the underlying SQLite connection cleanly.

        Should be called when the ``LifecycleManager`` is shut down to avoid
        "database is locked" errors during test teardown or OS shutdown.
        """
        self._conn.close()     # releases file lock so other processes can open the DB
        logger.debug("JobDatabase connection closed")


# ══════════════════════════════════════════════════════════════════════════════
#  CLASS: NotificationService
#  Responsibility: Send desktop alerts to the user when a deadline is imminent.
#  Keeps all notification logic isolated so it can be swapped for email/Slack.
# ══════════════════════════════════════════════════════════════════════════════

class NotificationService:
    """
    Desktop notification dispatcher for deadline alerts.

    Uses ``plyer.notification.notify()`` for cross-platform (Windows, macOS,
    Linux) toast/balloon notifications.  On systems where ``plyer`` is not
    installed or the notification daemon is unavailable (common on headless
    Linux CI runners), it degrades gracefully to a coloured ``print()`` call
    so the information is never silently lost.

    Notes
    -----
    ``plyer`` requires the ``notify2`` or ``dbus-python`` back-end on Linux.
    If those are absent (e.g. inside a Docker container), the ``ImportError``
    or ``RuntimeError`` is caught and the stdout fallback activates.
    """

    def send_daily_reminder(
        self,
        company: str,
        role: str,
        deadline: str,
        days_or_hours_text: str = "",
    ) -> None:
        """
        Dispatch a daily reminder notification to the user's device for an active job chat.
        """
        title = "📅 RoleSync Daily Reminder"
        message = (
            f"Daily Alert: {role} @ {company} — "
            f"{days_or_hours_text or 'Action required'} | Deadline: {deadline}"
        )
        self._dispatch_device_notification(title, message)

    def send_deadline_alert(
        self,
        company: str,
        role: str,
        deadline: str,
        hours_remaining: float,
    ) -> None:
        """
        Fire a desktop notification warning the user about an approaching deadline.
        """
        title = "⏰ RoleSync Deadline Alert"
        message = (
            f"Apply to {role} at {company} — "
            f"{hours_remaining:.0f}h left! "
            f"Deadline: {deadline}"
        )
        self._dispatch_device_notification(title, message)

    def _dispatch_device_notification(self, title: str, message: str) -> None:
        """
        Multi-channel device notification dispatcher:
        1. On Windows: Native Windows WinRT ToastNotification (Windows 10/11)
        2. Cross-platform: Plyer notification
        3. Formatted console block
        """
        dispatched = False

        # 1. On Windows, WinRT Toast is modern, clean, and avoids Shell_NotifyIconW tray errors
        if sys.platform == "win32":
            dispatched = self._windows_winrt_toast(title, message)

        # 2. Plyer fallback if not Windows or if WinRT failed
        if not dispatched and _PLYER_AVAILABLE:
            try:
                _plyer_notify.notify(
                    title=title,
                    message=message,
                    app_name="RoleSync",
                    timeout=NOTIFICATION_TIMEOUT_SECONDS,
                )
                logger.info("Plyer desktop notification sent: %s — %s", title, message)
                dispatched = True
            except Exception as exc:
                logger.warning("plyer notification failed: %s", exc)

        # 3. Formatted console block
        print("", flush=True)
        print("╔══════════════════════════════════════════════════════════════╗", flush=True)
        print(f"║  {title}", flush=True)
        print(f"║  {message}", flush=True)
        print("╚══════════════════════════════════════════════════════════════╝", flush=True)
        print("", flush=True)
        logger.info("Device alert recorded: %s — %s", title, message)

    def _windows_winrt_toast(self, title: str, message: str) -> bool:
        """Dispatch native Windows WinRT Toast notification via PowerShell."""
        try:
            t_clean = title.replace('"', '`"')
            m_clean = message.replace('"', '`"')
            ps_code = f'''
            [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
            $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
            $textNodes = $template.GetElementsByTagName("text")
            $textNodes.Item(0).AppendChild($template.CreateTextNode("{t_clean}")) > $null
            $textNodes.Item(1).AppendChild($template.CreateTextNode("{m_clean}")) > $null
            $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
            [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("RoleSync").Show($toast)
            '''
            subprocess.run(["powershell", "-NoProfile", "-Command", ps_code], capture_output=True, timeout=5)
            logger.info("Windows WinRT toast notification dispatched: %s", title)
            return True
        except Exception as err:
            logger.warning("WinRT toast fallback failed: %s", err)
            return False


# ══════════════════════════════════════════════════════════════════════════════
#  CLASS: ArchiveService
#  Responsibility: Move all artefacts for a submitted job into a named vault
#  sub-directory and write a summary manifest.  Copies rather than moves the
#  originals so the active_workspace/ remains intact for reference.
# ══════════════════════════════════════════════════════════════════════════════

class ArchiveService:
    """
    File-system archival service for completed job applications.

    After a job is submitted, all associated files (résumé, job description,
    chat log) are consolidated into a single vault sub-directory named
    ``{Company}_{Role}/``.  A ``summary.json`` manifest is written alongside
    them so the archive is self-contained and human-readable without needing
    access to the SQLite database.

    The service uses ``shutil.copy2()`` to preserve file metadata (timestamps)
    and then ``shutil.move()`` where appropriate, but critically it only copies
    originals — it never deletes files from ``active_workspace/``.  This
    prevents data loss if archival is triggered prematurely or in error.
    """

    def archive_job(self, job: dict) -> pathlib.Path:
        """
        Copy all artefacts for a submitted job into a vault sub-directory.

        Creates ``vault/{Company}_{Role}/`` and populates it with:
        - The résumé ``.docx`` (copied from ``resume_path``)
        - The raw job description text file (copied from ``jd_path``)
        - The agent chat log (copied from ``chat_log_path``, if it exists)
        - A ``summary.json`` manifest with the full job metadata dict

        Parameters
        ----------
        job : dict
            A full job row dict as returned by ``JobDatabase.all_jobs()`` or
            ``JobDatabase.get_job_by_id()``.  Must contain at minimum the
            ``company``, ``role``, and ``id`` keys.

        Returns
        -------
        pathlib.Path
            Absolute path to the archive directory that was created.

        Raises
        ------
        OSError
            If the archive directory cannot be created or a required source
            file cannot be read (e.g. permission error or disk full).
        """
        # Build a filesystem-safe directory name: replace spaces with underscores,
        # strip characters that are illegal on Windows (:<>|?*"/\)
        safe_company: str = "".join(c if c.isalnum() or c in "-_." else "_" for c in job["company"])
        safe_role: str = "".join(c if c.isalnum() or c in "-_." else "_" for c in job["role"])
        dir_name: str = f"{safe_company}_{safe_role}"  # unique enough for the vault namespace

        # Build the full archive directory path inside the vault
        archive_dir: pathlib.Path = VAULT_DIR / dir_name

        # Create the directory (and any missing parents) safely
        archive_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Archive directory: %s", archive_dir)

        # ── Copy résumé ───────────────────────────────────────────────────────
        resume_src: str | None = job.get("resume_path")  # may be None if job was added without a résumé
        if resume_src and pathlib.Path(resume_src).exists():
            # copy2 preserves metadata (mtime) so the archive timestamp reflects the original creation
            shutil.copy2(resume_src, archive_dir / pathlib.Path(resume_src).name)
            logger.debug("Copied résumé: %s", resume_src)
        else:
            # Log the skip reason so the operator knows the gap in the archive
            logger.warning("Résumé not found or not set for job id=%d; skipping", job["id"])

        # ── Copy job description ──────────────────────────────────────────────
        jd_src: str | None = job.get("jd_path")  # raw JD text file path from the scraper module
        if jd_src and pathlib.Path(jd_src).exists():
            shutil.copy2(jd_src, archive_dir / pathlib.Path(jd_src).name)
            logger.debug("Copied JD: %s", jd_src)
        else:
            logger.warning("JD file not found or not set for job id=%d; skipping", job["id"])

        # ── Copy chat log ─────────────────────────────────────────────────────
        chat_src: str | None = job.get("chat_log_path")  # agent conversation log; optional
        if chat_src and pathlib.Path(chat_src).exists():
            shutil.copy2(chat_src, archive_dir / pathlib.Path(chat_src).name)
            logger.debug("Copied chat log: %s", chat_src)
        else:
            # Chat log absence is non-fatal; not all pipeline runs produce one
            logger.debug("Chat log not found or not set for job id=%d; skipping", job["id"])

        # ── Write summary manifest ────────────────────────────────────────────
        summary_path: pathlib.Path = archive_dir / "summary.json"
        # Include a human-readable archived_at timestamp in the manifest
        summary_data: dict = {
            **job,                                                  # spread all DB fields into the manifest
            "archived_at_utc": datetime.now(timezone.utc).isoformat(),  # ISO timestamp for auditability
        }
        summary_path.write_text(
            json.dumps(summary_data, indent=2, default=str),  # default=str handles non-serialisable types
            encoding="utf-8",                                  # explicit encoding avoids locale surprises
        )
        logger.info("summary.json written to %s", summary_path)

        return archive_dir  # return Path so caller can store it in the DB


# ══════════════════════════════════════════════════════════════════════════════
#  CLASS: Watchdog
#  Responsibility: Scheduler daemon that runs periodic checks in a background
#  thread.  Keeps the main thread free for the orchestrator's own work.
# ══════════════════════════════════════════════════════════════════════════════

class Watchdog:
    """
    Background scheduler that periodically checks deadlines and archives jobs.

    Runs a ``schedule``-library loop inside a daemon thread so it doesn't
    block the host process.  Every ``check_interval_minutes`` it:

    1. Calls ``_check_deadlines()`` — fires desktop alerts for approaching
       deadlines and marks expired jobs.
    2. Calls ``_archive_submitted()`` — moves submitted jobs into the vault.

    The thread is marked as a daemon thread so Python's interpreter shuts it
    down automatically when the main thread exits, preventing zombie threads
    in long-running orchestrator processes.

    Parameters
    ----------
    db : JobDatabase
        The shared database instance (same object as ``LifecycleManager.db``).
    notifier : NotificationService
        The notification dispatcher.
    archiver : ArchiveService
        The file-system archival service.
    check_interval_minutes : int
        How often (in minutes) the scheduler tick fires.  Defaults to 5.
    """

    def __init__(
        self,
        db: JobDatabase,
        notifier: NotificationService,
        archiver: ArchiveService,
        check_interval_minutes: int = 5,
    ) -> None:
        """
        Store service references and create a stop event for clean shutdown.

        Parameters
        ----------
        db : JobDatabase
            Shared database connection; must remain open for the watchdog lifetime.
        notifier : NotificationService
            Used to send desktop alerts in ``_check_deadlines``.
        archiver : ArchiveService
            Used to copy files in ``_archive_submitted``.
        check_interval_minutes : int
            Scheduler tick interval; keep ≥1 to avoid hammering the DB.
        """
        self._db: JobDatabase = db                       # hold a reference; do NOT own the connection lifecycle
        self._notifier: NotificationService = notifier   # injected so tests can swap in a mock
        self._archiver: ArchiveService = archiver        # injected for the same reason
        self._interval: int = check_interval_minutes    # used when registering the schedule job

        # threading.Event is the idiomatic way to signal a background thread to stop;
        # avoids killing the thread forcefully which could leave the DB in a dirty state
        self._stop_event: threading.Event = threading.Event()

        # Placeholder for the thread object; set in start() so callers can check it
        self._thread: threading.Thread | None = None

        logger.debug("Watchdog initialised (interval=%d min)", check_interval_minutes)

    # ── Public lifecycle control ───────────────────────────────────────────────

    def start(self) -> None:
        """
        Register the tick job with ``schedule`` and launch the daemon thread.

        If the ``schedule`` library is not installed, logs a warning and
        returns without starting — the rest of the system continues to work,
        just without the automatic background checks.

        Returns
        -------
        None
        """
        if not _SCHEDULE_AVAILABLE:
            # Warn rather than raise so the orchestrator can continue without the daemon
            logger.warning(
                "schedule library not installed; Watchdog will not start. "
                "Install it with: pip install schedule"
            )
            return  # bail out gracefully

        # Register the _tick callback with the schedule library.
        # every(N).minutes means the first tick fires N minutes after start(),
        # which is intentional — we don't want an immediate heavy tick at startup.
        _schedule.every(self._interval).minutes.do(self._tick)

        # Create the background thread; daemon=True means Python won't wait
        # for it to finish before exiting, preventing hang-on-shutdown scenarios
        self._thread = threading.Thread(
            target=self._run_loop,   # the method that blocks inside the schedule loop
            daemon=True,             # die automatically when the main process exits
            name="RoleSync-Watchdog",  # named thread makes stack traces easier to interpret
        )
        self._stop_event.clear()     # reset in case start() is called after a previous stop()
        self._thread.start()         # kick off the daemon thread
        logger.info("Watchdog daemon thread started (tick every %d min)", self._interval)

    def stop(self) -> None:
        """
        Signal the watchdog loop to terminate and wait for the thread to exit.

        Sets the internal stop event and clears all scheduled ``schedule``
        jobs so they don't fire again after the thread stops.

        Returns
        -------
        None
        """
        self._stop_event.set()       # signal the _run_loop to break out of its while loop

        if self._thread and self._thread.is_alive():
            # Join with a timeout to avoid blocking the caller indefinitely;
            # 15 s is enough for one schedule.run_pending() call to complete
            self._thread.join(timeout=15)
            logger.info("Watchdog thread joined")

        if _SCHEDULE_AVAILABLE:
            _schedule.clear()        # remove all registered jobs so they can't fire again on a re-import

        logger.info("Watchdog stopped")

    # ── Internal loop ─────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        """
        Blocking loop that drives the ``schedule`` library until stop() is called.

        Checks every second whether any scheduled job is due; if so, calls it.
        Between ticks the thread sleeps for 1 second, keeping CPU usage near
        zero (``schedule.run_pending`` itself is O(jobs), not O(time)).

        Returns
        -------
        None
        """
        logger.debug("Watchdog _run_loop entered")
        while not self._stop_event.is_set():      # loop until stop() sets the event
            if _SCHEDULE_AVAILABLE:
                _schedule.run_pending()           # dispatch any jobs whose time has come
            time.sleep(1)                         # yield the CPU; 1 s granularity is fine for minute-scale jobs
        logger.debug("Watchdog _run_loop exited cleanly")

    # ── Tick handler ──────────────────────────────────────────────────────────

    def _tick(self) -> None:
        """
        Single watchdog heartbeat: check deadlines then archive submitted jobs.

        Called by the ``schedule`` library on its background thread.  Wraps
        both sub-checks in a try/except so a failure in one doesn't prevent
        the other from running.

        Returns
        -------
        None
        """
        logger.debug("Watchdog tick starting")
        try:
            self._check_deadlines()           # phase 1: notify and expire pending jobs
        except Exception as exc:              # broad catch: log and continue to archival phase
            logger.error("_check_deadlines failed: %s", exc, exc_info=True)

        try:
            self._archive_submitted()         # phase 2: move submitted jobs into vault
        except Exception as exc:
            logger.error("_archive_submitted failed: %s", exc, exc_info=True)

        logger.debug("Watchdog tick complete")

    def _check_deadlines(self) -> None:
        """
        Scan pending jobs for approaching or expired deadlines.

        For each job returned by ``db.get_pending_deadlines()``:
        - Attempts to parse the ``deadline`` column as an ISO date.
        - Calculates ``hours_remaining`` until midnight on that date.
        - If ``hours_remaining <= 0``: marks the job as ``'expired'``.
        - If ``hours_remaining <= 48``: sends a desktop notification and
          marks the job as notified so the alert fires only once.

        Skips any job whose deadline cannot be parsed (logs a warning).

        Returns
        -------
        None
        """
        pending: list[dict] = self._db.get_pending_deadlines()  # only unnotified pending jobs with dates

        for job in pending:
            deadline_str: str = job["deadline"]  # the raw string from the DB
            deadline_dt, friendly_text, is_urgent = parse_deadline_info(deadline_str)
            if not deadline_dt:
                # Unparseable deadline (e.g. "ASAP", "rolling") — skip gracefully
                logger.warning(
                    "Cannot parse deadline '%s' for job id=%d; skipping",
                    deadline_str, job["id"]
                )
                continue  # move to next job; don't mark notified so we try again next tick

            # Calculate how many hours remain until the deadline (may be negative if past)
            now_utc: datetime = datetime.now(timezone.utc)       # current moment in UTC
            delta = deadline_dt - now_utc                         # timedelta object (can be negative)
            hours_remaining: float = delta.total_seconds() / 3600  # convert seconds to fractional hours

            if hours_remaining <= 0:
                # Deadline has passed; mark expired so the job leaves the pending queue
                logger.info(
                    "Job id=%d '%s @ %s' deadline expired (%.1f h ago)",
                    job["id"], job["role"], job["company"], abs(hours_remaining)
                )
                self._db.update_status(job["id"], "expired")  # remove from active pipeline
                continue  # no need to send an alert for an already-expired job

            if hours_remaining <= DEADLINE_ALERT_HOURS:
                # Deadline is imminent; fire the alert and record it so we don't repeat
                logger.info(
                    "Deadline alert for job id=%d: %.1f h remaining",
                    job["id"], hours_remaining
                )
                self._notifier.send_deadline_alert(      # dispatch desktop notification
                    company=job["company"],
                    role=job["role"],
                    deadline=deadline_str,
                    hours_remaining=hours_remaining,
                )
                self._db.mark_notified(job["id"])        # flip flag so next tick skips this job

    def _archive_submitted(self) -> None:
        """
        Archive all submitted jobs that have not yet been moved to the vault.

        For each job returned by ``db.get_jobs_to_archive()``:
        - Calls ``archiver.archive_job(job)`` to copy files and write summary.
        - Calls ``db.mark_archived(job_id, archive_path)`` to update the DB.

        Failures on individual jobs are logged and skipped so one bad job
        doesn't block the others.

        Returns
        -------
        None
        """
        to_archive: list[dict] = self._db.get_jobs_to_archive()  # submitted jobs with no archived_at

        for job in to_archive:
            try:
                # Copy all artefacts into vault/{Company}_{Role}/
                archive_dir: pathlib.Path = self._archiver.archive_job(job)
                # Record the archive path and timestamp in the DB
                self._db.mark_archived(job["id"], str(archive_dir))
                logger.info("Job id=%d archived to %s", job["id"], archive_dir)
            except Exception as exc:
                # Log and continue; next tick will retry since archived_at remains NULL
                logger.error(
                    "Failed to archive job id=%d: %s", job["id"], exc, exc_info=True
                )


# ══════════════════════════════════════════════════════════════════════════════
#  CLASS: LifecycleManager
#  Responsibility: Top-level facade that the orchestrator imports.  Wires all
#  services together and provides a clean high-level API so callers never touch
#  the individual service classes directly.
# ══════════════════════════════════════════════════════════════════════════════

class LifecycleManager:
    """
    Single object the orchestrator imports to manage the full job lifecycle.

    Instantiating ``LifecycleManager`` wires up:
    - ``JobDatabase`` at the default vault path
    - ``NotificationService`` (stateless)
    - ``ArchiveService`` (stateless)
    - ``Watchdog`` connecting all three above

    The orchestrator only needs to interact with this class; the internals are
    hidden behind descriptive method names.

    Example
    -------
    >>> lm = LifecycleManager()
    >>> job_id = lm.register_job("Acme Corp", "Python Engineer", "https://...", "2026-11-30")
    >>> lm.start_daemon()
    >>> # ... after auto_submit runs ...
    >>> lm.on_submit_complete(job_id, json.dumps({"status": "ok"}))
    >>> lm.stop_daemon()
    """

    def __init__(self) -> None:
        """
        Instantiate and wire all RoleSync lifecycle services.

        Creates the vault directory tree, opens the SQLite database, and
        prepares the watchdog (but does **not** start the daemon thread —
        call ``start_daemon()`` explicitly when ready).
        """
        # Ensure the vault root exists before any service tries to write to it
        VAULT_DIR.mkdir(parents=True, exist_ok=True)

        # Create the shared database connection; all services share this instance
        self.db: JobDatabase = JobDatabase(DEFAULT_DB_PATH)

        # NotificationService is stateless; no config needed
        self._notifier: NotificationService = NotificationService()

        # ArchiveService is stateless; vault path is a module-level constant
        self._archiver: ArchiveService = ArchiveService()

        # Wire up the watchdog with all three services
        self._watchdog: Watchdog = Watchdog(
            db=self.db,
            notifier=self._notifier,
            archiver=self._archiver,
            check_interval_minutes=5,  # default; expose as a parameter if tuning is needed later
        )

        logger.info("LifecycleManager ready (vault: %s)", VAULT_DIR)

    @property
    def notifier(self) -> NotificationService:
        """Access the underlying NotificationService."""
        return self._notifier

    # ── Job registration ───────────────────────────────────────────────────────

    def register_job(
        self,
        company: str,
        role: str,
        job_url: str | None = None,
        deadline: str | None = "Not specified",
        resume_path: str | None = None,
        jd_path: str | None = None,
    ) -> int:
        """
        Register a newly discovered job posting in the database.

        Thin wrapper around ``JobDatabase.insert_job``; exists on the facade
        so the orchestrator never imports ``JobDatabase`` directly.

        Parameters
        ----------
        company : str
            Hiring company name.
        role : str
            Job title.
        job_url : str | None
            URL of the job posting.
        deadline : str | None
            ISO date string or ``"Not specified"``.
        resume_path : str | None
            Absolute path to the tailored résumé ``.docx``.
        jd_path : str | None
            Absolute path to the raw job description file.

        Returns
        -------
        int
            The ``id`` of the newly created job row.
        """
        # Delegate to the DB layer; return the new id so the caller can reference it
        return self.db.insert_job(company, role, job_url, deadline, resume_path, jd_path)

    # ── Submission callback ────────────────────────────────────────────────────

    def on_submit_complete(self, job_id: int, submit_result: str) -> None:
        """
        Called by the auto_submit module after a successful form submission.

        Records the submission receipt and transitions the job to
        ``'submitted'`` so the watchdog will archive it on the next tick.

        Parameters
        ----------
        job_id : int
            Primary key of the job that was submitted.
        submit_result : str
            JSON-serialised ``SubmitResult`` object from ``auto_submit``.

        Returns
        -------
        None
        """
        # Delegate to the DB; the watchdog will pick up the status change on next tick
        self.db.mark_submitted(job_id, submit_result)
        logger.info("on_submit_complete recorded for job id=%d", job_id)

    # ── Daemon control ─────────────────────────────────────────────────────────

    def start_daemon(self) -> None:
        """
        Start the watchdog background thread.

        Safe to call multiple times — the watchdog's ``start()`` checks the
        ``schedule`` library availability and no-ops if already running.

        Returns
        -------
        None
        """
        self._watchdog.start()      # starts the background thread
        logger.info("LifecycleManager daemon started")

    def stop_daemon(self) -> None:
        """
        Signal the watchdog thread to stop and wait for it to exit.

        Should be called in an ``atexit`` handler or ``finally`` block to
        ensure clean shutdown.  Also closes the DB connection.

        Returns
        -------
        None
        """
        self._watchdog.stop()       # signals the loop to exit and joins the thread
        self.db.close()             # release the SQLite file lock
        logger.info("LifecycleManager daemon stopped")

    # ── Status display ─────────────────────────────────────────────────────────

    def print_status_table(self) -> None:
        """
        Print a formatted ASCII table of all jobs to stdout.

        Uses ``str.format()`` for column alignment so there is no dependency
        on ``rich``, ``tabulate``, or any other third-party table library.
        The table is designed to be readable in an 80-column terminal but
        will expand for longer values.

        Returns
        -------
        None
        """
        jobs: list[dict] = self.db.all_jobs()  # fetch all rows, newest first

        if not jobs:
            # Friendly message rather than an empty table header with no rows
            print("No jobs registered yet. Use 'add' or register_job() to add one.")
            return  # nothing more to print

        # Define column widths to keep the table readable; adjust if content is wider
        col_id: int = 4         # fits up to 9999 jobs before wrapping
        col_co: int = 20        # company name; truncated with ellipsis if longer
        col_ro: int = 25        # role title
        col_st: int = 10        # status (longest value is 'submitted' = 9 chars)
        col_dl: int = 14        # deadline (ISO date = 10 chars; "Not specified" = 13)
        col_no: int = 8         # notified flag (0 or 1)

        # Build a reusable format string for data rows
        row_fmt: str = (
            f"{{:<{col_id}}} "   # id column, left-aligned, padded to col_id chars
            f"{{:<{col_co}}} "   # company column
            f"{{:<{col_ro}}} "   # role column
            f"{{:<{col_st}}} "   # status column
            f"{{:<{col_dl}}} "   # deadline column
            f"{{:<{col_no}}}"    # notified flag
        )

        # Print a top border to visually separate the table from prior output
        total_width: int = col_id + col_co + col_ro + col_st + col_dl + col_no + 6
        print("═" * total_width)

        # Print the header row
        print(
            row_fmt.format("ID", "Company", "Role", "Status", "Deadline", "Notif?")
        )

        # Print a separator between header and data rows
        print("─" * total_width)

        # Print one row per job, truncating long strings with ellipsis to preserve alignment
        for job in jobs:
            company_str: str = str(job["company"] or "")[:col_co]       # truncate to column width
            role_str: str = str(job["role"] or "")[:col_ro]             # truncate role
            status_str: str = str(job["status"] or "")[:col_st]         # truncate status
            deadline_str: str = str(job["deadline"] or "")[:col_dl]     # truncate deadline
            notified_str: str = str(job["deadline_notified"] or 0)[:col_no]  # 0 or 1

            print(
                row_fmt.format(
                    job["id"],      # integer id; format coerces it to str
                    company_str,
                    role_str,
                    status_str,
                    deadline_str,
                    notified_str,
                )
            )

        # Bottom border completes the table visually
        print("═" * total_width)
        print(f"  Total: {len(jobs)} job(s)")  # summary line so user knows how many rows were shown


# ══════════════════════════════════════════════════════════════════════════════
#  CLI INTERFACE
#  Provides a lightweight command-line interface for operators and developers
#  to inspect and manipulate the job database without writing Python scripts.
# ══════════════════════════════════════════════════════════════════════════════

def _cli_status(lm: LifecycleManager) -> None:
    """
    CLI handler for the ``status`` subcommand.

    Prints all job rows as a formatted ASCII table.

    Parameters
    ----------
    lm : LifecycleManager
        Shared lifecycle manager instance.
    """
    lm.print_status_table()  # delegate to the facade; keeps CLI thin


def _cli_add(lm: LifecycleManager, args: list[str]) -> None:
    """
    CLI handler for the ``add`` subcommand.

    Inserts a new test job with the provided company, role, URL, and deadline.
    Useful for populating the database during development without running the
    full orchestrator pipeline.

    Parameters
    ----------
    lm : LifecycleManager
        Shared lifecycle manager instance.
    args : list[str]
        Remaining positional arguments after the subcommand name:
        ``[company, role, url, deadline]``.

    Raises
    ------
    SystemExit
        If the wrong number of arguments is provided.
    """
    if len(args) < 4:
        # Print usage hint and exit with a non-zero code so shell scripts detect the error
        print("Usage: lifecycle_watchdog.py add <company> <role> <url> <deadline>")
        sys.exit(1)

    company: str = args[0]   # first positional arg is the company name
    role: str = args[1]      # second is the role title
    url: str = args[2]       # third is the job posting URL
    deadline: str = args[3]  # fourth is the deadline ISO date or "Not specified"

    new_id: int = lm.register_job(
        company=company,
        role=role,
        job_url=url,
        deadline=deadline,
    )
    # Confirm insertion to the operator
    print(f"✅ Job inserted with id={new_id}: {role} @ {company} (deadline: {deadline})")


def _cli_daemon(lm: LifecycleManager) -> None:
    """
    CLI handler for the ``daemon`` subcommand.

    Starts the watchdog in a background thread and blocks the main thread
    indefinitely (or until Ctrl+C).  Designed for running as a standalone
    process managed by a system supervisor (e.g. systemd, PM2, Windows SCM).

    Parameters
    ----------
    lm : LifecycleManager
        Shared lifecycle manager instance.
    """
    lm.start_daemon()   # kick off the background thread
    print("🟢 RoleSync Watchdog daemon running. Press Ctrl+C to stop.")
    try:
        while True:
            # Main thread has nothing to do; sleep in 1-second chunks so
            # KeyboardInterrupt is detected promptly rather than after a long sleep
            time.sleep(1)
    except KeyboardInterrupt:
        # Ctrl+C received; begin clean shutdown
        print("\n🔴 Shutdown signal received. Stopping daemon …")
    finally:
        lm.stop_daemon()   # stop the watchdog thread and close the DB
        print("👋 RoleSync Watchdog stopped cleanly.")


def _cli_archive(lm: LifecycleManager, args: list[str]) -> None:
    """
    CLI handler for the ``archive`` subcommand.

    Manually triggers the archive workflow for a single job identified by its
    database id.  Useful for re-running a failed automatic archive or for
    archiving a job that was submitted outside the agent pipeline.

    Parameters
    ----------
    lm : LifecycleManager
        Shared lifecycle manager instance.
    args : list[str]
        Remaining positional arguments after the subcommand name: ``[job_id]``.

    Raises
    ------
    SystemExit
        If the job_id is missing, non-numeric, or not found in the database.
    """
    if not args:
        print("Usage: lifecycle_watchdog.py archive <job_id>")
        sys.exit(1)

    try:
        job_id: int = int(args[0])  # validate that the argument is a valid integer
    except ValueError:
        # Non-integer id; tell the operator what went wrong
        print(f"Error: job_id must be an integer, got '{args[0]}'")
        sys.exit(1)

    # Look up the job to make sure it exists before attempting archival
    job: dict | None = lm.db.get_job_by_id(job_id)
    if job is None:
        print(f"Error: No job found with id={job_id}")
        sys.exit(1)

    # Instantiate a fresh ArchiveService (stateless, no shared state needed)
    archiver: ArchiveService = ArchiveService()
    try:
        archive_dir: pathlib.Path = archiver.archive_job(job)  # copy files to vault
        lm.db.mark_archived(job_id, str(archive_dir))           # update the DB record
        print(f"✅ Job id={job_id} archived to: {archive_dir}")
    except Exception as exc:
        # Archive failed; report the error and exit non-zero
        print(f"❌ Archive failed for job id={job_id}: {exc}")
        sys.exit(1)


def _print_help() -> None:
    """
    Print the CLI usage help text to stdout.

    Returns
    -------
    None
    """
    # Multi-line help string; kept here rather than in __main__ for reusability
    print(
        """
RoleSync LifecycleManager — CLI

Usage:
  python lifecycle_watchdog.py status
      Print all jobs as a formatted ASCII table.

  python lifecycle_watchdog.py add <company> <role> <url> <deadline>
      Insert a test job into the database.
      <deadline> should be an ISO date string (e.g. 2026-11-30) or "Not specified".

  python lifecycle_watchdog.py daemon
      Start the watchdog background scheduler. Blocks until Ctrl+C.

  python lifecycle_watchdog.py archive <job_id>
      Manually trigger archival for a submitted job by its numeric id.
        """
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
#  Dispatches CLI subcommands.  Instantiates LifecycleManager once so the DB
#  connection is reused across all handlers in the same invocation.
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # sys.argv[0] is always the script name; subcommand starts at index 1
    if len(sys.argv) < 2:
        _print_help()       # no subcommand given; show help
        sys.exit(0)         # exit cleanly (0 = success; user asked for help implicitly)

    subcommand: str = sys.argv[1].lower()   # normalise to lowercase for case-insensitive matching
    remaining_args: list[str] = sys.argv[2:]  # everything after the subcommand

    # Create the shared LifecycleManager; this opens the DB connection
    lifecycle: LifecycleManager = LifecycleManager()

    # Dispatch to the appropriate CLI handler based on the subcommand string
    if subcommand == "status":
        _cli_status(lifecycle)                       # print the jobs table
    elif subcommand == "add":
        _cli_add(lifecycle, remaining_args)          # insert a new job
    elif subcommand == "daemon":
        _cli_daemon(lifecycle)                       # start and block forever
    elif subcommand == "archive":
        _cli_archive(lifecycle, remaining_args)      # manually archive a job
    elif subcommand in ("help", "--help", "-h"):
        _print_help()                                # explicit help request
    else:
        # Unknown subcommand; show help and exit with error code
        print(f"Unknown subcommand: '{subcommand}'")
        _print_help()
        sys.exit(1)                                  # non-zero signals an error to shell callers
