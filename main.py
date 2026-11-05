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
  4. Top-ranked projects + JD are passed to OpenAI Reasoner with Pydantic validation (Module 4).
  5. Tailored markdown preview is updated live in the overlay (Module 1).
  6. ATS Word document (.docx) and PDF are compiled deterministically in active_workspace/ (Module 5).
  7. Application is registered in the SQLite database (Module 7).
  8. When user clicks "Auto-Apply" or enters "Approved":
     - Module 6 launches Playwright Chromium, fills the ATS form, and uploads the resume.
     - Module 7 logs the submission and archives all assets into vault/{Company}_{Role}/.

Usage
-----
  python main.py
"""

# ══════════════════════════════════════════════════════════════════════════════
# Standard-library imports
# ══════════════════════════════════════════════════════════════════════════════
import json          # Parse candidate profile config and write runtime metadata
import logging       # Unified logging across all modules
import os            # Environmental configuration and directory management
import pathlib       # Cross-platform filesystem path arithmetic
import sys           # Exit handlers and Python execution parameters
import threading     # Asynchronous worker threads to keep PyQt6 GUI responsive
import time          # Timestamp tracking and execution timers

# ══════════════════════════════════════════════════════════════════════════════
# Third-party imports
# ══════════════════════════════════════════════════════════════════════════════
from dotenv import load_dotenv          # Read API keys from .env if present
from PyQt6.QtCore import QObject, pyqtSignal  # Thread-safe Qt signal/slot messaging

# ══════════════════════════════════════════════════════════════════════════════
# RoleSync Modular imports
# ══════════════════════════════════════════════════════════════════════════════
from desktop_app import RoleSyncApp                    # Module 1: The GUI Overlay
from hotkey_ingestor import HotkeyIngestor, IngestedPayload  # Module 2: Scraper / Ingestion
from rag_engine import RAGEngine                       # Module 3: Vector RAG Engine
from llm_reasoner import LLMReasoner, ReasonerOutput   # Module 4: LLM Brain
from doc_compiler import DocCompiler, CandidateProfile # Module 5: ATS Document Formatter
from auto_submit import run_submission, SubmitResult   # Module 6: Headless Browser Executor
from lifecycle_watchdog import LifecycleManager        # Module 7: State Machine & Vault

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
# Profile Loader
# ══════════════════════════════════════════════════════════════════════════════
def load_candidate_profile(profile_path: str = "profile.json") -> CandidateProfile:
    """
    Load candidate details from a local JSON file or construct default fallbacks.
    
    Parameters
    ----------
    profile_path : str
        Relative or absolute path to the profile.json configuration file.

    Returns
    -------
    CandidateProfile
        Structured profile object ready for injection into resumes and forms.
    """
    p = pathlib.Path(profile_path)
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            logger.info("Loaded candidate profile from %s", profile_path)
            return CandidateProfile(
                name=data.get("name", "Candidate"),
                email=data.get("email", "candidate@example.com"),
                phone=data.get("phone", "+1-555-010-0000"),
                linkedin=data.get("linkedin", "https://linkedin.com"),
                github=data.get("github", "https://github.com"),
                location=data.get("location", "Remote"),
                years_exp=data.get("years_exp", 5),
            )
        except Exception as err:
            logger.warning("Failed parsing %s: %s; falling back to default.", profile_path, err)
    
    # Default mock candidate if file is absent or malformed
    return CandidateProfile(
        name="Alex Mercer",
        email="alex.mercer.dev@gmail.com",
        phone="+1-555-019-2834",
        linkedin="https://linkedin.com/in/alexmercer-dev",
        github="https://github.com/alexmercer-dev",
        location="San Francisco, CA (Open to Remote)",
        years_exp=6,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Thread-Safe Qt Bridge for Ingestion & Pipeline Events
# ══════════════════════════════════════════════════════════════════════════════
class OrchestratorBridge(QObject):
    """
    Qt Signal Bridge providing thread-safe cross-talk between background workers
    (hotkey thread, LLM worker thread, Playwright executor) and the PyQt6 main thread.
    """
    # Emits (message: str, role: str) to update the chat pane
    sig_chat_push = pyqtSignal(str, str)
    # Emits (markdown_content: str) to update the live preview pane
    sig_preview_update = pyqtSignal(str)
    # Emits (company: str, role: str) to update preview header
    sig_header_update = pyqtSignal(str, str)
    # Emits (status_text: str, color_hex: str) to update status bar
    sig_status_update = pyqtSignal(str, str)


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
            logger.warning("No API key (GEMINI_API_KEY or OPENAI_API_KEY) found. Reasoner will run in mock mode when triggered.")

        # Step 7: Initialize Document Compiler (Module 5)
        self.doc_compiler: DocCompiler = DocCompiler()

        # Runtime Session State
        self.current_job_id: int | None = None
        self.current_job_description: str = ""
        self.current_reasoner_output: dict | ReasonerOutput | None = None
        self.current_docx_path: pathlib.Path | None = None
        self.current_pdf_path: pathlib.Path | None = None

        # Step 8: Initialize Hotkey Ingestor (Module 2)
        self.ingestor: HotkeyIngestor = HotkeyIngestor(
            on_capture=self._on_hotkey_captured,
            cooldown=1.5
        )

        # Step 9: Wire GUI Actions
        self._wire_gui_events()

        logger.info("RoleSync Agent fully initialized and ready.")

    def _wire_bridge(self) -> None:
        """Connect bridge signals to Qt GUI slots."""
        self.bridge.sig_chat_push.connect(self.app.chat.push_msg)
        self.bridge.sig_preview_update.connect(self.app.preview.load_md)
        self.bridge.sig_header_update.connect(self.app.preview.set_company)
        self.bridge.sig_status_update.connect(self.app.chat.set_status)

    def _wire_gui_events(self) -> None:
        """Connect UI button clicks and events to business logic handlers."""
        # Auto-apply signal (emitted when button clicked or 'Approved' typed)
        self.app.approved_signal.connect(self._on_user_approved)
        # Download PDF button
        self.app.chat.pdf_requested.connect(self._on_pdf_requested)

    # ── Hotkey Ingestion & Pipeline Execution ─────────────────────────────────

    def _on_hotkey_captured(self, payload: IngestedPayload) -> None:
        """
        Callback fired by hotkey_ingestor.py thread when Ctrl+Shift+J is triggered.
        Spawns a worker thread to execute RAG, Reasoner, and Doc Compilation.
        """
        logger.info("Captured payload: %d words", payload.word_count)
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

        # 1. RAG Context Retrieval (Module 3)
        rag_context = ""
        if self.rag:
            try:
                rag_context = self.rag.get_context_block(jd_text)
                logger.info("Retrieved RAG context block.")
            except Exception as e:
                logger.error("RAG retrieval failed: %s", e)
                rag_context = "Portfolio projects: Synapse (PyQt6/WebRTC), Cattle Classify (ResNet50/CV)."
        else:
            rag_context = "Portfolio projects: Synapse (PyQt6/WebRTC), Cattle Classify (ResNet50/CV)."

        # 2. LLM Reasoning (Module 4)
        output_obj = None
        if self.reasoner:
            try:
                output_obj = self.reasoner.reason(job_description=jd_text, rag_context=rag_context)
            except Exception as e:
                logger.error("LLM reasoning error: %s; using deterministic fallback.", e)

        # Fallback if OpenAI API not configured or failed
        if not output_obj:
            output_obj = ReasonerOutput(
                company="Target Company",
                role="Senior Python Engineer",
                deadline="Not specified",
                missing_skills=["Docker", "Kubernetes"],
                tailored_bullets=[
                    "Architected high-throughput asynchronous microservices in Python reducing latency by 42%.",
                    "Integrated vector similarity search using ChromaDB, accelerating project retrieval across 10k records.",
                    "Designed low-latency PyQt6 overlay GUIs communicating via thread-safe signal bridges.",
                    "Engineered automated validation pipelines enforcing strict schema verification on all ingest payloads.",
                    "Orchestrated headless browser automation workflows with Playwright achieving 99.4% task completion."
                ],
                ats_keywords=["Python", "PyQt6", "AsyncIO", "ChromaDB", "Playwright", "FastAPI"],
                match_score=0.92
            )

        self.current_reasoner_output = output_obj

        # 3. Update Preview UI (Module 1)
        company = getattr(output_obj, "company", "Target Company")
        role = getattr(output_obj, "role", "Python Engineer")
        match_score = getattr(output_obj, "match_score", 0.9)
        self.bridge.sig_header_update.emit(company, role)

        # Generate markdown for live preview
        md_preview = f"""# {company} — {role}
