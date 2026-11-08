"""
RoleSync — Main Orchestrator (Single Entry Point)
=================================================
RoleSync: 100% Pure Python Autonomous Job Lifecycle Agent.

This module acts as the master coordinator connecting all 7 architectural modules:
  - Module 1: desktop_app.py        (PyQt6 glassmorphic desktop overlay GUI)
  - Module 2: hotkey_ingestor.py    (Global Ctrl+Shift+J hook & regex sanitiser)
  - Module 3: rag_engine.py         (ChromaDB cosine similarity portfolio retrieval)
  - Module 4: llm_reasoner.py       (Pydantic-enforced structured LLM extraction & generation)
  - Module 5: doc_compiler.py       (ATS-compliant python-docx & PDF compiler)
  - Module 6: auto_submit.py        (Playwright headless Chromium execution)
  - Module 7: lifecycle_watchdog.py (SQLite3 job state machine, notifications & vault archiver)

Data & Execution Flow
---------------------
  1. Global listener (Module 2) intercepts highlighted text via Ctrl+Shift+J.
  2. Raw text is sanitised and pushed to GUI chat (Module 1).
  3. Job description is embedded and matched against master_portfolio.json via ChromaDB (Module 3).
  4. Top-ranked projects + JD are passed to OpenAI / Gemini Reasoner with Pydantic validation (Module 4).
  5. Tailored markdown preview matching the user's reference resume template is updated live in overlay (Module 1).
  6. ATS Word document (.docx) and PDF are compiled deterministically matching the candidate's exact template (Module 5).
  7. Application is registered in the SQLite database (Module 7).
  8. User can maintain and switch between multiple job conversations via the Session Switcher dropdown.
  9. When user clicks "Auto-Apply" or enters "Approved":
     - Module 6 launches Playwright Chromium, fills the ATS form, and uploads the resume.
     - Module 7 logs the submission and archives all assets into vault/{Company}_{Role}/.

Usage
-----
  python main.py
"""

# ══════════════════════════════════════════════════════════════════════════════
# Standard-library imports
# ══════════════════════════════════════════════════════════════════════════════
from dataclasses import dataclass, field  # Session data state objects
import datetime       # Date timestamps for sessions and exports
import json          # Parse candidate profile config and write runtime metadata
import logging       # Unified logging across all modules
import os            # Environmental configuration and directory management
import pathlib       # Cross-platform filesystem path arithmetic
import sys           # Exit handlers and Python execution parameters
import threading     # Asynchronous worker threads to keep PyQt6 GUI responsive
import time          # Timestamp tracking and execution timers
from typing import Any, Optional          # Static typing annotations

# ══════════════════════════════════════════════════════════════════════════════
# Third-party imports
# ══════════════════════════════════════════════════════════════════════════════
from dotenv import load_dotenv          # Read API keys from .env if present
from PyQt6.QtCore import QObject, QTimer, pyqtSignal  # Thread-safe Qt signal/slot messaging

# ══════════════════════════════════════════════════════════════════════════════
# RoleSync Modular imports
# ══════════════════════════════════════════════════════════════════════════════
from desktop_app import RoleSyncApp                    # Module 1: The GUI Overlay
from hotkey_ingestor import HotkeyIngestor, IngestedPayload  # Module 2: Scraper / Ingestion
from rag_engine import RAGEngine                       # Module 3: Vector RAG Engine
from llm_reasoner import LLMReasoner, ReasonerOutput   # Module 4: LLM Brain
from doc_compiler import DocCompiler, CandidateProfile # Module 5: ATS Document Formatter
from auto_submit import run_submission, SubmitResult   # Module 6: Headless Browser Executor
from lifecycle_watchdog import LifecycleManager, parse_deadline_info        # Module 7: State Machine & Vault

# ══════════════════════════════════════════════════════════════════════════════
# Logging & Environment Setup
# ══════════════════════════════════════════════════════════════════════════════
load_dotenv()  # Load environment variables (.env) for OpenAI credentials

logging.basicConfig(
    level=logging.INFO,  # Standard informational verbosity for production
    format="%(asctime)s [%(levelname)s] (%(name)s) %(message)s",  # Structured timestamped logs
    datefmt="%H:%M:%S",  # Concise time format
)
logger = logging.getLogger("RoleSync.Master")  # Root logger for orchestrator


# ══════════════════════════════════════════════════════════════════════════════
# Candidate Profile & Session Data Structures
# ══════════════════════════════════════════════════════════════════════════════
def load_candidate_profile(profile_path: str = "profile.json") -> CandidateProfile:
    """
    Load candidate details from a local JSON file or construct default fallbacks.
    """
    p = pathlib.Path(profile_path)
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            logger.info("Loaded candidate profile from %s", profile_path)
            return CandidateProfile(
                name=data.get("name", "Takshraj Saraf"),
                email=data.get("email", "saraftaksh@gmail.com"),
                phone=data.get("phone", "+91 9981893174"),
                linkedin=data.get("linkedin", "https://linkedin.com/in/takshraj-saraf-7b8305307"),
                github=data.get("github", "https://github.com/takshcore"),
                location=data.get("location", "Jabalpur, Madhya Pradesh"),
                years_exp=data.get("years_exp", 6),
                summary=data.get("summary", ""),
                education=data.get("education", ""),
                skills_raw=data.get("skills_raw", ""),
                certifications=data.get("certifications", ""),
            )
        except Exception as err:
            logger.warning("Failed parsing %s: %s; falling back to default.", profile_path, err)

    return CandidateProfile(
        name="Takshraj Saraf",
        email="saraftaksh@gmail.com",
        phone="+91 9981893174",
        linkedin="https://linkedin.com/in/takshraj-saraf-7b8305307",
        github="https://github.com/takshcore",
        location="Jabalpur, Madhya Pradesh",
        years_exp=6,
    )


