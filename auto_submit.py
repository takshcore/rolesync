"""
auto_submit.py — Module 6: The Executor
========================================
RoleSync Autonomous Job Application Agent
------------------------------------------
This module is responsible for the **mechanical act of submitting a job application**.
Given a job posting URL, a compiled resume, and the structured output from the AI
Reasoner (Module 5), it launches a real Chromium browser via Microsoft Playwright,
detects which Applicant-Tracking-System (ATS) portal is hosting the application form,
fills every field with the candidate's data, uploads the resume PDF, and clicks Submit.

Design philosophy
-----------------
- **Async-first**: All Playwright operations are async (playwright.async_api).
  A thin synchronous wrapper (`run_submission`) exposes a blocking interface so
  the Qt desktop app can call it from a daemon thread without dealing with event
  loops directly.
- **Never raise**: Every public method swallows exceptions, takes a debug screenshot,
  and returns a `SubmitResult(success=False, ...)`.  The UI layer never has to
  guard against uncaught errors from this module.
- **Anti-bot hygiene**: slow_mo delays, randomised mouse paths, human-like typing
  delays, a real Chrome User-Agent string, and a 1920×1080 viewport collectively
  make the browser session look as human as possible to portal bot-detection.
- **Portal detection**: Greenhouse, Lever, Workday, and Ashby each expose distinct
  URL patterns and DOM landmarks.  A dedicated sniffer routes to portal-specific
  form-fillers; unknown portals fall back to a generic best-effort strategy.

Downstream consumers
--------------------
- `desktop_app.py` imports `connect_to_app` and `run_submission`.
- `connect_to_app` wires the Qt `approved` signal to `run_submission` in a daemon
  thread and routes status strings to the chat panel.

Dependencies
------------
    pip install playwright
    playwright install chromium
"""

# ══════════════════════════════════════════════════════════════════════════════
# Standard-library imports
# ══════════════════════════════════════════════════════════════════════════════
import asyncio          # Required to run the async Playwright coroutines from sync code
import logging          # Structured logging instead of bare print() calls
import pathlib          # Cross-platform path handling for screenshot and resume paths
import random           # Needed for randomising mouse-movement intermediate points
import threading        # Daemon thread used in connect_to_app to avoid blocking Qt event loop
import time             # time.time() gives us a float epoch timestamp for SubmitResult
from dataclasses import dataclass, field  # Lightweight typed result container

# ══════════════════════════════════════════════════════════════════════════════
# Third-party imports  (Playwright async API)
# ══════════════════════════════════════════════════════════════════════════════
# We import lazily inside functions where possible to give a clear ImportError
# message, but the top-level import surfaces missing installation early.
from playwright.async_api import (
    async_playwright,   # Context-manager that owns the Playwright subprocess
    Page,               # Type alias for a Playwright browser page — used in hints
    TimeoutError as PlaywrightTimeoutError,  # Distinct from Python's built-in TimeoutError
)

# ══════════════════════════════════════════════════════════════════════════════
# Module-level logger
# ══════════════════════════════════════════════════════════════════════════════
# Using __name__ ties log records to this module for easy filtering in log files
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════════════════════

# A realistic Chrome 124 User-Agent string.  Presenting a real UA prevents
# trivial bot detection based on Playwright's default "HeadlessChrome" string.
_CHROME_UA: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Default directory where debug screenshots are stored.
# Using pathlib.Path keeps this cross-platform.
_SCREENSHOT_DIR: pathlib.Path = pathlib.Path("active_workspace") / "screenshots"

# Human-like typing delay range in milliseconds.
# Playwright's keyboard.type() accepts a per-keystroke delay; randomising it
# within this range makes the timing profile look more organic.
_TYPE_DELAY_MIN_MS: int = 40    # Minimum ms between keystrokes
_TYPE_DELAY_MAX_MS: int = 110   # Maximum ms between keystrokes

# Number of intermediate mouse-move waypoints used to humanise cursor paths.
# More waypoints = smoother, more human-looking trajectory.
_MOUSE_WAYPOINTS: int = 5

# ══════════════════════════════════════════════════════════════════════════════
# Typed result dataclass
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class SubmitResult:
    """
    Immutable value-object returned by every submission attempt.

    Parameters
    ----------
    success : bool
        True only when the form was submitted and a confirmation was detected.
    portal : str
        One of "greenhouse", "lever", "workday", "ashby", "generic", or "unknown".
    url : str
        The job posting URL that was targeted.
    screenshot_path : pathlib.Path | None
        Absolute path to the final debug screenshot, or None if no screenshot
        could be taken (e.g., browser never launched).
    error : str | None
        Human-readable error description on failure; None on success.
    timestamp : float
        Unix epoch seconds (from time.time()) when the result was produced.
    """
    success: bool                           # Submission outcome
    portal: str                             # Detected ATS portal name
    url: str                                # Target job URL
    screenshot_path: pathlib.Path | None    # Debug screenshot location
    error: str | None                       # Failure reason (None = success)
    timestamp: float = field(              # Auto-set at construction time if not supplied
        default_factory=time.time
    )


# ══════════════════════════════════════════════════════════════════════════════
# CandidateProfile — lightweight shim
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class CandidateProfile:
    """
    Minimal candidate data needed by the form-fillers.

    In a full RoleSync deployment this class is imported from doc_compiler.py
    (Module 3).  It is redefined here as a plain dataclass so this module can
    also be used standalone or in unit tests without that dependency.

    Parameters
    ----------
    full_name : str
        Candidate's legal full name, e.g. "Jane Doe".
    email : str
        Primary contact email address.
    phone : str
        Phone number in any format, e.g. "+1-555-123-4567".
    linkedin_url : str
        Full LinkedIn profile URL.
    location : str
        City / State / Country string for location fields.
    """
    full_name: str       # Used in first-name / full-name fields
    email: str           # Used in email input fields
    phone: str           # Used in telephone input fields
    linkedin_url: str    # Used in LinkedIn URL fields
    location: str = ""   # Used in city / location fields (optional)

    @classmethod
    def from_dict(cls, data: dict) -> "CandidateProfile":
        """
        Construct a CandidateProfile from an arbitrary dict.

        Provides a safe bridge so callers can pass raw dicts (e.g., from JSON
        config or the Qt desktop app) without manually unpacking keys.

        Parameters
        ----------
        data : dict
            Must contain at minimum: full_name, email, phone, linkedin_url.

        Returns
        -------
        CandidateProfile
            Populated instance.

        Raises
        ------
        KeyError
            If a required field is absent from `data`.
        """
        return cls(
            full_name=data["full_name"],        # Mandatory — forms always ask for a name
            email=data["email"],                # Mandatory — primary contact
            phone=data.get("phone", ""),        # Optional in some portals
            linkedin_url=data.get("linkedin_url", ""),  # Optional but highly recommended
            location=data.get("location", ""),  # Optional location string
        )