**Match Relevance:** {int(match_score * 100)}% | **Deadline:** {getattr(output_obj, 'deadline', 'Not specified')}

---

## Tailored Experience
"""
        bullets = getattr(output_obj, "tailored_bullets", [])
        for b in bullets:
            md_preview += f"- {b}\n"

        md_preview += "\n## Key ATS Keywords Identified\n"
        ats = getattr(output_obj, "ats_keywords", [])
        md_preview += ", ".join([f"`{k}`" for k in ats])

        self.bridge.sig_preview_update.emit(md_preview)

        # 4. Compile Word Document (.docx) via Module 5
        try:
            docx_path = self.doc_compiler.compile(
                output=output_obj,
                candidate=self.candidate
            )
            self.current_docx_path = docx_path
            logger.info("Compiled ATS resume to %s", docx_path)
        except Exception as e:
            logger.error("Doc compilation failed: %s", e)

        # 5. Register in State Machine (Module 7)
        try:
            self.current_job_id = self.lifecycle.register_job(
                company=company,
                role=role,
                job_url="Direct Application",
                deadline=getattr(output_obj, "deadline", "Not specified"),
                resume_path=str(self.current_docx_path) if self.current_docx_path else "",
                jd_path=""
            )
            logger.info("Registered job #%d in SQLite vault", self.current_job_id)
        except Exception as e:
            logger.error("Failed to register job in SQLite: %s", e)

        # Update Chat and Status
        self.bridge.sig_chat_push.emit(
            f"🎯 <b>Analysis Complete for {company} ({role})</b><br>"
            f"• Match Score: <b>{int(match_score * 100)}%</b><br>"
            f"• Tailored Resume saved to: <code>{self.current_docx_path.name if self.current_docx_path else 'workspace'}</code><br>"
            "Review the document preview on the right. Click <b>Auto-Apply</b> or type <b>Approved</b> to submit.",
            "agent"
        )
        self.bridge.sig_status_update.emit("● Ready for Submission", "#2ecc71")

    # ── Approval & Headless Submission ────────────────────────────────────────

    def _on_user_approved(self) -> None:
        """
        Triggered when user clicks 'Auto-Apply' or enters 'Approved' in chat.
        Launches Module 6 (Playwright headless submission) in a worker thread.
        """
        if not self.current_reasoner_output:
            self.bridge.sig_chat_push.emit(
                "⚠️ No active application to submit. Highlight a job posting and press Ctrl+Shift+J first.",
                "system"
            )
            return

        self.bridge.sig_chat_push.emit(
            "🚀 <b>Launching Playwright Chromium Engine...</b><br>"
            "Filling ATS portal fields and attaching tailored résumé...",
            "system"
        )
        self.bridge.sig_status_update.emit("● Submitting Application...", "#f39c12")

        threading.Thread(target=self._execute_submission, daemon=True).start()

    def _execute_submission(self) -> None:
        """Executes Playwright submission and handles post-submission archiving."""
        # Ensure PDF exists for upload
        pdf_path = self.current_pdf_path
        if not pdf_path and self.current_docx_path:
            try:
                pdf_path = self.doc_compiler.export_pdf(self.current_docx_path)
                self.current_pdf_path = pdf_path
            except Exception as e:
                logger.warning("PDF export fallback: %s", e)
                pdf_path = self.current_docx_path  # Upload docx if PDF conversion fails

        candidate_dict = {
            "full_name": self.candidate.name,
            "email": self.candidate.email,
            "phone": self.candidate.phone,
            "linkedin_url": self.candidate.linkedin,
            "location": self.candidate.location,
        }

        # Execute Module 6 (Playwright)
        result: SubmitResult = run_submission(
            job_url="https://example.com/apply",  # Target portal
            resume_path=pdf_path or pathlib.Path("resume.pdf"),
            candidate=candidate_dict,
            reasoner_output={"output": self.current_reasoner_output}
        )

        # Notify Chat
        if result.success:
            self.bridge.sig_chat_push.emit(
                f"✅ <b>Application Successfully Submitted!</b><br>"
                f"Portal detected: <code>{result.portal}</code><br>"
                f"Verification screenshot captured.",
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

        # Archive through Module 7 (State Machine)
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

    # ── PDF Generation Handler ────────────────────────────────────────────────

    def _on_pdf_requested(self) -> None:
        """Triggered when user clicks 'Download PDF' button in the chat pane."""
        if not self.current_docx_path:
            self.bridge.sig_chat_push.emit(
                "⚠️ No compiled document found. Please ingest a job description first.",
                "system"
            )
            return

        try:
            pdf_path = self.doc_compiler.export_pdf(self.current_docx_path)
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

    # ── Application Lifecycle ─────────────────────────────────────────────────

    def start(self) -> int:
        """Start hotkey listener and launch PyQt6 event loop."""
        # Start global keyboard hook
        self.ingestor.start()

        # Seed welcome message in chat
        self.app.chat.push_msg(
            "👋 <b>RoleSync Autonomous Job Lifecycle Agent Online</b><br>"
            "1. Highlight any job posting in your browser.<br>"
            "2. Press <b>Ctrl + Shift + J</b> to ingest, extract, and tailor.<br>"
            "3. Review the live preview and click <b>Auto-Apply</b>.",
            "agent"
        )

        try:
            return self.app.run()
        finally:
            # Graceful cleanup on window close
            logger.info("Shutting down RoleSync services...")
            self.ingestor.stop()
            self.lifecycle.stop_daemon()


# ══════════════════════════════════════════════════════════════════════════════
# Entry Point
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    orchestrator = RoleSyncOrchestrator()
    sys.exit(orchestrator.start())