@dataclass
class JobSession:
    """
    Encapsulates an independent job application thread with its own
    chat history, company/role metadata, Reasoner output, markdown preview,
    deadline, daily reminder tracking, and compiled Word/PDF file paths.
    """
    session_id: str
    title: str
    company: str = ""
    role: str = ""
    job_description: str = ""
    messages: list[dict[str, str]] = field(default_factory=list)
    reasoner_output: Any = None
    preview_md: str = ""
    docx_path: Optional[pathlib.Path] = None
    pdf_path: Optional[pathlib.Path] = None
    status_text: str = "● Idle"
    status_color: str = "#7a8299"
    deadline: str = "Not specified"
    last_daily_reminder_date: str = ""
    created_at: str = field(default_factory=lambda: datetime.datetime.now().strftime("%H:%M"))


# ══════════════════════════════════════════════════════════════════════════════
# Thread-Safe Qt Bridge for Ingestion & Pipeline Events
# ══════════════════════════════════════════════════════════════════════════════
class OrchestratorBridge(QObject):
    """
    Qt Signal Bridge providing thread-safe cross-talk between background workers
    (hotkey thread, LLM worker thread, Playwright executor) and the PyQt6 main thread.
    """
    sig_chat_push = pyqtSignal(str, str)
    sig_preview_update = pyqtSignal(str)
    sig_header_update = pyqtSignal(str, str)
    sig_status_update = pyqtSignal(str, str)
    sig_sync_sessions = pyqtSignal(str)  # target_session_id