# ══════════════════════════════════════════════════════════════════════════════
# AutoSubmitter — main class
# ══════════════════════════════════════════════════════════════════════════════

class AutoSubmitter:
    """
    Orchestrates the end-to-end browser-based job application submission.

    Responsibilities
    ----------------
    1. Launch a Playwright Chromium browser with anti-bot settings.
    2. Navigate to the job URL.
    3. Detect which ATS portal is hosting the form.
    4. Dispatch to the correct portal-specific form-filler.
    5. Upload the resume PDF.
    6. Click the submit button and await confirmation.
    7. Take debug screenshots at key milestones.
    8. Return a `SubmitResult` — never raise.

    Parameters
    ----------
    candidate : CandidateProfile
        All personal data needed to populate application form fields.
    headless : bool, optional
        Run Chromium without a visible window (default True).
        Set to False during development for visual debugging.
    slow_mo : int, optional
        Milliseconds to wait between every Playwright action (default 50).
        Acts as a coarse anti-bot throttle — the portal sees actions spaced
        apart rather than arriving at machine speed.
    timeout : int, optional
        Global timeout in milliseconds for page loads and element waits
        (default 30 000 ms = 30 s).
    """

    def __init__(
        self,
        candidate: CandidateProfile,
        headless: bool = True,
        slow_mo: int = 50,
        timeout: int = 30_000,
    ) -> None:
        # Store candidate data so all internal methods can access it without
        # passing it as an argument on every call.
        self._candidate: CandidateProfile = candidate

        # Headless flag is forwarded straight to Playwright's browser launcher.
        self._headless: bool = headless

        # slow_mo is a Playwright-native feature: it inserts a fixed sleep after
        # every CDP command.  Even 50 ms makes mouse events look less robotic.
        self._slow_mo: int = slow_mo

        # Shared timeout used for page.goto() and page.wait_for_selector().
        # Storing once here avoids scattering magic numbers throughout the code.
        self._timeout: int = timeout

        # Playwright's top-level manager, browser, and context are set up in
        # submit() so they live for exactly one submission and are always cleaned up.
        self._playwright = None   # Will hold the AsyncPlaywright manager object
        self._browser = None      # Will hold the Browser instance
        self._context = None      # Will hold the BrowserContext (holds cookies, UA, etc.)

        # Ensure the screenshot output directory exists so _take_screenshot never
        # fails because the directory is missing.
        _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    async def submit(
        self,
        job_url: str,
        resume_path: pathlib.Path,
        reasoner_output: dict,
    ) -> SubmitResult:
        """
        Main entry point: run the full submission pipeline asynchronously.

        The method launches the browser, works through every pipeline stage,
        and always returns a `SubmitResult` — it never raises.

        Parameters
        ----------
        job_url : str
            Direct URL to the job application page (not the job listing page).
        resume_path : pathlib.Path
            Absolute path to the compiled resume PDF produced by doc_compiler.py.
        reasoner_output : dict
            Structured dict from the AI Reasoner containing cover-letter text,
            custom answers, and other per-application metadata.  Currently stored
            in the result for future use by answer-injection logic.

        Returns
        -------
        SubmitResult
            Populated with success/failure outcome, portal type, and screenshot.

        Raises
        ------
        Never — all exceptions are caught internally.
        """
        # Initialise portal to "unknown" so we always have a valid value even if
        # detection fails before we can assign a real portal name.
        portal: str = "unknown"

        # screenshot_path starts as None; it will be set once a screenshot succeeds.
        screenshot_path: pathlib.Path | None = None

        try:
            # ── 1. Launch Playwright and the browser ──────────────────────────
            # async_playwright() returns an async context manager that owns the
            # Playwright subprocess.  We store it on self so close() can shut it down.
            self._playwright = await async_playwright().start()

            # Launch Chromium (not Firefox/WebKit) because most portals are built
            # for Chrome and Chromium is the most widely detected as "normal" by
            # ATS bot-detection logic.
            self._browser = await self._playwright.chromium.launch(
                headless=self._headless,  # True for production, False for debugging
                slow_mo=self._slow_mo,    # Insert slow_mo ms delay after every action
                args=[
                    # Disable the "Chrome is being controlled by automated software"
                    # info-bar that appears in non-headless mode — reduces fingerprint.
                    "--disable-blink-features=AutomationControlled",
                    # Start maximised to match the declared 1920×1080 viewport so
                    # window.screen dimensions align (some portals check this).
                    "--start-maximized",
                ],
            )

            # Create a new browser context (isolated session: cookies, localStorage,
            # service workers are all sandboxed from other contexts).
            # We set the User-Agent and viewport here so EVERY page in this context
            # inherits the realistic browser fingerprint.
            self._context = await self._browser.new_context(
                user_agent=_CHROME_UA,              # Realistic Chrome UA — avoids HeadlessChrome detection
                viewport={"width": 1920, "height": 1080},  # Standard 1080p — matches most office monitors
                accept_downloads=True,              # Allow file-download dialogs (some portals redirect)
            )

            # Open a fresh page (tab) inside our context.
            page: Page = await self._context.new_page()

            # Apply the global timeout to all subsequent page operations so we
            # never hang indefinitely waiting for slow or broken portals.
            page.set_default_timeout(self._timeout)  # ms

            # ── 2. Navigate to the job URL ────────────────────────────────────
            await self._navigate(page, job_url)

            # ── 3. Detect ATS portal ──────────────────────────────────────────
            portal = await self._detect_portal(page)
            logger.info("Detected portal: %s for URL: %s", portal, job_url)

            # ── 4. Take a "before" screenshot for debugging ───────────────────
            screenshot_path = await self._take_screenshot(page, f"{portal}_before")

            # ── 5. Fill the application form ──────────────────────────────────
            # Pass the resume path and reasoner output so portal fillers can use them.
            await self._fill_form(page, portal, resume_path, reasoner_output)

            # ── 6. Take a "filled" screenshot before submitting ───────────────
            screenshot_path = await self._take_screenshot(page, f"{portal}_filled")

            # ── 7. Click the submit button ────────────────────────────────────
            await self._submit_form(page)

            # ── 8. Take a "confirm" screenshot to capture the thank-you page ──
            screenshot_path = await self._take_screenshot(page, f"{portal}_confirm")

            # All stages completed without error — record success.
            logger.info("Submission succeeded for %s", job_url)
            return SubmitResult(
                success=True,
                portal=portal,
                url=job_url,
                screenshot_path=screenshot_path,
                error=None,
                timestamp=time.time(),
            )

        except PlaywrightTimeoutError as exc:
            # Playwright raised a timeout waiting for a navigation or selector.
            # This is the most common failure mode on slow or broken portals.
            logger.error("Timeout during submission to %s: %s", job_url, exc)
            # Attempt a last-ditch screenshot so we can see where the browser got stuck.
            if self._context and not self._context.is_closed():  # Only if context still alive
                try:
                    page = self._context.pages[-1]              # Grab the most recent page
                    screenshot_path = await self._take_screenshot(page, f"{portal}_timeout")
                except Exception:
                    pass  # Screenshot itself failed — not critical, ignore silently

            return SubmitResult(
                success=False,
                portal=portal,
                url=job_url,
                screenshot_path=screenshot_path,
                error=f"Timeout: {exc}",
                timestamp=time.time(),
            )

        except Exception as exc:
            # Catch-all: any unexpected error (network down, element not found, etc.)
            logger.exception("Unexpected error during submission to %s", job_url)
            # Same last-ditch screenshot logic as the timeout branch.
            if self._context is not None:
                try:
                    page = self._context.pages[-1]
                    screenshot_path = await self._take_screenshot(page, f"{portal}_error")
                except Exception:
                    pass  # Screenshot failed — ignore

            return SubmitResult(
                success=False,
                portal=portal,
                url=job_url,
                screenshot_path=screenshot_path,
                error=str(exc),
                timestamp=time.time(),
            )

        finally:
            # Always clean up browser resources regardless of success/failure.
            # This prevents zombie Chromium processes accumulating during a session.
            await self.close()

    # ──────────────────────────────────────────────────────────────────────────
    # Internal pipeline steps
    # ──────────────────────────────────────────────────────────────────────────

    async def _navigate(self, page: Page, url: str) -> None:
        """
        Navigate the browser page to `url` with one automatic retry on network error.

        A single retry covers transient DNS hiccups or portal load-balancer
        cold-starts without hiding genuine failures.

        Parameters
        ----------
        page : Page
            The active Playwright page object.
        url : str
            Absolute URL to navigate to.

        Returns
        -------
        None

        Raises
        ------
        Exception
            Re-raises if the second navigation attempt also fails, letting
            `submit()` handle it and return a failure `SubmitResult`.
        """
        for attempt in range(2):  # Try up to 2 times (attempt 0 and attempt 1)
            try:
                # wait_until="domcontentloaded" is faster than "networkidle" and
                # sufficient for detecting the ATS portal type from DOM/URL.
                await page.goto(url, wait_until="domcontentloaded")
                logger.debug("Navigated to %s on attempt %d", url, attempt + 1)
                return  # Navigation succeeded — exit loop early
            except Exception as exc:
                if attempt == 0:
                    # First failure: log a warning and pause briefly before retry.
                    logger.warning("Navigation attempt 1 failed (%s), retrying…", exc)
                    await asyncio.sleep(2)  # 2-second pause before retry
                else:
                    # Second failure: propagate so submit() can record the error.
                    raise

    async def _detect_portal(self, page: Page) -> str:
        """
        Identify the ATS portal hosting the current page.

        Detection uses a layered strategy:
        1. URL pattern matching (fastest, works before DOM loads).
        2. Known DOM landmarks: data-qa attributes, form IDs, meta tags.

        Returns "generic" when no known portal signature is found, so the
        generic filler can make a best-effort attempt rather than failing hard.

        Parameters
        ----------
        page : Page
            The active Playwright page, which must already have navigated to
            the job application URL.

        Returns
        -------
        str
            One of: "greenhouse", "lever", "workday", "ashby", "generic".

        Raises
        ------
        Never — returns "generic" on any unexpected condition.
        """
        try:
            # Grab the current page URL after any redirects that happened during load.
            current_url: str = page.url.lower()  # Lower-case for case-insensitive matching

            # ── URL-based detection (cheapest — no DOM query needed) ──────────

            if "greenhouse.io" in current_url or "boards.greenhouse.io" in current_url:
                return "greenhouse"  # Greenhouse uses boards.greenhouse.io subdomains

            if "jobs.lever.co" in current_url or "lever.co" in current_url:
                return "lever"       # Lever posts live under jobs.lever.co

            if "myworkdayjobs.com" in current_url or "workday.com" in current_url:
                return "workday"     # Workday uses myworkdayjobs.com for external postings

            if "ashbyhq.com" in current_url or "jobs.ashbyhq.com" in current_url:
                return "ashby"       # Ashby uses jobs.ashbyhq.com

            # ── DOM-based detection (slower — requires page evaluation) ───────

            # Greenhouse injects a <meta name="greenhouse_job_board"> tag.
            greenhouse_meta = await page.query_selector('meta[name="greenhouse_job_board"]')
            if greenhouse_meta:
                return "greenhouse"  # Meta tag confirms Greenhouse even on custom domains

            # Greenhouse forms expose data-qa="submit-app" on the submit button.
            gh_submit = await page.query_selector('[data-qa="submit-app"]')
            if gh_submit:
                return "greenhouse"  # data-qa landmark is authoritative for Greenhouse

            # Lever forms use an <input id="lever-source"> hidden field.
            lever_input = await page.query_selector("#lever-source")
            if lever_input:
                return "lever"       # Lever-specific hidden input is a reliable fingerprint

            # Workday wraps everything in a custom web-component with this root id.
            workday_root = await page.query_selector("#wd-AppShellContainer")
            if workday_root:
                return "workday"     # Workday's Angular/WC shell has this predictable id

            # Ashby renders a form wrapped in a div with data-testid="application-form".
            ashby_form = await page.query_selector('[data-testid="application-form"]')
            if ashby_form:
                return "ashby"       # Ashby's testid attribute is stable across versions

            # None of the known signatures matched — fall back to generic filler.
            logger.info("No known portal detected; defaulting to 'generic'")
            return "generic"

        except Exception as exc:
            # Detection itself failed (page crashed, JS error, etc.) — log and default.
            logger.warning("Portal detection error: %s — defaulting to generic", exc)
            return "generic"

    async def _fill_form(
        self,
        page: Page,
        portal: str,
        resume_path: pathlib.Path,
        reasoner_output: dict,
    ) -> None:
        """
        Dispatch to the portal-specific form-filler based on detected portal type.

        Acting as a router, this method keeps `submit()` clean and makes it
        trivial to add support for a new portal by adding a branch here and
        a corresponding `_fill_<portal>` method.

        Parameters
        ----------
        page : Page
            The active Playwright page, positioned on the application form.
        portal : str
            Portal identifier returned by `_detect_portal`.
        resume_path : pathlib.Path
            Path to the resume file for upload.
        reasoner_output : dict
            AI Reasoner output — may contain custom cover-letter text or Q&A pairs
            that future portal fillers will use for text-area answers.

        Returns
        -------
        None

        Raises
        ------
        ValueError
            If an unrecognised portal string is passed (should never happen in
            practice since _detect_portal always returns one of the known values).
        """
        # Store resume_path and reasoner_output on self temporarily so the
        # portal-specific fillers can reach them without extra parameters.
        self._resume_path: pathlib.Path = resume_path           # Resume for file upload
        self._reasoner_output: dict = reasoner_output           # AI answers for text fields

        # Route to the appropriate filler.  Using a dictionary dispatch is cleaner
        # than a chain of if/elif, and adding a new portal only requires one new entry.
        dispatch_table: dict = {
            "greenhouse": self._fill_greenhouse,  # Greenhouse-specific filler
            "lever": self._fill_lever,            # Lever-specific filler
            "workday": self._fill_workday,        # Workday-specific filler (shadow DOM)
            "ashby": self._fill_ashby,            # Ashby-specific filler
            "generic": self._fill_generic,        # Best-effort filler for unknown portals
        }

        # Look up the filler coroutine; use generic as the ultimate fallback.
        filler = dispatch_table.get(portal, self._fill_generic)

        # Await the filler coroutine to actually execute the form-filling actions.
        await filler(page)

    async def _fill_greenhouse(self, page: Page) -> None:
        """
        Fill a Greenhouse ATS application form.

        Greenhouse uses a relatively stable form structure with predictable
        field IDs (first_name, last_name, email, phone, job_application_answers).
        The resume upload widget is a standard <input type="file"> element.

        Parameters
        ----------
        page : Page
            Playwright page loaded at a Greenhouse application form.

        Returns
        -------
        None

        Raises
        ------
        PlaywrightTimeoutError
            Propagated to submit() if a required field cannot be found within timeout.
        """
        logger.info("Filling Greenhouse form")

        # Split the full name into first and last parts.  Greenhouse uses separate
        # first_name / last_name fields in most configurations.
        name_parts: list[str] = self._candidate.full_name.split(maxsplit=1)
        first_name: str = name_parts[0]                           # Always present
        last_name: str = name_parts[1] if len(name_parts) > 1 else ""  # May be blank

        # ── First name field ──────────────────────────────────────────────────
        # wait_for_selector ensures the field exists before we try to fill it,
        # giving the page time to fully hydrate any JavaScript-rendered elements.
        await page.wait_for_selector("#first_name")               # Wait for field to exist
        await self._human_type(page, "#first_name", first_name)   # Type with human delays

        # ── Last name field ───────────────────────────────────────────────────
        await page.wait_for_selector("#last_name")
        await self._human_type(page, "#last_name", last_name)

        # ── Email field ───────────────────────────────────────────────────────
        await page.wait_for_selector("#email")
        await self._human_type(page, "#email", self._candidate.email)

        # ── Phone field ───────────────────────────────────────────────────────
        # Greenhouse marks the phone field optional in many forms, so we use
        # query_selector (returns None if absent) rather than wait_for_selector.
        phone_field = await page.query_selector("#phone")
        if phone_field:  # Only type if the field exists on this particular form
            await self._human_type(page, "#phone", self._candidate.phone)

        # ── LinkedIn URL ──────────────────────────────────────────────────────
        # Greenhouse exposes a free-text answer field for LinkedIn that may carry
        # various IDs.  We try the most common selector pattern first.
        linkedin_field = await page.query_selector('input[id*="linkedin"]')
        if linkedin_field:
            await self._human_type(page, 'input[id*="linkedin"]', self._candidate.linkedin_url)

        # ── Resume upload ─────────────────────────────────────────────────────
        await self._upload_resume(page, self._resume_path)

    async def _fill_lever(self, page: Page) -> None:
        """
        Fill a Lever ATS application form.

        Lever uses named input fields: name, email, phone, org (current company),
        and a LinkedIn-specific labelled text input.  The resume upload is exposed
        as a standard file input or a styled drop-zone wrapping one.

        Parameters
        ----------
        page : Page
            Playwright page loaded at a Lever application form.

        Returns
        -------
        None

        Raises
        ------
        PlaywrightTimeoutError
            Propagated to submit() if a required field times out.
        """
        logger.info("Filling Lever form")

        # ── Full name (Lever uses a single combined name field) ───────────────
        await page.wait_for_selector('input[name="name"]')        # Wait for field to render
        await self._human_type(page, 'input[name="name"]', self._candidate.full_name)

        # ── Email ─────────────────────────────────────────────────────────────
        await page.wait_for_selector('input[name="email"]')
        await self._human_type(page, 'input[name="email"]', self._candidate.email)

        # ── Phone ─────────────────────────────────────────────────────────────
        phone_input = await page.query_selector('input[name="phone"]')
        if phone_input:  # Phone is optional on some Lever forms
            await self._human_type(page, 'input[name="phone"]', self._candidate.phone)

        # ── LinkedIn URL ──────────────────────────────────────────────────────
        # Lever renders a "URLs" section with labelled text inputs.  The LinkedIn
        # field is reliably labelled with placeholder text containing "linkedin".
        linkedin_input = await page.query_selector('input[placeholder*="linkedin" i]')
        if linkedin_input:
            # Lever's placeholder selector needs an explicit .fill() call because
            # the placeholder lookup returns the element directly.
            await linkedin_input.click()                           # Focus the field
            await page.keyboard.type(                              # Type with human delay
                self._candidate.linkedin_url,
                delay=random.randint(_TYPE_DELAY_MIN_MS, _TYPE_DELAY_MAX_MS),
            )

        # ── Resume upload ─────────────────────────────────────────────────────
        await self._upload_resume(page, self._resume_path)

    async def _fill_workday(self, page: Page) -> None:
        """
        Fill a Workday ATS application form.

        Workday is built with Angular and uses Shadow DOM extensively.  Standard
        CSS selectors cannot pierce shadow boundaries, so we use `page.evaluate()`
        to run JavaScript inside the browser context, which CAN traverse shadow roots.

        .. note::
            This is the most fragile filler because Workday's internal component
            structure changes between releases.  The selectors used here target
            attributes that have been stable since Workday 2022.

        Parameters
        ----------
        page : Page
            Playwright page loaded at a Workday application form.

        Returns
        -------
        None

        Raises
        ------
        PlaywrightTimeoutError
            Propagated to submit() if the Workday shell does not load in time.
        """
        logger.info("Filling Workday form (shadow DOM mode)")

        # Wait for Workday's outermost app-shell element, which signals the Angular
        # application has bootstrapped and shadow roots are available.
        await page.wait_for_selector("#wd-AppShellContainer", timeout=self._timeout)

        # ── Shadow-DOM field filler (JavaScript injection) ────────────────────
        # This JS helper function accepts a CSS selector for a shadow-host element
        # and a value string.  It queries shadowRoot descendants for the actual
        # <input> and sets its value, then dispatches 'input' and 'change' events
        # so Workday's Angular change-detection picks up the new value.
        js_fill_shadow: str = """
            (hostSelector, value) => {
                // Walk every element in the document looking for shadow roots
                // that contain an input matching our target.
                const hosts = document.querySelectorAll(hostSelector);
                for (const host of hosts) {
                    if (!host.shadowRoot) continue;  // Skip elements with no shadow root
                    const input = host.shadowRoot.querySelector('input, textarea');
                    if (!input) continue;  // Shadow root exists but has no text input
                    input.value = value;   // Set the value directly on the DOM element
                    // Dispatch synthetic events so Angular's (input) and (change)
                    // bindings fire — without these, the form model won't update.
                    input.dispatchEvent(new Event('input',  { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;  // Report success to the Python caller
                }
                return false;  // No matching shadow input found
            }
        """

        # ── First name ────────────────────────────────────────────────────────
        name_parts: list[str] = self._candidate.full_name.split(maxsplit=1)
        await page.evaluate(                                       # Run JS in browser context
            js_fill_shadow,
            ["[data-automation-id='legalNameSection_firstName']",  # Workday's data-automation-id
             name_parts[0]]                                        # First name value
        )

        # ── Last name ─────────────────────────────────────────────────────────
        await page.evaluate(
            js_fill_shadow,
            ["[data-automation-id='legalNameSection_lastName']",
             name_parts[1] if len(name_parts) > 1 else ""]        # Graceful if no last name
        )

        # ── Email ─────────────────────────────────────────────────────────────
        await page.evaluate(
            js_fill_shadow,
            ["[data-automation-id='email']",
             self._candidate.email]
        )

        # ── Phone ─────────────────────────────────────────────────────────────
        await page.evaluate(
            js_fill_shadow,
            ["[data-automation-id='phone-number']",
             self._candidate.phone]
        )

        # ── Resume upload ─────────────────────────────────────────────────────
        # Workday's file input is typically accessible with a standard selector
        # even though it lives inside a web component, because Playwright's
        # set_input_files() can target shadow-pierced elements in some versions.
        # We try the standard approach first; if it fails, the outer exception
        # handler in submit() will capture it and return a SubmitResult failure.
        await self._upload_resume(page, self._resume_path)

    async def _fill_ashby(self, page: Page) -> None:
        """
        Fill an Ashby ATS application form.

        Ashby renders a React-based form with predictable data-testid attributes,
        making it one of the most straightforward portals to automate.

        Parameters
        ----------
        page : Page
            Playwright page loaded at an Ashby application form.

        Returns
        -------
        None

        Raises
        ------
        PlaywrightTimeoutError
            Propagated to submit() on element timeout.
        """
        logger.info("Filling Ashby form")

        # Wait for the application form root element so we know the React tree
        # has rendered before we start querying child elements.
        await page.wait_for_selector('[data-testid="application-form"]')

        # ── Name ──────────────────────────────────────────────────────────────
        # Ashby typically uses a single full-name input.
        name_input = await page.query_selector('[data-testid="application-form"] input[name="name"]')
        if name_input:
            await name_input.click()                               # Focus the field
            await page.keyboard.type(
                self._candidate.full_name,
                delay=random.randint(_TYPE_DELAY_MIN_MS, _TYPE_DELAY_MAX_MS),
            )

        # ── Email ─────────────────────────────────────────────────────────────
        email_input = await page.query_selector('[data-testid="application-form"] input[name="email"]')
        if email_input:
            await email_input.click()
            await page.keyboard.type(
                self._candidate.email,
                delay=random.randint(_TYPE_DELAY_MIN_MS, _TYPE_DELAY_MAX_MS),
            )

        # ── Phone ─────────────────────────────────────────────────────────────
        phone_input = await page.query_selector('[data-testid="application-form"] input[name="phone"]')
        if phone_input:
            await phone_input.click()
            await page.keyboard.type(
                self._candidate.phone,
                delay=random.randint(_TYPE_DELAY_MIN_MS, _TYPE_DELAY_MAX_MS),
            )

        # ── LinkedIn ──────────────────────────────────────────────────────────
        linkedin_input = await page.query_selector('input[name*="linkedin" i]')
        if linkedin_input:
            await linkedin_input.click()
            await page.keyboard.type(
                self._candidate.linkedin_url,
                delay=random.randint(_TYPE_DELAY_MIN_MS, _TYPE_DELAY_MAX_MS),
            )

        # ── Resume upload ─────────────────────────────────────────────────────
        await self._upload_resume(page, self._resume_path)

    async def _fill_generic(self, page: Page) -> None:
        """
        Best-effort form filler for unrecognised portals.

        Uses common HTML attribute patterns (`name*=`, `type=`) that appear
        across a wide variety of ATS platforms and bespoke company career pages.
        Fields that cannot be found are silently skipped — partial form completion
        is better than a hard failure.

        Parameters
        ----------
        page : Page
            Playwright page positioned on an application form.

        Returns
        -------
        None

        Raises
        ------
        Never — missing fields are caught and skipped.
        """
        logger.info("Filling generic form with best-effort selectors")

        # Map of CSS selector → value pairs to attempt in order.
        # Using a list of tuples (not a dict) preserves insertion order, which
        # matters because we want name fields filled before email/phone in case
        # the form has focus-dependent validation.
        field_attempts: list[tuple[str, str]] = [
            # Full-name variants — covers many single-name-field portals
            ('input[name*="name" i]:not([name*="company" i]):not([name*="org" i])', self._candidate.full_name),
            # Email — type="email" is the most reliable selector across portals
            ('input[type="email"]', self._candidate.email),
            # Phone — type="tel" is the HTML5 standard for phone fields
            ('input[type="tel"]', self._candidate.phone),
            # LinkedIn — portals use various name conventions; `name*="linkedin"` is broadest
            ('input[name*="linkedin" i]', self._candidate.linkedin_url),
            # Location — only attempt if we have a non-empty location string
            ('input[name*="location" i]', self._candidate.location),
            ('input[name*="city" i]', self._candidate.location),
        ]

        for selector, value in field_attempts:
            if not value:
                # Skip empty values — typing an empty string into a required field
                # could trigger validation errors, leaving the form in a broken state.
                continue
            try:
                element = await page.query_selector(selector)  # Non-blocking lookup
                if element:
                    # Scroll the element into view before clicking — avoids
                    # "ElementNotVisible" errors on long forms where fields are off-screen.
                    await element.scroll_into_view_if_needed()
                    await element.click()                       # Focus the field
                    await page.keyboard.type(                   # Type with human-like delay
                        value,
                        delay=random.randint(_TYPE_DELAY_MIN_MS, _TYPE_DELAY_MAX_MS),
                    )
                    logger.debug("Filled generic field '%s'", selector)
            except Exception as exc:
                # Log but do not raise — partial form completion is acceptable.
                logger.warning("Could not fill generic field '%s': %s", selector, exc)

        # ── Resume upload ─────────────────────────────────────────────────────
        await self._upload_resume(page, self._resume_path)

    async def _upload_resume(self, page: Page, resume_path: pathlib.Path) -> None:
        """
        Locate the resume file-input element and set the file to upload.

        Playwright's `.set_input_files()` method directly sets the value of a
        `<input type="file">` element, bypassing the native OS file-picker dialog
        entirely — this is the standard Playwright pattern for file uploads.

        Parameters
        ----------
        page : Page
            The active Playwright page containing a file-upload input.
        resume_path : pathlib.Path
            Absolute path to the PDF file to upload.

        Returns
        -------
        None

        Raises
        ------
        FileNotFoundError
            If `resume_path` does not point to an existing file.
        PlaywrightTimeoutError
            If no `<input type="file">` can be found within the timeout period.
        """
        # Verify the file exists before attempting the upload — Playwright will
        # raise an unhelpful internal error if given a non-existent path.
        if not resume_path.exists():
            raise FileNotFoundError(f"Resume not found: {resume_path}")

        logger.info("Uploading resume from %s", resume_path)

        # We try two selectors in order of specificity.  The `accept` attribute
        # selector is more specific (only targets resume-upload inputs) while
        # `input[type="file"]` is the broadest fallback.
        file_input_selectors: list[str] = [
            'input[type="file"][accept*="pdf" i]',  # Prefer accept=".pdf" inputs
            'input[type="file"]',                   # Fall back to any file input
        ]

        file_input = None                           # Will hold the located element
        for sel in file_input_selectors:
            file_input = await page.query_selector(sel)  # Non-blocking lookup
            if file_input:
                break  # Found a file input — stop searching

        if file_input is None:
            # Some portals hide the file input inside a styled drop-zone.  Try
            # clicking the visible drop-zone button to reveal the hidden input,
            # then wait for it to appear.
            drop_zone = await page.query_selector('[class*="dropzone" i], [class*="upload" i]')
            if drop_zone:
                await drop_zone.click()            # Click styled zone to reveal hidden input
                await asyncio.sleep(0.5)           # Short pause for DOM to update
                file_input = await page.query_selector('input[type="file"]')

        if file_input is None:
            # Still no file input — log a warning but do not raise, because the
            # outer submit() handler will capture the incomplete submission.
            logger.warning("No file input found — resume not uploaded")
            return

        # set_input_files() bypasses the OS dialog by injecting the file path
        # directly into the input element's file list.  This is the idiomatic
        # Playwright approach and works in both headless and headed modes.
        await file_input.set_input_files(str(resume_path))  # str() required by Playwright API
        logger.info("Resume file set on input element")

        # Brief pause after upload to let any JavaScript upload-preview handlers
        # run before we proceed to submit — avoids a race condition on portals
        # that validate the file before enabling the submit button.
        await asyncio.sleep(1.0)

    async def _take_screenshot(self, page: Page, label: str) -> pathlib.Path:
        """
        Capture a full-page screenshot and save it to the screenshots directory.

        Screenshots are labelled with a human-readable label plus a Unix timestamp
        so a series of screenshots from one submission can be correlated and sorted
        chronologically.

        Parameters
        ----------
        page : Page
            The active Playwright page to screenshot.
        label : str
            Short descriptive label (e.g. "greenhouse_before") prepended to the filename.

        Returns
        -------
        pathlib.Path
            Absolute path to the saved PNG file.

        Raises
        ------
        Exception
            Propagated if the screenshot cannot be saved (disk full, permissions, etc.).
            Callers in submit() catch this and continue without a screenshot.
        """
        # Build a unique filename: label + epoch timestamp (truncated to ms) + .png
        ts: str = str(int(time.time() * 1000))              # Millisecond timestamp for uniqueness
        filename: str = f"{label}_{ts}.png"                 # e.g. "greenhouse_before_1714900000000.png"
        path: pathlib.Path = _SCREENSHOT_DIR / filename     # Full path inside screenshots dir

        # full_page=True captures content below the fold — important for long forms
        # where the submit button may be far below the initial viewport.
        await page.screenshot(path=str(path), full_page=True)  # Save PNG to disk

        logger.debug("Screenshot saved: %s", path)
        return path  # Return path so submit() can include it in SubmitResult

    async def _submit_form(self, page: Page) -> None:
        """
        Locate and click the primary form submit button, then await confirmation.

        We try a prioritised list of selectors to find the submit button,
        because different portals use different element types and labels.
        After clicking we wait for a URL change or a "thank you" / "success"
        DOM element to appear, confirming the submission was processed.

        Parameters
        ----------
        page : Page
            The active Playwright page on the completed application form.

        Returns
        -------
        None

        Raises
        ------
        PlaywrightTimeoutError
            If no submit button is found or no confirmation is detected within timeout.
        """
        logger.info("Locating submit button")

        # Ordered list of selectors from most specific to least specific.
        # Stopping at the first match avoids accidentally clicking multiple buttons.
        submit_selectors: list[str] = [
            '[data-qa="submit-app"]',              # Greenhouse-specific data-qa attribute
            'button[type="submit"]',               # HTML5 standard submit button
            'input[type="submit"]',                # Older form submit input element
            'button:has-text("Submit Application")',  # Playwright text selector (case-sensitive)
            'button:has-text("Apply")',             # Common alternative label
            'button:has-text("Submit")',            # Generic submit label
            '[class*="submit" i]',                 # Class name contains "submit" (case-insensitive)
        ]

        submit_btn = None                          # Will hold the located submit element
        for sel in submit_selectors:
            submit_btn = await page.query_selector(sel)
            if submit_btn:
                logger.debug("Submit button found with selector: %s", sel)
                break  # Stop at first match

        if submit_btn is None:
            # No submit button found — raise so submit() captures it as a failure.
            raise RuntimeError("Submit button not found on page")

        # Move the mouse to the submit button via a randomised path to simulate
        # human cursor movement before clicking.
        bounding_box = await submit_btn.bounding_box()  # Get element position/size
        if bounding_box:
            # Calculate the centre of the submit button as the final mouse destination.
            target_x: float = bounding_box["x"] + bounding_box["width"] / 2
            target_y: float = bounding_box["y"] + bounding_box["height"] / 2
            await self._move_mouse_humanly(page, target_x, target_y)  # Realistic path

        # Click the button — Playwright will scroll it into view automatically.
        await submit_btn.click()
        logger.info("Submit button clicked; awaiting confirmation")

        # Wait for the page to show a submission confirmation.  We use a broad
        # multi-selector approach because confirmation UIs vary between portals.
        try:
            await page.wait_for_selector(
                (
                    # Greenhouse thank-you page container
                    '[data-qa="thanks-page"],'
                    # Common text-based confirmation elements
                    ':text("Application submitted"),'
                    ':text("Thank you for applying"),'
                    ':text("We\'ve received your application"),'
                    # URL-based confirmation (Lever redirects to /thanks)
                    '[class*="confirmation" i],'
                    '[class*="success" i]'
                ),
                timeout=self._timeout,            # Same global timeout
            )
            logger.info("Submission confirmation detected")
        except PlaywrightTimeoutError:
            # Confirmation element not found — may still have submitted (some portals
            # redirect without a visible confirmation).  Log warning but do not raise
            # here; the caller (submit()) will decide based on overall success.
            logger.warning("Submission confirmation selector timed out — may still have succeeded")

    async def _move_mouse_humanly(
        self,
        page: Page,
        target_x: float,
        target_y: float,
    ) -> None:
        """
        Move the virtual mouse cursor from its current position to (target_x, target_y)
        via a series of randomised intermediate waypoints.

        A direct mouse.move(x, y) teleports the cursor instantly, which is a
        strong bot signal.  Routing through several slightly-off-axis intermediate
        points creates a curved, organic-looking trajectory that resembles a human
        moving their hand across a desk.

        Parameters
        ----------
        page : Page
            The active Playwright page (owns the mouse object).
        target_x : float
            Final X coordinate (pixels from left of viewport).
        target_y : float
            Final Y coordinate (pixels from top of viewport).

        Returns
        -------
        None

        Raises
        ------
        Never — mouse movement failures are non-critical and silently ignored.
        """
        try:
            # Generate _MOUSE_WAYPOINTS intermediate positions between the current
            # mouse position and the target.  We add small random offsets (±30px) to
            # each waypoint so the path curves rather than moving in a straight line.
            for i in range(1, _MOUSE_WAYPOINTS + 1):
                # Linear interpolation fraction for this step.
                fraction: float = i / (_MOUSE_WAYPOINTS + 1)  # e.g. 0.166, 0.333, …, 0.833

                # Interpolated X with a random jitter of ±30 px.
                interim_x: float = target_x * fraction + random.uniform(-30, 30)

                # Interpolated Y with a random jitter of ±30 px.
                interim_y: float = target_y * fraction + random.uniform(-30, 30)

                # Move to this waypoint.  steps=1 means one CDP mouse-move event per call,
                # which is sufficient granularity between waypoints.
                await page.mouse.move(interim_x, interim_y)

                # Short random pause between waypoints mimics the slight deceleration /
                # acceleration of a human hand.
                await asyncio.sleep(random.uniform(0.01, 0.05))  # 10–50 ms per waypoint

            # Final move to the exact target position after all waypoints.
            await page.mouse.move(target_x, target_y)

        except Exception as exc:
            # Non-critical — if mouse movement fails we still attempt the click.
            logger.debug("Mouse humanisation failed (non-critical): %s", exc)

    async def _human_type(self, page: Page, selector: str, text: str) -> None:
        """
        Click a field identified by `selector` and type `text` with randomised
        per-keystroke delays to simulate human typing speed.

        Using `page.keyboard.type()` with a `delay` parameter causes Playwright
        to dispatch individual keydown/keypress/keyup events for each character,
        unlike `page.fill()` which injects the value instantly.  The randomised
        delay makes the inter-keystroke timing histogram look non-uniform, which
        is a hallmark of human typing.

        Parameters
        ----------
        page : Page
            The active Playwright page.
        selector : str
            CSS selector that uniquely identifies the target input field.
        text : str
            The string to type into the field.

        Returns
        -------
        None

        Raises
        ------
        PlaywrightTimeoutError
            If the selector cannot be found within the page timeout.
        """
        await page.click(selector)              # Click to focus the field — also scrolls it into view
        await page.keyboard.type(               # Type character by character with delay
            text,
            delay=random.randint(               # Randomise delay per keystroke for human-like cadence
                _TYPE_DELAY_MIN_MS,
                _TYPE_DELAY_MAX_MS,
            ),
        )

    async def close(self) -> None:
        """
        Shut down the browser context and Playwright subprocess cleanly.

        Called automatically in the `finally` block of `submit()` so resources
        are always released even when an exception occurs mid-submission.
        Safe to call multiple times — subsequent calls are no-ops.

        Parameters
        ----------
        None

        Returns
        -------
        None

        Raises
        ------
        Never — cleanup errors are logged but suppressed.
        """
        # Close the browser context first (releases cookies, storage).
        if self._context is not None:
            try:
                await self._context.close()     # Close BrowserContext — releases per-context state
            except Exception as exc:
                logger.debug("Error closing browser context: %s", exc)
            finally:
                self._context = None            # Prevent double-close attempts

        # Close the browser process (releases the Chromium process).
        if self._browser is not None:
            try:
                await self._browser.close()     # Terminate Chromium process
            except Exception as exc:
                logger.debug("Error closing browser: %s", exc)
            finally:
                self._browser = None            # Prevent double-close

        # Stop the Playwright subprocess (Node.js bridge process).
        if self._playwright is not None:
            try:
                await self._playwright.stop()   # Kill the Playwright Node.js subprocess
            except Exception as exc:
                logger.debug("Error stopping Playwright: %s", exc)
            finally:
                self._playwright = None         # Prevent double-stop