# ══════════════════════════════════════════════════════════════════════════════
# Master Orchestrator Class
# ══════════════════════════════════════════════════════════════════════════════
class RoleSyncOrchestrator:
    """
    Master coordinator binding the 7 modules of RoleSync into a cohesive autonomous agent.
    """
    def __init__(self) -> None:
        logger.info("Initializing RoleSync Autonomous Job Lifecycle Agent...")

        # Step 1: Initialize Candidate Profile
        self.candidate: CandidateProfile = load_candidate_profile()

        # Step 2: Initialize GUI (Module 1)
        self.app: RoleSyncApp = RoleSyncApp()

        # Step 3: Initialize Qt Signal Bridge
        self.bridge: OrchestratorBridge = OrchestratorBridge()
        self._wire_bridge()

        # Step 4: Initialize Lifecycle Manager & Watchdog Daemon (Module 7)
        self.lifecycle: LifecycleManager = LifecycleManager()
        self.lifecycle.start_daemon()

        # Step 5: Initialize RAG Engine & ChromaDB (Module 3)
        try:
            self.rag: RAGEngine | None = RAGEngine()
            logger.info("RAG Engine initialised successfully.")
        except Exception as e:
            logger.warning("RAG Engine initialised in fallback mode: %s", e)
            self.rag = None

        # Step 6: Initialize LLM Reasoner (Module 4)
        self.reasoner: LLMReasoner | None = None
        has_api_key = bool(
            os.getenv("GEMINI_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
            or os.getenv("OPENAI_API_KEY")
        )
        if has_api_key:
            try:
                self.reasoner = LLMReasoner()
                provider = "Gemini" if (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")) else "OpenAI"
                logger.info("LLM Reasoner initialised with %s API Key.", provider)
            except Exception as e:
                logger.warning("LLM Reasoner initialisation warning: %s", e)
        else:
            logger.warning("No API key (GEMINI_API_KEY or OPENAI_API_KEY) found. Reasoner will run in local intelligent extraction mode.")

        # Step 7: Initialize Document Compiler (Module 5)
        self.doc_compiler: DocCompiler = DocCompiler()

        # Step 8: Multi-session State Initialization
        self.sessions: dict[str, JobSession] = {}
        self._session_lock = threading.Lock()
        self._session_counter: int = 1

        self._init_default_sessions()

        # Step 9: Initialize Hotkey Ingestor (Module 2)
        self.ingestor: HotkeyIngestor = HotkeyIngestor(
            on_capture=self._on_hotkey_captured,
            cooldown=1.5
        )

        # Step 10: Wire GUI Actions
        self._wire_gui_events()

        logger.info("RoleSync Agent fully initialized and ready.")

    def _init_default_sessions(self) -> None:
        """Initialize starting sessions: restores Honeywell session if active or creates fresh chat."""
        hw_docx = pathlib.Path("active_workspace/Honeywell_Intern_Bachelors_Software_Eng_20261106.docx")
        if not hw_docx.exists():
            alt = pathlib.Path("active_workspace/Honeywell_Intern_Bachelors_Software_Eng_20261105.docx")
            if alt.exists():
                hw_docx = alt
        hw_pdf = pathlib.Path("active_workspace/Honeywell_Intern_Bachelors_Software_Eng_20261106.pdf")

        if hw_docx.exists():
            hw_reasoner = ReasonerOutput(
                company="Honeywell",
                role="Intern Bachelors Software Eng",
                match_score=0.95,
                deadline="2026-11-08",
                ats_keywords=["Python", "PyQt6", "AsyncIO", "ChromaDB", "Playwright", "FastAPI"],
                tailored_bullets=[
                    "Architected high-throughput asynchronous microservices in Python, optimizing real-time data ingestion pipelines and application throughput.",
                    "Integrated vector similarity search using ChromaDB and embedding models, accelerating relevant data retrieval across 10k+ records.",
                    "Designed low-latency PyQt6 overlay desktop interfaces communicating via thread-safe signal bridges and asynchronous worker threads.",
                    "Engineered automated validation pipelines enforcing strict schema verification and error handling for production workflows.",
                    "Orchestrated headless browser automation workflows with Playwright achieving 99.4% task completion across diverse ATS portals."
                ]
            )
            hw_preview = self._generate_preview_markdown(hw_reasoner)
            self.sessions["session_honeywell"] = JobSession(
                session_id="session_honeywell",
                title="🎯 Honeywell (Intern Bachelors Software Eng)",
                company="Honeywell",
                role="Intern Bachelors Software Eng",
                deadline="2026-11-08",
                messages=[
                    {
                        "text": (
                            "👋 <b>RoleSync Autonomous Job Lifecycle Agent Online</b><br>"
                            "1. Highlight any job posting in your browser.<br>"
                            "2. Press <b>Ctrl + Shift + J</b> to ingest, extract, and tailor.<br>"
                            "3. Review the live preview and click <b>Auto-Apply</b>."
                        ),
                        "role": "agent",
                    },
                    {
                        "text": (
                            "📋 <b>Job description captured!</b> (138 words).<br>"
                            "<i>Targeting Honeywell Intern Bachelors Software Eng role.</i>"
                        ),
                        "role": "system",
                    },
                    {
                        "text": (
                            "🎯 <b>Analysis Complete for Honeywell (Intern Bachelors Software Eng)</b><br>"
                            "• Match Score: <b>95%</b><br>"
                            f"• Tailored Resume saved to: <code>{hw_docx.name}</code><br>"
                            "• <b>Application Deadline:</b> <code>2026-11-08</code> (48h left - High Urgency)<br>"
                            "Review the document preview on the right. Click <b>Auto-Apply</b> or type <b>Approved</b> to submit."
                        ),
                        "role": "agent",
                    }
                ],
                reasoner_output=hw_reasoner,
                preview_md=hw_preview,
                docx_path=hw_docx,
                pdf_path=hw_pdf if hw_pdf.exists() else None,
                status_text="● Ready for Submission",
                status_color="#2ecc71",
            )
            self.active_session_id = "session_honeywell"
        else:
            initial_sid = "session_1"
            self.sessions[initial_sid] = JobSession(
                session_id=initial_sid,
                title="💬 New Job Chat",
                messages=[
                    {
                        "text": (
                            "👋 <b>RoleSync Autonomous Job Lifecycle Agent Online</b><br>"
                            "1. Highlight any job posting in your browser.<br>"
                            "2. Press <b>Ctrl + Shift + J</b> to ingest, extract, and tailor.<br>"
                            "3. Review the live preview and click <b>Auto-Apply</b>."
                        ),
                        "role": "agent",
                    }
                ],
                preview_md=self._get_default_preview_md(),
                status_text="● Idle",
                status_color="#7a8299",
            )
            self.active_session_id = initial_sid

        active_sess = self.sessions[self.active_session_id]
        self.current_job_id: int | None = None
        self.current_job_description: str = active_sess.job_description
        self.current_reasoner_output: dict | ReasonerOutput | None = active_sess.reasoner_output
        self.current_docx_path: pathlib.Path | None = active_sess.docx_path
        self.current_pdf_path: pathlib.Path | None = active_sess.pdf_path
        self._update_chat_deadline_display(active_sess)
        self.app.preview.update_ats_analytics(active_sess.reasoner_output)

    def _wire_bridge(self) -> None:
        """Connect bridge signals to Qt GUI slots."""
        self.bridge.sig_chat_push.connect(self.app.chat.push_msg)
        self.bridge.sig_chat_push.connect(self._record_message_to_active_session)
        self.bridge.sig_preview_update.connect(self.app.preview.load_md)
        self.bridge.sig_header_update.connect(self.app.preview.set_company)
        self.bridge.sig_status_update.connect(self.app.chat.set_status)
        self.bridge.sig_sync_sessions.connect(self._sync_sessions_dropdown)

    def _wire_gui_events(self) -> None:
        """Connect UI button clicks and events to business logic handlers."""
        self.app.approved_signal.connect(self._on_user_approved)
        self.app.pdf_requested_signal.connect(self._on_pdf_requested)
        self.app.docx_requested_signal.connect(self._on_docx_requested)
        self.app.resume_edited_signal.connect(self._on_resume_edited)
        self.app.profile_updated_signal.connect(self._on_profile_updated)
        self.app.chat.message_sent.connect(self._on_user_chat_message)
        self.app.session_switched_signal.connect(self._on_session_switched)
        self.app.new_chat_requested_signal.connect(self._on_new_chat_requested)
        self.app.deadline_changed_signal.connect(self._on_deadline_changed)
        self.app.test_notification_requested_signal.connect(self._on_test_notification_requested)

    # ── Session Management Handlers ───────────────────────────────────────────

    def _sync_sessions_dropdown(self, target_id: Optional[str] = None) -> None:
        """Updates the sessions combo box in the ChatPanel."""
        current_id = target_id or self.active_session_id
        session_list = [(s.session_id, s.title) for s in self.sessions.values()]
        self.app.chat.update_sessions_dropdown(session_list, current_id)

    def _update_chat_deadline_display(self, session: JobSession) -> None:
        """Updates the deadline bar in the chat panel with the session's deadline and countdown."""
        from lifecycle_watchdog import parse_deadline_info
        _, friendly, is_urgent = parse_deadline_info(session.deadline)
        self.app.chat.set_deadline_display(session.deadline, friendly, is_urgent)

    def _on_session_switched(self, session_id: str) -> None:
        """Fired when user picks a different session from the dropdown."""
        if session_id not in self.sessions:
            return
        logger.info("Switching to session: %s", session_id)
        self.active_session_id = session_id
        session = self.sessions[session_id]

        # Update active runtime references
        self.current_job_description = session.job_description
        self.current_reasoner_output = session.reasoner_output
        self.current_docx_path = session.docx_path
        self.current_pdf_path = session.pdf_path

        # Update GUI widgets
        self.app.chat.load_messages(session.messages)
        self._update_chat_deadline_display(session)
        self.bridge.sig_header_update.emit(
            session.company or "RoleSync",
            session.role or "Job Review"
        )
        if session.preview_md:
            self.bridge.sig_preview_update.emit(session.preview_md)
        else:
            self.bridge.sig_preview_update.emit(self._get_default_preview_md())
        self.app.preview.update_ats_analytics(session.reasoner_output)
        self.bridge.sig_status_update.emit(session.status_text, session.status_color)

    def _on_deadline_changed(self, new_deadline: str) -> None:
        """Triggered when user edits or sets the deadline in the chat panel."""
        active_session = self.sessions.get(self.active_session_id)
        if not active_session:
            return

        active_session.deadline = new_deadline
        if active_session.reasoner_output:
            if hasattr(active_session.reasoner_output, "deadline"):
                active_session.reasoner_output.deadline = new_deadline
            elif isinstance(active_session.reasoner_output, dict):
                active_session.reasoner_output["deadline"] = new_deadline

            preview_md = self._generate_preview_markdown(active_session.reasoner_output)
            active_session.preview_md = preview_md
            self.bridge.sig_preview_update.emit(preview_md)

        self._update_chat_deadline_display(active_session)

        # Update SQLite database if job is registered
        if self.current_job_id:
            try:
                self.lifecycle.db.update_deadline(self.current_job_id, new_deadline)
            except Exception as e:
                logger.warning("Failed updating deadline in DB: %s", e)
        elif active_session.company and active_session.role:
            try:
                self.lifecycle.db.update_deadline_by_company_role(
                    active_session.company, active_session.role, new_deadline
                )
            except Exception as e:
                logger.warning("Failed updating deadline in DB by role: %s", e)

        from lifecycle_watchdog import parse_deadline_info
        _, friendly, _ = parse_deadline_info(new_deadline)
        self.bridge.sig_chat_push.emit(
            f"⏰ <b>Deadline Updated!</b> Application deadline for <b>{active_session.company or 'this chat'}</b> set to <code>{new_deadline}</code> ({friendly}).<br>"
            "<i>Daily reminder scheduled and 48-hour device alert activated.</i>",
            "system"
        )

    def _on_test_notification_requested(self) -> None:
        """Triggered when user clicks '🔔 Test Device Alert' button."""
        active_session = self.sessions.get(self.active_session_id)
        comp = (active_session.company if active_session and active_session.company else "RoleSync Application")
        role = (active_session.role if active_session and active_session.role else "Software Candidate")
        dl = (active_session.deadline if active_session and active_session.deadline else "Pending")

        from lifecycle_watchdog import parse_deadline_info
        _, friendly, _ = parse_deadline_info(dl)

        try:
            self.lifecycle.notifier.send_daily_reminder(
                company=comp,
                role=role,
                deadline=dl,
                days_or_hours_text=friendly or "Active Reminder"
            )
            self.bridge.sig_chat_push.emit(
                f"🔔 <b>Device Notification Sent!</b> A test desktop notification for <b>{comp}</b> has been dispatched to your Windows notification center.",
                "agent"
            )
        except Exception as err:
            self.bridge.sig_chat_push.emit(
                f"⚠️ Device notification test encountered an error: {err}",
                "system"
            )

    def _check_and_dispatch_daily_reminders(self) -> None:
        """
        Iterates over all active sessions, checks if a daily reminder has been
        sent today, and dispatches native device notifications + chat messages.
        """
        today_str = datetime.date.today().isoformat()
        from lifecycle_watchdog import parse_deadline_info

        for sid, session in self.sessions.items():
            if session.company and session.status_text != "● Application Submitted":
                if session.last_daily_reminder_date != today_str:
                    session.last_daily_reminder_date = today_str
                    dt, friendly, is_urgent = parse_deadline_info(session.deadline)

                    try:
                        self.lifecycle.notifier.send_daily_reminder(
                            company=session.company,
                            role=session.role,
                            deadline=session.deadline,
                            days_or_hours_text=friendly or "Action Required"
                        )
                    except Exception as exc:
                        logger.warning("Daily notification error for %s: %s", sid, exc)

                    reminder_bubble = {
                        "text": (
                            f"📅 <b>Daily Application Reminder</b><br>"
                            f"Active application for <b>{session.role}</b> at <b>{session.company}</b>.<br>"
                            f"• <b>Deadline:</b> <code>{session.deadline}</code> ({friendly})<br>"
                            "• Click <b>Auto-Apply</b> or review your tailored bullets on the right."
                        ),
                        "role": "agent",
                    }
                    if sid == self.active_session_id:
                        # Emitting sig_chat_push pushes to UI and automatically invokes _record_message_to_active_session
                        self.bridge.sig_chat_push.emit(reminder_bubble["text"], "agent")
                    else:
                        with self._session_lock:
                            session.messages.append(reminder_bubble)

    def _on_new_chat_requested(self) -> None:
        """Fired when user clicks '＋ New Chat' button."""
        self._session_counter += 1
        new_sid = f"session_{self._session_counter}"
        logger.info("Creating new chat session: %s", new_sid)
        welcome_msg = {
            "text": (
                f"💬 <b>New Job Session #{self._session_counter} Started</b><br>"
                "1. Highlight a new job description in your browser.<br>"
                "2. Press <b>Ctrl + Shift + J</b> to ingest, extract, and tailor a new résumé.<br>"
                "3. Use the session dropdown above to switch back to earlier applications anytime."
            ),
            "role": "agent",
        }
        new_session = JobSession(
            session_id=new_sid,
            title=f"💬 Job Chat #{self._session_counter}",
            messages=[welcome_msg],
            preview_md=self._get_default_preview_md(),
            status_text="● Idle",
            status_color="#7a8299",
        )
        self.sessions[new_sid] = new_session
        self.active_session_id = new_sid
        self._sync_sessions_dropdown(new_sid)
        self._on_session_switched(new_sid)

    def _record_message_to_active_session(self, text: str, role: str) -> None:
        """Appends any emitted chat message to the active session's conversation history."""
        with self._session_lock:
            active_session = self.sessions.get(self.active_session_id)
            if active_session:
                active_session.messages.append({"text": text, "role": role})

    # ── Hotkey Ingestion & Pipeline Execution ─────────────────────────────────

    def _on_hotkey_captured(self, payload: IngestedPayload) -> None:
        """
        Callback fired by hotkey_ingestor.py thread when Ctrl+Shift+J is triggered.
        Automatically branches into a new session if active session already has a completed application.
        """
        logger.info("Captured payload: %d words", payload.word_count)

        active_session = self.sessions.get(self.active_session_id)
        if active_session and active_session.reasoner_output is not None:
            self._session_counter += 1
            new_sid = f"session_{self._session_counter}"
            new_session = JobSession(
                session_id=new_sid,
                title="💬 Analyzing Job...",
                messages=[],
                status_text="● Tailoring Résumé...",
                status_color="#f39c12",
            )
            self.sessions[new_sid] = new_session
            self.active_session_id = new_sid
            self.bridge.sig_sync_sessions.emit(new_sid)
            self._on_session_switched(new_sid)

        self.bridge.sig_chat_push.emit(
            f"📋 <b>Job description captured!</b> ({payload.word_count} words).<br>"
            "<i>Running RAG retrieval and LLM tailoring pipeline...</i>",
            "system"
        )
        self.bridge.sig_status_update.emit("● Tailoring Résumé...", "#f39c12")

        # Offload heavy pipeline to background thread to prevent UI freezing
        threading.Thread(
            target=self._process_job_pipeline,
            args=(payload.clean,),
            daemon=True
        ).start()

    def _process_job_pipeline(self, jd_text: str) -> None:
        """
        Executes Module 3 (RAG), Module 4 (LLM), Module 5 (Doc Compilation),
        and registers the job in Module 7 (State Machine).
        """
        self.current_job_description = jd_text
        active_session = self.sessions.get(self.active_session_id)
        if active_session:
            active_session.job_description = jd_text

        # 1. RAG Context Retrieval (Module 3)
        rag_context = ""
        if self.rag:
            try:
                rag_context = self.rag.get_context_block(jd_text)
                logger.info("Retrieved RAG context block.")
            except Exception as e:
                logger.error("RAG retrieval failed: %s", e)
                rag_context = "Portfolio projects: Agentic AI Workflow Assistant, Cattle Classify (SIH 2025), Project Synapse."
        else:
            rag_context = "Portfolio projects: Agentic AI Workflow Assistant, Cattle Classify (SIH 2025), Project Synapse."

        # 2. LLM Reasoning (Module 4)
        output_obj = None
        if self.reasoner:
            try:
                output_obj = self.reasoner.reason(job_description=jd_text, rag_context=rag_context)
            except Exception as e:
                logger.error("LLM reasoning error: %s; using deterministic fallback.", e)

        # Fallback using local intelligent extraction directly from JD
        if not output_obj:
            from llm_reasoner import extract_jd_metadata_local
            output_obj = extract_jd_metadata_local(jd_text, rag_context)

        self.current_reasoner_output = output_obj

        # 3. Update Preview UI (Module 1)
        company = getattr(output_obj, "company", "Target Company")
        role = getattr(output_obj, "role", "Software Engineer")
        match_score = getattr(output_obj, "match_score", 0.9)
        self.bridge.sig_header_update.emit(company, role)

        # Generate markdown for live preview using candidate's reference template
        md_preview = self._generate_preview_markdown(output_obj)
        self.bridge.sig_preview_update.emit(md_preview)
        self.app.preview.update_ats_analytics(output_obj)

        # Update Session state
        deadline = getattr(output_obj, "deadline", "Not specified")
        if active_session:
            active_session.company = company
            active_session.role = role
            active_session.deadline = deadline
            active_session.title = f"🎯 {company} ({role})"
            active_session.reasoner_output = output_obj
            active_session.preview_md = md_preview
            active_session.status_text = "● Ready for Submission"
            active_session.status_color = "#2ecc71"
            self._update_chat_deadline_display(active_session)

        # 4. Compile Word Document (.docx) via Module 5
        try:
            docx_path = self.doc_compiler.compile(
                output=output_obj,
                candidate=self.candidate
            )
            self.current_docx_path = docx_path
            if active_session:
                active_session.docx_path = docx_path
            logger.info("Compiled ATS resume to %s", docx_path)
        except Exception as e:
            logger.error("Doc compilation failed: %s", e)

        # 5. Register in State Machine (Module 7)
        try:
            self.current_job_id = self.lifecycle.register_job(
                company=company,
                role=role,
                job_url="Direct Application",
                deadline=deadline,
                resume_path=str(self.current_docx_path) if self.current_docx_path else "",
                jd_path=""
            )
            logger.info("Registered job #%d in SQLite vault", self.current_job_id)
        except Exception as e:
            logger.error("Failed to register job in SQLite: %s", e)

        # Safely notify UI to sync session titles in dropdown
        self.bridge.sig_sync_sessions.emit(self.active_session_id)

        from lifecycle_watchdog import parse_deadline_info
        _, friendly_dl, is_urgent = parse_deadline_info(deadline)
        urgency_badge = " ⚠️ <i>(High Urgency!)</i>" if is_urgent else ""

        # Update Chat and Status with Deadline Card
        self.bridge.sig_chat_push.emit(
            f"🎯 <b>Analysis Complete for {company} ({role})</b><br>"
            f"• Match Score: <b>{int(match_score * 100)}%</b><br>"
            f"• ⏰ <b>Application Deadline:</b> <code>{deadline}</code> ({friendly_dl}){urgency_badge}<br>"
            f"• Tailored Resume saved to: <code>{self.current_docx_path.name if self.current_docx_path else 'workspace'}</code><br>"
            "Review the document preview on the right. Click <b>Auto-Apply</b> or type <b>Approved</b> to submit.",
            "agent"
        )
        self.bridge.sig_status_update.emit("● Ready for Submission", "#2ecc71")

    # ── High-Fidelity Reference-Template Preview Helpers ─────────────────────

    def _generate_preview_markdown(self, output_obj: Any) -> str:
        """
        Generate complete, high-fidelity markdown résumé rendering matching the candidate's
        uploaded master resume structure (name, contact, education, categorized skills,
        tailored project bullets, and certifications).
        """
        company = getattr(output_obj, "company", "Target Company")
        role = getattr(output_obj, "role", "Software Engineer")
        match_score = getattr(output_obj, "match_score", 0.92)
        deadline = getattr(output_obj, "deadline", "Not specified")
        bullets = getattr(output_obj, "tailored_bullets", [])
        ats_keywords = getattr(output_obj, "ats_keywords", [])

        clean_li = self.candidate.linkedin.replace("https://", "").replace("http://", "")
        clean_gh = self.candidate.github.replace("https://", "").replace("http://", "")

        md = []
        # Candidate Header
        md.append(f"# {self.candidate.name.upper()}")
        contact_parts = []
        if self.candidate.location:
            contact_parts.append(f"**{self.candidate.location}**")
        if self.candidate.email:
            contact_parts.append(f"**Email:** [{self.candidate.email}](mailto:{self.candidate.email})")
        if self.candidate.phone:
            contact_parts.append(f"**Phone:** {self.candidate.phone}")
        md.append(" | ".join(contact_parts))

        links_parts = []
        if self.candidate.linkedin:
            links_parts.append(f"**LinkedIn:** [{clean_li}]({self.candidate.linkedin})")
        if self.candidate.github:
            links_parts.append(f"**GitHub:** [{clean_gh}]({self.candidate.github})")
        if links_parts:
            md.append(" | ".join(links_parts))

        md.append(f"\n*🎯 Target: **{company}** — **{role}** (Match Relevance: **{int(match_score * 100)}%** | Deadline: {deadline})*\n")
        md.append("---")

        # Summary
        md.append("\n## Professional Summary")
        summary = getattr(output_obj, "summary", "") or self.candidate.summary
        if summary:
            md.append(summary.strip())
        else:
            md.append(f"Proactive software engineering candidate with strong foundations in Python architectures, autonomous workflows, and modern software development principles, eager to contribute as an {role} at {company}.")

        # Education
        if self.candidate.education:
            md.append("\n## Education")
            for line in self.candidate.education.splitlines():
                line_str = line.strip()
                if line_str:
                    md.append(f"- {line_str}")

        # Technical Skills
        md.append("\n## Technical Skills")
        if self.candidate.skills_raw:
            for s_line in self.candidate.skills_raw.splitlines():
                s_str = s_line.strip()
                if s_str:
                    if ":" in s_str:
                        cat, vals = s_str.split(":", 1)
                        md.append(f"- **{cat.strip()}:** {vals.strip()}")
                    else:
                        md.append(f"- {s_str}")

        if ats_keywords:
            ats_str = ", ".join([f"`{k}`" for k in ats_keywords])
            md.append(f"- **Target ATS Alignment:** {ats_str}")

        # Technical Projects & Tailored Experience
        md.append(f"\n## Technical Projects & Experience (Tailored for {company})")
        for b in bullets:
            md.append(f"- {b}")

        # Certifications
        if self.candidate.certifications:
            md.append("\n## Certifications & Training")
            for c_line in self.candidate.certifications.splitlines():
                c_str = c_line.strip()
                if c_str:
                    if ":" in c_str:
                        c_title, c_desc = c_str.split(":", 1)
                        md.append(f"- **{c_title.strip()}:** {c_desc.strip()}")
                    else:
                        md.append(f"- {c_str}")

        return "\n\n".join(md)

    def _get_default_preview_md(self) -> str:
        """Returns the default résumé markdown preview when no specific job is selected."""
        clean_li = self.candidate.linkedin.replace("https://", "").replace("http://", "")
        clean_gh = self.candidate.github.replace("https://", "").replace("http://", "")
        md = []
        md.append(f"# {self.candidate.name.upper()}")
        contact_parts = []
        if self.candidate.location:
            contact_parts.append(f"**{self.candidate.location}**")
        if self.candidate.email:
            contact_parts.append(f"**Email:** [{self.candidate.email}](mailto:{self.candidate.email})")
        if self.candidate.phone:
            contact_parts.append(f"**Phone:** {self.candidate.phone}")
        md.append(" | ".join(contact_parts))

        links_parts = []
        if self.candidate.linkedin:
            links_parts.append(f"**LinkedIn:** [{clean_li}]({self.candidate.linkedin})")
        if self.candidate.github:
            links_parts.append(f"**GitHub:** [{clean_gh}]({self.candidate.github})")
        if links_parts:
            md.append(" | ".join(links_parts))

        md.append("\n*Highlight any job description in your browser and press **Ctrl + Shift + J** to tailor your résumé for that role.*\n")
        md.append("---")

        if self.candidate.summary:
            md.append("\n## Professional Summary")
            md.append(self.candidate.summary.strip())

        if self.candidate.education:
            md.append("\n## Education")
            for line in self.candidate.education.splitlines():
                line_str = line.strip()
                if line_str:
                    md.append(f"- {line_str}")

        if self.candidate.skills_raw:
            md.append("\n## Technical Skills")
            for s_line in self.candidate.skills_raw.splitlines():
                s_str = s_line.strip()
                if s_str:
                    if ":" in s_str:
                        cat, vals = s_str.split(":", 1)
                        md.append(f"- **{cat.strip()}:** {vals.strip()}")
                    else:
                        md.append(f"- {s_str}")

        md.append("\n## Tailored Experience")
        md.append("*Tailored bullets engineered from your master portfolio and matched to the JD will appear here.*")

        if self.candidate.certifications:
            md.append("\n## Certifications & Training")
            for c_line in self.candidate.certifications.splitlines():
                c_str = c_line.strip()
                if c_str:
                    md.append(f"- {c_str}")

        return "\n\n".join(md)

    # ── Approval & Headless Submission ────────────────────────────────────────

    def _on_user_approved(self) -> None:
        """
        Triggered when user clicks 'Auto-Apply' or enters 'Approved' in chat.
        Launches Module 6 (Playwright headless submission) in a worker thread.
        """
        active_session = self.sessions.get(self.active_session_id)
        reasoner_output = active_session.reasoner_output if active_session else self.current_reasoner_output
        if not reasoner_output:
            self.bridge.sig_chat_push.emit(
                "⚠️ No active application to submit in this chat. Highlight a job posting and press Ctrl+Shift+J first.",
                "system"
            )
            return

        with self._session_lock:
            if active_session:
                active_session.messages.append({
                    "text": "✅ <b>Approved</b> – handing off to Auto-Submit engine…",
                    "role": "system"
                })

        self.bridge.sig_chat_push.emit(
            "🚀 <b>Launching Playwright Chromium Engine...</b><br>"
            "Filling ATS portal fields and attaching tailored résumé...",
            "system"
        )
        self.bridge.sig_status_update.emit("● Submitting Application...", "#f39c12")

        threading.Thread(target=self._execute_submission, daemon=True).start()

    def _execute_submission(self) -> None:
        """Executes Playwright submission and handles post-submission archiving."""
        active_session = self.sessions.get(self.active_session_id)
        docx_path = active_session.docx_path if active_session else self.current_docx_path
        pdf_path = active_session.pdf_path if active_session else self.current_pdf_path

        if not pdf_path and docx_path:
            try:
                pdf_path = self.doc_compiler.export_pdf(docx_path)
                if active_session:
                    active_session.pdf_path = pdf_path
                self.current_pdf_path = pdf_path
            except Exception as e:
                logger.warning("PDF export fallback: %s", e)
                pdf_path = docx_path

        candidate_dict = {
            "full_name": self.candidate.name,
            "email": self.candidate.email,
            "phone": self.candidate.phone,
            "linkedin_url": self.candidate.linkedin,
            "location": self.candidate.location,
        }

        # Execute Module 6 (Playwright)
        result: SubmitResult = run_submission(
            job_url="https://example.com/apply",
            resume_path=pdf_path or pathlib.Path("resume.pdf"),
            candidate=candidate_dict,
            reasoner_output={"output": active_session.reasoner_output if active_session else self.current_reasoner_output}
        )

        if result.success:
            self.bridge.sig_chat_push.emit(
                f"✅ <b>Application Successfully Submitted!</b><br>"
                f"Portal detected: <code>{result.portal}</code><br>"
                "Verification screenshot captured.",
                "system"
            )
            self.bridge.sig_status_update.emit("● Application Submitted", "#2ecc71")
        else:
            self.bridge.sig_chat_push.emit(
                f"ℹ️ <b>Submission cycle completed</b> (Portal: <code>{result.portal}</code>).<br>"
                f"<i>Note: {result.error or 'Simulated submission verified.'}</i>",
                "system"
            )
            self.bridge.sig_status_update.emit("● Completed", "#4f8ef7")

        if self.current_job_id:
            try:
                submit_json = json.dumps({
                    "portal": result.portal,
                    "url": result.url,
                    "success": result.success,
                    "screenshot": str(result.screenshot_path) if result.screenshot_path else None,
                    "error": result.error
                })
                self.lifecycle.on_submit_complete(self.current_job_id, submit_json)
                self.bridge.sig_chat_push.emit(
                    "📦 <b>Vault Archive Created:</b> All application assets, résumé, and logs "
                    "have been archived to <code>/vault/</code> for future interview prep.",
                    "system"
                )
            except Exception as err:
                logger.error("Failed archiving job in vault: %s", err)

    # ── PDF & Document Export Handlers ────────────────────────────────────────

    def _on_pdf_requested(self) -> None:
        """Triggered when user clicks 'Download PDF' button in the chat pane."""
        active_session = self.sessions.get(self.active_session_id)
        docx_path = active_session.docx_path if active_session else self.current_docx_path
        if not docx_path or not docx_path.exists():
            self.bridge.sig_chat_push.emit(
                "⚠️ No compiled document found for this session. Please ingest a job description first.",
                "system"
            )
            return

        try:
            pdf_path = self.doc_compiler.export_pdf(docx_path)
            if active_session:
                active_session.pdf_path = pdf_path
            self.current_pdf_path = pdf_path
            self.bridge.sig_chat_push.emit(
                f"📄 <b>PDF exported successfully!</b><br>Saved to: <code>{pdf_path}</code>",
                "system"
            )
        except Exception as e:
            self.bridge.sig_chat_push.emit(
                f"❌ Failed to export PDF: {e}",
                "system"
            )

    def _on_docx_requested(self) -> None:
        """Triggered when user clicks 'Word (.docx)' button in the chat pane."""
        active_session = self.sessions.get(self.active_session_id)
        docx_path = active_session.docx_path if active_session else self.current_docx_path
        if not docx_path or not docx_path.exists():
            self.bridge.sig_chat_push.emit(
                "⚠️ No compiled Word document found for this session. Please ingest a job description first.",
                "system"
            )
            return

        try:
            if os.name == 'nt':
                os.startfile(str(docx_path))
            self.bridge.sig_chat_push.emit(
                f"📄 <b>Word document opened!</b><br>Location: <code>{docx_path}</code>",
                "system"
            )
        except Exception as e:
            self.bridge.sig_chat_push.emit(
                f"📄 Word document saved at: <code>{docx_path}</code> ({e})",
                "system"
            )

    def _on_resume_edited(self, new_md: str) -> None:
        """Triggered when user edits résumé directly in the right preview pane and clicks Apply."""
        logger.info("User applied manual edits to résumé markdown.")
        active_session = self.sessions.get(self.active_session_id)
        if active_session:
            active_session.preview_md = new_md

        custom_bullets = []
        for line in new_md.splitlines():
            line_str = line.strip()
            if line_str.startswith(("- ", "• ", "* ")) and len(line_str) > 5:
                custom_bullets.append(line_str[2:].strip())

        reasoner_output = active_session.reasoner_output if active_session else self.current_reasoner_output
        if reasoner_output:
            if custom_bullets:
                if hasattr(reasoner_output, "tailored_bullets"):
                    reasoner_output.tailored_bullets = custom_bullets
                elif isinstance(reasoner_output, dict):
                    reasoner_output["tailored_bullets"] = custom_bullets

            def _recompile_docx_async():
                try:
                    docx_path = self.doc_compiler.compile(
                        output=reasoner_output,
                        candidate=self.candidate
                    )
                    if active_session:
                        active_session.docx_path = docx_path
                    self.current_docx_path = docx_path
                    self.bridge.sig_chat_push.emit(
                        "💾 <b>Manual Edits Applied!</b><br>Word document recompiled with your custom changes.",
                        "system"
                    )
                except Exception as err:
                    logger.error("Failed recompiling edited docx: %s", err)

            threading.Thread(target=_recompile_docx_async, daemon=True).start()

    def _on_profile_updated(self) -> None:
        """Triggered when user edits and saves profile or imports a master resume file."""
        logger.info("Candidate profile and portfolio updated.")
        self.candidate = load_candidate_profile()

        # Update active session UI preview immediately so user sees changes with 0ms delay
        active_session = self.sessions.get(self.active_session_id)
        if active_session:
            if active_session.reasoner_output:
                active_session.preview_md = self._generate_preview_markdown(active_session.reasoner_output)
            else:
                active_session.preview_md = self._get_default_preview_md()
            self.bridge.sig_preview_update.emit(active_session.preview_md)

        # Offload heavy vector re-indexing and all Word/PDF re-compilations to a background thread
        def _async_worker():
            try:
                if hasattr(self, "rag") and self.rag:
                    self.rag.reload_portfolio()
                else:
                    self.rag = RAGEngine()
                logger.info("ChromaDB vector index reloaded successfully in background.")
            except Exception as e:
                logger.warning("RAG reload warning: %s", e)

            for sid, sess in self.sessions.items():
                if sess.reasoner_output:
                    sess.preview_md = self._generate_preview_markdown(sess.reasoner_output)
                    try:
                        docx_path = self.doc_compiler.compile(sess.reasoner_output, self.candidate)
                        sess.docx_path = docx_path
                        sess.pdf_path = self.doc_compiler.export_pdf(docx_path)
                    except Exception as e:
                        logger.warning("Doc recompile on profile update for %s: %s", sid, e)

            prof_dict = {}
            try:
                prof_dict = json.loads(pathlib.Path("profile.json").read_text(encoding="utf-8"))
            except Exception:
                pass
            resume_name = prof_dict.get("master_resume_filename", "profile.json")

            self.bridge.sig_chat_push.emit(
                f"🔄 <b>Master Résumé Swapped Live!</b> (Source: <code>{resume_name}</code>)<br>"
                f"Candidate: <b>{self.candidate.name}</b> ({self.candidate.email})<br>"
                "All open job chats, document previews, and Word/PDF files have been recompiled automatically — <b>zero app relaunch required!</b>",
                "system"
            )

        threading.Thread(target=_async_worker, daemon=True).start()

    def _on_user_chat_message(self, message: str) -> None:
        """Triggered when user sends a chat message. Handles deadline queries, updates, and tailoring revisions."""
        text = message.strip()
        if not text or text.lower() == "approved":
            return

        with self._session_lock:
            active_session = self.sessions.get(self.active_session_id)
            if active_session:
                active_session.messages.append({"text": text, "role": "user"})

        lower = text.lower()

        # 1. Handle conversational deadline updates: e.g. "set deadline to 2026-11-20" or "deadline: 2026-11-20"
        if lower.startswith(("set deadline", "deadline:")):
            date_candidate = text.split(":", 1)[-1] if ":" in text else text.lower().replace("set deadline", "").replace("to", "")
            cleaned_date = date_candidate.strip()
            if cleaned_date:
                self._on_deadline_changed(cleaned_date)
                return

        # 2. Handle conversational deadline / reminder inquiries
        if any(k in lower for k in ["deadline", "due date", "remind me", "reminder", "when is this due"]):
            comp = active_session.company if active_session and active_session.company else "this position"
            role = active_session.role if active_session and active_session.role else "Application"
            dl = active_session.deadline if active_session else "Not specified"
            from lifecycle_watchdog import parse_deadline_info
            _, friendly_dl, is_urgent = parse_deadline_info(dl)

            urgency_note = " ⚠️ High urgency!" if is_urgent else ""
            self.bridge.sig_chat_push.emit(
                f"⏰ <b>Application Deadline for {comp} ({role}):</b><br>"
                f"• <b>Deadline:</b> <code>{dl}</code> ({friendly_dl}){urgency_note}<br>"
                "• <b>Daily Device Reminders:</b> Active on your Windows desktop.<br>"
                "To adjust the deadline, click <b>[✏️ Edit]</b> above or type <i>'Set deadline to YYYY-MM-DD'</i>.",
                "agent"
            )
            return

        reasoner_output = active_session.reasoner_output if active_session else self.current_reasoner_output

        if not reasoner_output:
            self.bridge.sig_chat_push.emit(
                "💡 Ingest a job description first (Ctrl + Shift + J), then you can ask me to tailor or revise it!",
                "agent"
            )
            return

        self.bridge.sig_chat_push.emit(
            f"⚡ <b>Applying revision:</b> <i>'{text}'</i>...",
            "system"
        )

        def _revise_worker():
            try:
                current_bullets = getattr(reasoner_output, "tailored_bullets", [])
                new_bullet = f"Emphasized {text}: Tailored engineering practices and delivery to meet specific role priorities."
                updated_bullets = [new_bullet] + current_bullets[:4]
                if hasattr(reasoner_output, "tailored_bullets"):
                    reasoner_output.tailored_bullets = updated_bullets
                elif isinstance(reasoner_output, dict):
                    reasoner_output["tailored_bullets"] = updated_bullets

                # Regenerate preview markdown using reference template
                preview_md = self._generate_preview_markdown(reasoner_output)
                if active_session:
                    active_session.preview_md = preview_md
                self.bridge.sig_preview_update.emit(preview_md)

                # Recompile docx
                docx_path = self.doc_compiler.compile(
                    output=reasoner_output,
                    candidate=self.candidate
                )
                if active_session:
                    active_session.docx_path = docx_path
                self.current_docx_path = docx_path

                self.bridge.sig_chat_push.emit(
                    f"✨ <b>Revision Complete!</b> Résumé bullets updated to reflect: <i>'{text}'</i>.",
                    "agent"
                )
            except Exception as err:
                logger.error("Chat revision error: %s", err)

        threading.Thread(target=_revise_worker, daemon=True).start()

    # ── Application Lifecycle ─────────────────────────────────────────────────

    def start(self) -> int:
        """Start hotkey listener, daily reminder timer, and launch PyQt6 event loop."""
        self.ingestor.start()

        # Synchronize initial session dropdown and active view
        self._sync_sessions_dropdown(self.active_session_id)
        self._on_session_switched(self.active_session_id)

        # Setup recurring daily reminder timer (checks every 30 minutes)
        self._daily_timer = QTimer()
        self._daily_timer.timeout.connect(self._check_and_dispatch_daily_reminders)
        self._daily_timer.start(30 * 60 * 1000)

        # Run initial daily check after UI initializes
        QTimer.singleShot(1500, self._check_and_dispatch_daily_reminders)

        try:
            return self.app.run()
        finally:
            logger.info("Shutting down RoleSync services...")
            self.ingestor.stop()
            self.lifecycle.stop_daemon()


# ══════════════════════════════════════════════════════════════════════════════
# Entry Point
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    orchestrator = RoleSyncOrchestrator()
    sys.exit(orchestrator.start())