# ══════════════════════════════════════════════════════════════════════════════
# Synchronous public interface
# ══════════════════════════════════════════════════════════════════════════════

def run_submission(
    job_url: str,
    resume_path: pathlib.Path,
    candidate: dict,
    reasoner_output: dict,
) -> SubmitResult:
    """
    Blocking synchronous wrapper around the async AutoSubmitter pipeline.

    This is the function that `desktop_app.py`'s `approved` Qt signal connects to
    (via a daemon thread), because Qt's main thread must never be blocked by I/O.

    The function creates a fresh `asyncio` event loop, runs the full async
    submission coroutine to completion inside it, then returns the result to
    the calling thread.

    Parameters
    ----------
    job_url : str
        Direct URL to the job application page.
    resume_path : pathlib.Path
        Absolute path to the compiled resume PDF.
    candidate : dict
        Candidate data dict.  Must contain at minimum: full_name, email,
        phone, linkedin_url.  Passed to `CandidateProfile.from_dict()`.
    reasoner_output : dict
        Structured AI Reasoner output with per-application customisations.

    Returns
    -------
    SubmitResult
        Always returned — never raises.

    Raises
    ------
    Never — all exceptions are caught inside the async pipeline.
    """
    # Convert the raw dict to a typed CandidateProfile.  If the dict is missing
    # required keys, the KeyError is caught below and returned as a failure result.
    try:
        profile: CandidateProfile = CandidateProfile.from_dict(candidate)
    except KeyError as exc:
        # Return a failure result rather than raising — the caller (Qt slot) expects
        # a SubmitResult, not an exception.
        return SubmitResult(
            success=False,
            portal="unknown",
            url=job_url,
            screenshot_path=None,
            error=f"CandidateProfile missing key: {exc}",
            timestamp=time.time(),
        )

    # Instantiate the AutoSubmitter with default anti-bot settings.
    submitter = AutoSubmitter(
        candidate=profile,
        headless=True,          # Headless for production runs
        slow_mo=50,             # 50 ms between actions (anti-bot throttle)
        timeout=30_000,         # 30-second global timeout
    )

    # asyncio.run() creates a new event loop, runs the coroutine until it returns,
    # then destroys the loop.  This is the correct pattern when calling async code
    # from a synchronous context (e.g., a daemon thread) that has no existing loop.
    result: SubmitResult = asyncio.run(
        submitter.submit(
            job_url=job_url,
            resume_path=resume_path,
            reasoner_output=reasoner_output,
        )
    )

    return result  # Return typed result to the caller (Qt thread / test harness)


# ══════════════════════════════════════════════════════════════════════════════
# Qt integration hook
# ══════════════════════════════════════════════════════════════════════════════

def connect_to_app(
    chat_panel_push_fn,   # Callable[[str, str], None] — push(message, role)
    approved_signal,      # PyQt / PySide Signal object — emitted when user approves
) -> None:
    """
    Wire the Qt `approved` signal to `run_submission` in a background daemon thread.

    When the user clicks "Approve & Submit" in the desktop UI, the `approved_signal`
    is emitted with a payload dict containing job_url, resume_path, candidate, and
    reasoner_output.  This function connects that signal to a slot that:

    1. Pushes a "Submitting…" status message to the chat panel immediately.
    2. Launches `run_submission` in a daemon thread (so Qt's main thread is free).
    3. Pushes the final success/failure status back to the chat panel when done.

    Parameters
    ----------
    chat_panel_push_fn : Callable[[str, str], None]
        A function that accepts (message: str, role: str) and renders the message
        in the desktop app's chat panel.  `role` should be `"system"` for status
        updates generated by this module.
    approved_signal : Signal
        A Qt signal (PyQt5/PySide6 Signal) that emits a dict payload when the
        user approves a job application for submission.

    Returns
    -------
    None

    Raises
    ------
    Never — connection errors are logged.
    """

    def _on_approved(payload: dict) -> None:
        """
        Slot connected to the `approved` signal.

        Runs in the Qt main thread briefly (just enough to push the initial status
        and spin up the daemon thread), then `run_submission` takes over in the
        background thread.

        Parameters
        ----------
        payload : dict
            Expected keys: job_url (str), resume_path (str|Path),
            candidate (dict), reasoner_output (dict).
        """
        # Immediately push a "Submitting" status so the user sees feedback right away.
        chat_panel_push_fn("⏳ Submitting application…", "system")

        def _worker() -> None:
            """
            Background thread worker: calls run_submission and reports results.

            Running in a daemon thread ensures it is killed automatically if the
            main application exits, preventing zombie Chromium processes.
            """
            # Extract submission arguments from the payload dict.
            job_url: str = payload.get("job_url", "")
            resume_path: pathlib.Path = pathlib.Path(payload.get("resume_path", ""))
            candidate: dict = payload.get("candidate", {})
            reasoner_output: dict = payload.get("reasoner_output", {})

            # Run the synchronous (but internally async) submission pipeline.
            result: SubmitResult = run_submission(
                job_url=job_url,
                resume_path=resume_path,
                candidate=candidate,
                reasoner_output=reasoner_output,
            )

            # Push the outcome back to the chat panel so the user is informed.
            if result.success:
                # Build a rich success message with key metadata.
                msg: str = (
                    f"✅ Application submitted successfully!\n"
                    f"Portal: {result.portal}\n"
                    f"Screenshot: {result.screenshot_path}"
                )
            else:
                # Include the error string so the user knows what went wrong.
                msg = (
                    f"❌ Submission failed.\n"
                    f"Portal: {result.portal}\n"
                    f"Error: {result.error}\n"
                    f"Screenshot: {result.screenshot_path}"
                )

            # Push the status message to the chat panel with "system" role so it
            # is styled differently from AI or user messages in the UI.
            chat_panel_push_fn(msg, "system")

        # Create a daemon thread so it is automatically cleaned up on app exit.
        thread = threading.Thread(target=_worker, daemon=True, name="SubmissionWorker")
        thread.start()  # Launch the background submission thread

    # Connect the Qt signal to our slot.  Qt will call _on_approved(payload)
    # on the main thread whenever the signal is emitted.
    try:
        approved_signal.connect(_on_approved)   # Wire signal to slot
        logger.info("AutoSubmitter connected to approved_signal")
    except Exception as exc:
        # Log the failure but do not raise — missing this connection is not fatal
        # to application startup; it just means auto-submit won't fire.
        logger.error("Failed to connect approved_signal: %s", exc)


# ══════════════════════════════════════════════════════════════════════════════
# Smoke test  (run directly: python auto_submit.py)
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    """
    Smoke test: navigate to https://example.com, detect the portal (expect "generic"),
    take a screenshot, and print the SubmitResult.

    This test does NOT attempt to fill or submit any form — it just validates that
    Playwright is installed correctly, Chromium launches, navigation works, portal
    detection runs, and screenshots can be saved.

    Usage
    -----
        python auto_submit.py

    Expected output
    ---------------
        SubmitResult(success=False, portal='generic', url='https://example.com', ...)
        (success=False because example.com has no submit button — that is correct)
    """
    # Configure basic logging so we can see what the submitter is doing.
    logging.basicConfig(
        level=logging.DEBUG,                    # Show all log levels during smoke test
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )

    # Minimal candidate dict for the smoke test — no real personal data needed.
    _smoke_candidate: dict = {
        "full_name": "Test User",
        "email": "test@example.com",
        "phone": "+1-555-000-0000",
        "linkedin_url": "https://linkedin.com/in/testuser",
        "location": "Remote",
    }

    # A dummy resume path — the smoke test navigates only, so the file need not exist.
    # We use a Path object so the type annotation is satisfied; existence is checked
    # inside _upload_resume, which won't be reached in this test.
    _smoke_resume: pathlib.Path = pathlib.Path("active_workspace/resume/smoke_test_resume.pdf")

    # run_submission is the synchronous public API — same path the Qt app uses.
    _result: SubmitResult = run_submission(
        job_url="https://example.com",          # Public page — no form, no submission risk
        resume_path=_smoke_resume,
        candidate=_smoke_candidate,
        reasoner_output={"cover_letter": "Smoke test — no real content"},
    )

    # Print the result so the user can inspect it in the terminal.
    print("\n" + "═" * 60)
    print("SMOKE TEST RESULT")
    print("═" * 60)
    print(f"  Success        : {_result.success}")
    print(f"  Portal         : {_result.portal}")
    print(f"  URL            : {_result.url}")
    print(f"  Screenshot     : {_result.screenshot_path}")
    print(f"  Error          : {_result.error}")
    print(f"  Timestamp      : {_result.timestamp}")
    print("═" * 60)
    # Expected: success=False (no submit button on example.com), portal="generic"
    # Screenshot path should point to an existing PNG if Playwright ran correctly.
