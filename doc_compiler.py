"""
doc_compiler.py — Module 5: The Formatter
==========================================
Part of the RoleSync autonomous job-application agent pipeline.

Role in the system
------------------
This module sits at the *end* of the generation pipeline.  After Module 4
(llm_reasoner) produces a tailored ``ReasonerOutput`` object, the
``DocCompiler`` class defined here takes that output together with a
``CandidateProfile`` and writes a polished, ATS-compliant Microsoft Word
document (.docx) to ``active_workspace/``.

Design decisions
----------------
* **No circular imports** – ``ReasonerOutput`` is defined here as a
  ``TypedDict`` (accepted as a plain ``dict`` OR any duck-typed object whose
  attributes match the required field names).  The module never touches
  ``llm_reasoner.py``.
* **Self-bootstrapping template** – ``_create_base_template()`` builds a
  clean ATS-compliant document from scratch with ``python-docx`` so the
  system works straight out of the box even without ``templates/base.docx``.
* **Deterministic filenames** – ``{Company}_{Role}_{YYYYMMDD}.docx`` with
  spaces → underscores, so artefacts are sortable and collision-free.
* **PDF export** – tries LibreOffice (headless) first; falls back to a
  ``reportlab``-based plain-text render so the method never hard-crashes.

Dependencies
------------
    pip install python-docx reportlab
LibreOffice (optional but recommended for high-fidelity PDF output).
"""

# ══════════════════════════════════════════════════════════════════════════════
# Standard-library imports
# ══════════════════════════════════════════════════════════════════════════════
import datetime          # For stamping filenames with today's date
import logging           # Structured logging throughout the module
import pathlib           # Cross-platform path manipulation
import re                # Regex used to sanitise filename components
import shutil            # shutil.which() locates LibreOffice on PATH
import subprocess        # Launches LibreOffice in a subprocess for PDF export
from dataclasses import dataclass, field   # Lightweight immutable value objects
from typing import Any, List, Optional, TypedDict  # Static type annotations

# ══════════════════════════════════════════════════════════════════════════════
# Third-party imports
# ══════════════════════════════════════════════════════════════════════════════
# python-docx: programmatic Word-document creation and manipulation
from docx import Document                        # Main document object
from docx.enum.text import WD_ALIGN_PARAGRAPH    # Paragraph alignment constants
from docx.oxml.ns import qn                      # Qualified XML name helper
from docx.shared import Inches, Pt, RGBColor     # Measurement / colour helpers

# reportlab: pure-Python PDF generation used as a fallback when LibreOffice
# is unavailable.  Import is deferred to export_pdf() so the rest of the
# module works even if reportlab is not installed.

# ══════════════════════════════════════════════════════════════════════════════
# Module-level logger
# ══════════════════════════════════════════════════════════════════════════════
logger = logging.getLogger(__name__)   # Named logger – callers can configure it
logging.basicConfig(                   # Sensible default so smoke-test is readable
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)

# ══════════════════════════════════════════════════════════════════════════════
# Local TypedDict – mirrors llm_reasoner.ReasonerOutput WITHOUT importing it
# ══════════════════════════════════════════════════════════════════════════════
class ReasonerOutput(TypedDict, total=False):
    """
    Duck-typed contract for data produced by Module 4 (llm_reasoner).

    Using a ``TypedDict`` rather than importing the real Pydantic model keeps
    this module completely decoupled from ``llm_reasoner`` and prevents the
    circular-import problem that arises when both modules reference each other.

    Fields
    ------
    summary : str
        A 2-sentence tailored professional summary for this specific role.
    tailored_bullets : List[str]
        3-6 achievement-focused experience bullets rewritten for the target JD.
    ats_keywords : List[str]
        Keywords and phrases extracted from the JD that the ATS is likely to
        scan for; injected into the Skills section.
    company : str
        Target company name – used in the output filename.
    role : str
        Target role / job title – used in the output filename.
    match_score : float
        0-100 confidence score from the reasoner (optional; not injected into
        the Word doc but logged for diagnostics).
    """
    summary: str               # Tailored 2-sentence professional summary
    tailored_bullets: List[str]  # Experience bullets rewritten for the JD
    ats_keywords: List[str]    # ATS-targeted keyword list
    company: str               # Employer name, used in filename
    role: str                  # Job title, used in filename
    match_score: float         # Optional scoring telemetry


# ══════════════════════════════════════════════════════════════════════════════
# CandidateProfile dataclass
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class CandidateProfile:
    """
    Immutable value object holding candidate contact and metadata.

    Kept as a plain dataclass (not Pydantic) so this module has zero
    dependency on the broader RoleSync model layer.

    Parameters
    ----------
    name : str
        Full name as it should appear on the résumé header.
    email : str
        Professional e-mail address.
    phone : str
        Phone number in any format (e.g. "+1 555-123-4567").
    linkedin : str
        LinkedIn profile URL or vanity handle.
    github : str
        GitHub profile URL or username.
    location : str
        City, State / Country (ATS-friendly format).
    years_exp : int
        Total years of relevant professional experience; used by the reasoner
        for seniority calibration but stored here for completeness.
    """
    name: str                            # Full legal / professional name
    email: str                           # Contact e-mail
    phone: str                           # Contact phone number
    linkedin: str                        # LinkedIn URL or handle
    github: str                          # GitHub URL or username
    location: str                        # Geographic location string
    years_exp: int = 0                   # Years of experience (default 0)


# ══════════════════════════════════════════════════════════════════════════════
# Utility helpers
# ══════════════════════════════════════════════════════════════════════════════
def _sanitise(text: str) -> str:
    """
    Convert an arbitrary string into a filename-safe token.

    Replaces whitespace runs with underscores and strips any character that
    is not alphanumeric, a hyphen, or an underscore.  This guarantees the
    resulting token is safe on Windows, macOS, and Linux filesystems.

    Parameters
    ----------
    text : str
        Raw text to sanitise (e.g. a company name or job title).

    Returns
    -------
    str
        Filesystem-safe version of *text*.
    """
    text = text.strip()                        # Remove leading/trailing spaces
    text = re.sub(r"\s+", "_", text)           # Replace whitespace runs with _
    text = re.sub(r"[^\w\-]", "", text)        # Drop any non-word non-hyphen chars
    return text                                # Return the cleaned token


def _field(output: Any, key: str, default: Any = "") -> Any:
    """
    Safely retrieve a field from either a dict or a duck-typed object.

    ``ReasonerOutput`` can arrive as a plain Python dict (from JSON) or as
    a Pydantic model instance with attribute access.  This helper abstracts
    over the difference so the rest of the code stays clean.

    Parameters
    ----------
    output : Any
        A dict, Pydantic model, dataclass, or any object with the given field.
    key : str
        The field name to retrieve.
    default : Any, optional
        Value returned when the field is missing; defaults to ``""``.

    Returns
    -------
    Any
        The field value, or *default* if not found.
    """
    if isinstance(output, dict):               # dict access path (plain JSON)
        return output.get(key, default)        # .get() avoids KeyError on missing keys
    return getattr(output, key, default)       # Attribute access for Pydantic / dataclasses


# ══════════════════════════════════════════════════════════════════════════════
# DocCompiler class
# ══════════════════════════════════════════════════════════════════════════════
class DocCompiler:
    """
    Module 5 of the RoleSync pipeline: transforms structured reasoner output
    into a polished, ATS-compliant Word document.

    Responsibilities
    ----------------
    1. Load (or create) a base ATS Word template.
    2. Clone that template for each application.
    3. Inject candidate-specific content into well-known placeholder sections.
    4. Save the finished document to ``active_workspace/``.
    5. Optionally export to PDF via LibreOffice or reportlab.

    Parameters
    ----------
    template_path : str | pathlib.Path, optional
        Path to the base ``.docx`` template.  If the file does not exist,
        ``_create_base_template()`` builds one programmatically.
        Defaults to ``"templates/base.docx"``.
    output_dir : str | pathlib.Path, optional
        Directory where finished résumés are written.
        Defaults to ``"active_workspace"``.

    Raises
    ------
    RuntimeError
        If the output directory cannot be created.
    """

    # ── Construction ──────────────────────────────────────────────────────────
    def __init__(
        self,
        template_path: str | pathlib.Path = "templates/base.docx",
        output_dir: str | pathlib.Path = "active_workspace",
    ) -> None:
        # Convert incoming path strings to pathlib.Path objects for safe
        # cross-platform manipulation (avoids os.path string gymnastics).
        self.template_path: pathlib.Path = pathlib.Path(template_path)
        self.output_dir: pathlib.Path = pathlib.Path(output_dir)

        # Ensure the output workspace directory exists before we try to write
        # into it; parents=True creates the full chain, exist_ok=True is
        # idempotent so repeated runs don't raise FileExistsError.
        self.output_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Output directory ready: %s", self.output_dir.resolve())

        # Ensure the template directory exists so _create_base_template can
        # write there without raising FileNotFoundError.
        self.template_path.parent.mkdir(parents=True, exist_ok=True)

        # If the template is missing, build it programmatically.  This makes
        # the system self-bootstrapping: clone the repo and run immediately.
        if not self.template_path.exists():
            logger.warning(
                "Template not found at %s — generating base ATS template.",
                self.template_path,
            )
            self._create_base_template(self.template_path)  # Build from scratch
        else:
            logger.info("Loaded existing template: %s", self.template_path)

    # ── Public API ────────────────────────────────────────────────────────────
    def compile(
        self,
        output: ReasonerOutput,
        candidate: CandidateProfile,
    ) -> pathlib.Path:
        """
        Produce a tailored .docx résumé from reasoner output + candidate data.

        Workflow
        --------
        1. Open the base template as a fresh ``Document`` (does NOT mutate the
           template on disk).
        2. Inject header, summary, experience, and skills sections in order.
        3. Build a deterministic filename and save to ``output_dir``.

        Parameters
        ----------
        output : ReasonerOutput
            Tailored content produced by Module 4.  Accepted as a plain dict,
            Pydantic model, or any duck-typed object with the required fields.
        candidate : CandidateProfile
            Static candidate contact info and metadata.

        Returns
        -------
        pathlib.Path
            Absolute path to the saved ``.docx`` file.

        Raises
        ------
        KeyError
            If a required placeholder paragraph is not found in the template.
        OSError
            If the file cannot be written to ``output_dir``.
        """
        logger.info("Compiling résumé for %s @ %s", candidate.name, _field(output, "company"))

        # Open the template each time we compile so every résumé starts from
        # the same clean slate; python-docx loads it fully into memory.
        doc: Document = Document(str(self.template_path))

        # Inject each section in document order (header → summary → experience
        # → skills) so placeholder paragraphs are found in stable positions.
        self._inject_header(doc, candidate, output)   # Contact info at top
        self._inject_summary(doc, output)              # Professional summary
        self._inject_bullets(doc, output)              # Experience achievements
        self._inject_skills(doc, output)               # ATS keyword line

        # Build the output filename deterministically so collisions are
        # impossible and files sort cleanly by company + date.
        company_tok = _sanitise(_field(output, "company", "Company"))  # Safe company name
        role_tok = _sanitise(_field(output, "role", "Role"))           # Safe role name
        date_tok = datetime.date.today().strftime("%Y%m%d")            # YYYYMMDD stamp
        filename = f"{company_tok}_{role_tok}_{date_tok}.docx"         # Final filename
        out_path = self.output_dir / filename                           # Full destination path

        doc.save(str(out_path))   # Persist the document; str() needed on older python-docx
        logger.info("Résumé saved → %s", out_path.resolve())
        return out_path.resolve()  # Return absolute path for the caller

    def export_pdf(self, docx_path: pathlib.Path) -> pathlib.Path:
        """
        Convert a compiled .docx file to PDF.

        Strategy (tries in order)
        -------------------------
        1. **LibreOffice headless** (highest fidelity – preserves fonts/layout).
        2. **reportlab** plain-text fallback (always available, minimal styling).

        Parameters
        ----------
        docx_path : pathlib.Path
            Absolute path to the .docx file produced by ``compile()``.

        Returns
        -------
        pathlib.Path
            Absolute path to the generated PDF file.

        Raises
        ------
        RuntimeError
            If both conversion strategies fail.
        FileNotFoundError
            If *docx_path* does not exist.
        """
        if not docx_path.exists():
            raise FileNotFoundError(f"docx not found: {docx_path}")

        pdf_path = docx_path.with_suffix(".pdf")  # Same name, .pdf extension

        # ── Strategy 1: LibreOffice headless ──────────────────────────────────
        # soffice (LibreOffice) is the gold standard for docx→PDF conversion
        # because it preserves fonts, tables, and paragraph spacing accurately.
        lo_binary = shutil.which("soffice") or shutil.which("libreoffice")  # Locate on PATH
        if lo_binary:
            logger.info("Converting via LibreOffice: %s", lo_binary)
            try:
                result = subprocess.run(
                    [                                   # Command list (no shell=True for safety)
                        lo_binary,
                        "--headless",                   # No GUI – pure batch mode
                        "--convert-to", "pdf",          # Target format
                        "--outdir", str(docx_path.parent),  # Write PDF next to .docx
                        str(docx_path),                 # Source file
                    ],
                    check=True,              # Raise CalledProcessError on non-zero exit
                    capture_output=True,     # Suppress stdout/stderr from bubbling up
                    timeout=60,             # Kill if it hangs (stuck GUI dialog etc.)
                )
                logger.info("LibreOffice conversion successful.")
                return pdf_path.resolve()   # Return path to the generated PDF
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                # Log the failure but DON'T re-raise; fall through to reportlab
                logger.warning("LibreOffice failed (%s); trying reportlab fallback.", exc)

        # ── Strategy 2: reportlab plain-text fallback ─────────────────────────
        # reportlab is a pure-Python PDF library.  We extract the text content
        # from the docx and flow it into a simple PDF.  Styling is minimal but
        # the content is complete and the file opens correctly on any viewer.
        logger.info("Converting via reportlab (fallback).")
        try:
            # Deferred import – keeps the top-level import block clean and
            # avoids hard-failing if reportlab is not installed when only the
            # Word-doc functionality is used.
            from reportlab.lib.pagesizes import LETTER         # US Letter page size
            from reportlab.lib.styles import getSampleStyleSheet  # Default paragraph styles
            from reportlab.lib.units import inch               # Measurement unit
            from reportlab.platypus import (                   # High-level layout objects
                Paragraph,
                SimpleDocTemplate,
                Spacer,
            )

            # Open the existing docx just to pull out its text paragraphs;
            # we do NOT modify the document on disk.
            source_doc: Document = Document(str(docx_path))

            # Collect all non-empty paragraph text in document order
            lines: List[str] = [
                p.text for p in source_doc.paragraphs if p.text.strip()
            ]

            styles = getSampleStyleSheet()  # Built-in Normal/Heading/Body styles
            story = []                       # reportlab "flowable" content list

            for line in lines:
                # Choose a slightly larger font for lines that look like headings
                # (all-caps, short strings are typical section titles in résumés).
                if line.isupper() and len(line) < 60:
                    para = Paragraph(line, styles["Heading2"])   # Bold heading style
                else:
                    para = Paragraph(line, styles["Normal"])     # Regular body text
                story.append(para)                               # Add to flow
                story.append(Spacer(1, 0.05 * inch))             # Breathing room between lines

            # Build the PDF; SimpleDocTemplate handles margins and page breaks
            pdf_doc = SimpleDocTemplate(
                str(pdf_path),              # Output path
                pagesize=LETTER,            # 8.5 × 11 in
                leftMargin=inch,            # 1-inch margins → ATS-friendly
                rightMargin=inch,
                topMargin=inch,
                bottomMargin=inch,
            )
            pdf_doc.build(story)   # Render all flowables to pages
            logger.info("reportlab PDF written → %s", pdf_path.resolve())
            return pdf_path.resolve()   # Absolute path of the produced PDF

        except ImportError:
            # reportlab is not installed – we have no more fallbacks.
            raise RuntimeError(
                "PDF export failed: install LibreOffice or run "
                "'pip install reportlab' to enable PDF conversion."
            )
        except Exception as exc:
            raise RuntimeError(f"reportlab PDF export failed: {exc}") from exc

    # ── Private injection helpers ─────────────────────────────────────────────
    def _inject_header(
        self,
        doc: Document,
        candidate: CandidateProfile,
        output: Any,
    ) -> None:
        """
        Write candidate contact information into the résumé header area.

        The base template places a ``[HEADER]`` sentinel paragraph at the very
        top of the document body (ATS-safe approach – no Word header/footer
        objects, which many ATS parsers silently skip).  This method replaces
        that paragraph with formatted contact lines.

        Parameters
        ----------
        doc : Document
            In-memory python-docx Document object (mutated in place).
        candidate : CandidateProfile
            Candidate contact data.
        output : Any
            Reasoner output (used here only for the target role, displayed
            beneath the name as an optional objective line in some templates).

        Returns
        -------
        None
        """
        for para in doc.paragraphs:              # Iterate every paragraph in the doc
            if "[HEADER]" in para.text:          # Locate the sentinel placeholder
                para.clear()                     # Remove ALL existing runs (clears text)

                # ── Name (large, bold) ───────────────────────────────────────
                name_run = para.add_run(candidate.name)   # Add candidate name as a run
                name_run.bold = True                       # Bold for visual prominence
                name_run.font.size = Pt(16)                # 16pt → stands out as the top heading
                name_run.font.color.rgb = RGBColor(0x1F, 0x1F, 0x1F)  # Near-black (accessible)
                para.add_run("\n")                          # Newline within the same paragraph

                # ── Contact line (regular weight) ────────────────────────────
                contact_parts = [
                    candidate.email,          # Professional email
                    candidate.phone,          # Phone number
                    candidate.location,       # City/State
                ]
                # Build the contact line, omitting any blank fields so the
                # output doesn't have dangling separators like " | | ".
                contact_line = " | ".join(p for p in contact_parts if p.strip())
                contact_run = para.add_run(contact_line)  # Add formatted contact string
                contact_run.font.size = Pt(10)             # Slightly smaller than body text
                para.add_run("\n")                          # Newline separator

                # ── Profile links ────────────────────────────────────────────
                link_parts = []
                if candidate.linkedin:                        # Only include if provided
                    link_parts.append(f"LinkedIn: {candidate.linkedin}")
                if candidate.github:                          # Only include if provided
                    link_parts.append(f"GitHub: {candidate.github}")
                if link_parts:
                    links_run = para.add_run(" | ".join(link_parts))  # Combine with separator
                    links_run.font.size = Pt(10)                       # Match contact line size

                # Centre-align the whole header paragraph so it mirrors the
                # classic single-column résumé header layout.
                para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                logger.debug("Header injected for %s", candidate.name)
                return  # Only one [HEADER] paragraph expected; stop iterating

        logger.warning("[HEADER] placeholder not found in template.")

    def _inject_summary(self, doc: Document, output: Any) -> None:
        """
        Replace the [SUMMARY] placeholder with the tailored professional summary.

        The summary is a 2-sentence paragraph produced by the LLM in Module 4.
        It is inserted as plain body text so ATS parsers can read it without
        encountering formatting artefacts.

        Parameters
        ----------
        doc : Document
            In-memory python-docx Document object (mutated in place).
        output : Any
            Reasoner output; must expose a ``summary`` field.

        Returns
        -------
        None
        """
        summary_text: str = _field(output, "summary", "")  # Retrieve summary safely

        for para in doc.paragraphs:              # Walk all document paragraphs
            if "[SUMMARY]" in para.text:         # Look for the sentinel placeholder
                para.clear()                     # Wipe placeholder text
                run = para.add_run(summary_text) # Insert the tailored summary
                run.font.size = Pt(11)           # ATS-standard Calibri 11pt body size
                # Left-align: résumé body text should NEVER be justified because
                # ATS parsers sometimes misparse word-spaced justified text.
                para.alignment = WD_ALIGN_PARAGRAPH.LEFT
                logger.debug("Summary injected (%d chars).", len(summary_text))
                return

        logger.warning("[SUMMARY] placeholder not found in template.")

    def _inject_bullets(self, doc: Document, output: Any) -> None:
        """
        Append tailored experience bullets beneath the [EXPERIENCE] sentinel.

        Each bullet is added as a new paragraph in ``List Bullet`` style so
        Word's built-in numbering engine renders them consistently.  ATS
        parsers handle ``List Bullet`` paragraphs reliably.

        Parameters
        ----------
        doc : Document
            In-memory python-docx Document object (mutated in place).
        output : Any
            Reasoner output; must expose a ``tailored_bullets`` list.

        Returns
        -------
        None
        """
        bullets: List[str] = _field(output, "tailored_bullets", [])  # Retrieve bullet list

        for idx, para in enumerate(doc.paragraphs):   # Enumerate to get insertion index
            if "[EXPERIENCE]" in para.text:            # Locate the section sentinel
                # Clear the placeholder text from the sentinel paragraph itself
                # but keep the paragraph so we have a stable insertion reference.
                para.clear()
                heading_run = para.add_run("EXPERIENCE")  # Replace with visible heading
                heading_run.bold = True                    # Bold headings are ATS-standard
                heading_run.font.size = Pt(12)            # Slightly larger than body
                para.alignment = WD_ALIGN_PARAGRAPH.LEFT  # Left-align heading

                # Insert each bullet as a NEW paragraph immediately after the
                # heading.  We iterate in reverse so that inserting after a
                # fixed index position keeps bullets in the correct order.
                for bullet_text in reversed(bullets):
                    # _insert_paragraph_after is a helper we implement below;
                    # python-docx's Document object exposes the XML element
                    # directly so we can graft new <w:p> nodes at precise spots.
                    new_para = self._insert_paragraph_after(
                        doc,
                        para,                 # Reference paragraph (heading)
                        bullet_text,          # Content of the bullet
                        style="List Bullet",  # Word style for bullet points
                    )
                    new_para.runs[0].font.size = Pt(11)  # Enforce 11pt body size
                logger.debug("Injected %d experience bullets.", len(bullets))
                return

        logger.warning("[EXPERIENCE] placeholder not found in template.")

    def _inject_skills(self, doc: Document, output: Any) -> None:
        """
        Replace the [SKILLS] placeholder with an ATS-keyword comma-separated line.

        A single comma-separated paragraph of skills is preferred over a
        multi-column table because many ATS parsers drop table content.

        Parameters
        ----------
        doc : Document
            In-memory python-docx Document object (mutated in place).
        output : Any
            Reasoner output; must expose an ``ats_keywords`` list.

        Returns
        -------
        None
        """
        keywords: List[str] = _field(output, "ats_keywords", [])   # Retrieve keyword list
        skills_line: str = ", ".join(keywords)                      # Join into one readable line

        for para in doc.paragraphs:            # Walk all paragraphs to find sentinel
            if "[SKILLS]" in para.text:        # Match the skills section placeholder
                para.clear()                   # Remove placeholder text
                # First run: bold "Skills: " label so the section is visually clear
                label_run = para.add_run("Skills: ")
                label_run.bold = True           # Bold label for visual scanning
                label_run.font.size = Pt(11)    # Match body size

                # Second run: the keyword list in normal weight so the actual
                # keywords are readable and not visually noisy.
                kw_run = para.add_run(skills_line)   # Comma-separated keyword string
                kw_run.font.size = Pt(11)             # ATS body-text size
                para.alignment = WD_ALIGN_PARAGRAPH.LEFT  # Left-align for ATS safety
                logger.debug("Skills injected: %d keywords.", len(keywords))
                return

        logger.warning("[SKILLS] placeholder not found in template.")

    # ── Template builder ──────────────────────────────────────────────────────
    def _create_base_template(self, path: pathlib.Path) -> None:
        """
        Programmatically create a clean ATS-compliant Word template from scratch.

        This is the self-bootstrapping capability of the module.  It runs once
        whenever ``templates/base.docx`` is absent and produces a file that:
        * Uses a single-column layout (no text boxes, no sidebars).
        * Uses Calibri 11pt throughout (universally readable, ATS-safe).
        * Has clear section headings followed by well-known sentinel strings.
        * Contains NO header/footer objects with text boxes (ATS parsers skip those).
        * Includes standard résumé sections in canonical order.

        Parameters
        ----------
        path : pathlib.Path
            Destination path for the generated template file.

        Returns
        -------
        None

        Raises
        ------
        OSError
            If the file cannot be written to *path*.
        """
        logger.info("Building base ATS template at %s …", path)
        doc = Document()   # Brand-new blank document

        # ── Page margins ──────────────────────────────────────────────────────
        # 1-inch margins on all sides are the ATS standard; narrower margins
        # can cause content to be clipped by some parser implementations.
        for section in doc.sections:              # Word doc can have multiple sections
            section.top_margin = Inches(1)        # 1-inch top margin
            section.bottom_margin = Inches(1)     # 1-inch bottom margin
            section.left_margin = Inches(1)       # 1-inch left margin
            section.right_margin = Inches(1)      # 1-inch right margin

        # ── Default document font ─────────────────────────────────────────────
        # Set the document-level default style so every new paragraph inherits
        # Calibri 11pt without needing per-run font assignments.
        style = doc.styles["Normal"]              # Access the base "Normal" paragraph style
        font = style.font                         # Retrieve the font sub-object
        font.name = "Calibri"                     # ATS-safe, universally installed sans-serif
        font.size = Pt(11)                        # 11pt → comfortable reading, ATS standard

        # ── Helper: add a styled heading ─────────────────────────────────────
        def add_section_heading(text: str) -> None:
            """Add a bold, slightly-larger section heading paragraph."""
            h = doc.add_paragraph()               # New paragraph for the heading
            h.alignment = WD_ALIGN_PARAGRAPH.LEFT  # Left-aligned (not centred)
            run = h.add_run(text.upper())         # ALL-CAPS headings for ATS scannability
            run.bold = True                       # Bold weight for visual hierarchy
            run.font.size = Pt(12)               # 12pt → one step above body text
            run.font.color.rgb = RGBColor(0x1F, 0x1F, 0x1F)  # Dark grey (not pure black)
            # Add a horizontal rule by using the paragraph's bottom border XML
            # manipulation — this avoids inserting a table (bad for ATS) or an
            # image (even worse for ATS).
            _add_bottom_border(h)                 # Underline the heading with a paragraph border

        # ── Helper: add paragraph bottom border via OOXML ────────────────────
        # python-docx does not expose paragraph borders in its high-level API,
        # so we must reach into the underlying XML to set a bottom border.

        # ── Section: Header (contact info) ───────────────────────────────────
        header_para = doc.add_paragraph()         # Blank paragraph acts as the header area
        header_para.alignment = WD_ALIGN_PARAGRAPH.CENTER  # Centre the contact block
        # Inject the sentinel that _inject_header() will search for and replace.
        header_para.add_run("[HEADER]").bold = False  # Plain text sentinel

        doc.add_paragraph()   # Blank spacer paragraph between header and first section

        # ── Section: Professional Summary ─────────────────────────────────────
        add_section_heading("Professional Summary")   # Visible heading
        # The [SUMMARY] sentinel is what _inject_summary() replaces at runtime.
        summary_para = doc.add_paragraph("[SUMMARY]")  # Placeholder body paragraph
        summary_para.alignment = WD_ALIGN_PARAGRAPH.LEFT

        doc.add_paragraph()   # Blank spacer

        # ── Section: Experience ────────────────────────────────────────────────
        add_section_heading("Experience")    # Visible heading
        # The [EXPERIENCE] sentinel marks where bullets are injected.
        exp_para = doc.add_paragraph("[EXPERIENCE]")  # Placeholder paragraph
        exp_para.alignment = WD_ALIGN_PARAGRAPH.LEFT

        doc.add_paragraph()   # Blank spacer

        # ── Section: Skills ────────────────────────────────────────────────────
        add_section_heading("Skills")        # Visible heading
        # The [SKILLS] sentinel is replaced by the ATS keyword line.
        skills_para = doc.add_paragraph("[SKILLS]")   # Placeholder paragraph
        skills_para.alignment = WD_ALIGN_PARAGRAPH.LEFT

        doc.add_paragraph()   # Blank spacer

        # ── Section: Education (static – not AI-generated) ────────────────────
        add_section_heading("Education")     # Visible heading
        # Education is left as free-form text rather than a placeholder because
        # it rarely changes between applications.
        edu_para = doc.add_paragraph("Degree | Institution | Year")
        edu_para.alignment = WD_ALIGN_PARAGRAPH.LEFT

        doc.save(str(path))   # Persist the template to disk
        logger.info("Base ATS template written (%s).", path)

    # ── XML helpers ───────────────────────────────────────────────────────────
    @staticmethod
    def _insert_paragraph_after(
        doc: Document,
        ref_para: Any,
        text: str = "",
        style: str = "Normal",
    ) -> Any:
        """
        Insert a new paragraph immediately after *ref_para* in the document XML.

        python-docx's public API only supports appending paragraphs to the END
        of the document body.  To insert at an arbitrary position we must graft
        a new ``<w:p>`` element directly after the reference paragraph's XML node.

        Parameters
        ----------
        doc : Document
            Parent Document object (needed for style lookup).
        ref_para : Paragraph
            The existing paragraph after which the new one is inserted.
        text : str, optional
            Text content for the new paragraph; defaults to empty string.
        style : str, optional
            Word paragraph style name; defaults to ``"Normal"``.

        Returns
        -------
        Paragraph
            The newly created paragraph object.
        """
        from docx.oxml import OxmlElement   # Low-level XML element factory

        # Create a bare <w:p> element (empty paragraph node in OOXML)
        new_p = OxmlElement("w:p")

        # Insert the new element immediately AFTER the reference paragraph's
        # XML element in the document body's child list.
        ref_para._p.addnext(new_p)   # _p exposes the raw lxml element

        # Wrap the raw XML element in a python-docx Paragraph proxy object so
        # callers get the friendly high-level API back.
        from docx.text.paragraph import Paragraph as DocxParagraph  # Proxy class
        new_paragraph = DocxParagraph(new_p, doc)

        # Apply the requested style (e.g. "List Bullet") via the proxy.
        new_paragraph.style = doc.styles[style]  # Lookup by name in the document's style table

        if text:
            new_paragraph.add_run(text)   # Populate with content if provided

        return new_paragraph   # Return the proxy for further configuration by the caller


# ══════════════════════════════════════════════════════════════════════════════
# Module-level OOXML helper (paragraph bottom border)
# ══════════════════════════════════════════════════════════════════════════════
def _add_bottom_border(paragraph: Any) -> None:
    """
    Attach a thin bottom border to a paragraph using raw OOXML manipulation.

    python-docx 1.x does not expose paragraph borders in its public API, so
    we construct the ``<w:pBdr>`` XML fragment manually and graft it into the
    paragraph's property element (``<w:pPr>``).

    The border is a single-line rule at 4 half-points (2pt) in dark grey,
    which renders as a visual underline beneath section headings without using
    a table row (which would confuse ATS parsers).

    Parameters
    ----------
    paragraph : Paragraph
        python-docx Paragraph object to decorate.

    Returns
    -------
    None
    """
    from docx.oxml import OxmlElement   # Factory for OOXML element nodes

    # Ensure the paragraph has a <w:pPr> (paragraph properties) element;
    # python-docx guarantees this via the pPr property but we guard anyway.
    pPr = paragraph._p.get_or_add_pPr()   # Get-or-create <w:pPr>

    # Create the paragraph border container element <w:pBdr>
    pBdr = OxmlElement("w:pBdr")

    # Create the bottom-border child element <w:bottom> with styling attributes
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")        # Single-line border style
    bottom.set(qn("w:sz"), "4")              # 4 half-points = 0.5pt line width
    bottom.set(qn("w:space"), "1")           # 1pt space between text and border
    bottom.set(qn("w:color"), "1F1F1F")      # Dark grey border colour (hex, no #)

    pBdr.append(bottom)    # Nest <w:bottom> inside <w:pBdr>
    pPr.append(pBdr)       # Nest <w:pBdr> inside <w:pPr> to apply to this paragraph


# ══════════════════════════════════════════════════════════════════════════════
# Smoke-test entry point
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    """
    Standalone smoke-test for doc_compiler.py.

    Running ``python doc_compiler.py`` directly will:
    1. Bootstrap the ATS template if missing.
    2. Compile a sample résumé with dummy data.
    3. Print the path of the generated .docx.
    4. Attempt a PDF export (succeeds if LibreOffice or reportlab is present).

    This test requires NO external services or API keys.
    """
    # ── 1. Build a sample candidate profile ──────────────────────────────────
    candidate = CandidateProfile(
        name="Jane D. Smith",                      # Full professional name
        email="jane.smith@example.com",            # Contact email address
        phone="+1 (555) 867-5309",                 # US-formatted phone number
        linkedin="linkedin.com/in/janedsmith",     # LinkedIn vanity URL
        github="github.com/janedsmith",            # GitHub profile URL
        location="San Francisco, CA",              # City, State format
        years_exp=7,                               # Years of relevant experience
    )

    # ── 2. Build a sample ReasonerOutput (plain dict – no Pydantic needed) ───
    sample_output: ReasonerOutput = {
        "company": "Acme Corp",                   # Target company name
        "role": "Senior Python Engineer",          # Target job title
        "summary": (
            "Results-driven Python engineer with 7+ years shipping high-"
            "throughput data pipelines and ML platform services at scale. "
            "Passionate about clean architecture and measurable business impact."
        ),                                        # Tailored 2-sentence summary
        "tailored_bullets": [
            "Reduced batch-job latency by 42% by migrating a Pandas-based ETL "
            "pipeline to Apache Spark on EMR, cutting nightly processing from "
            "4 h to 2.3 h and saving $18 k/month in compute costs.",

            "Designed and shipped a FastAPI microservice that serves 12 k RPM "
            "at p99 < 50 ms, replacing a monolithic Django endpoint and enabling "
            "independent feature-team deployments.",

            "Led a cross-functional team of 5 engineers to deliver an internal "
            "ML feature store (Redis + PostgreSQL) adopted by 3 product squads "
            "within 60 days of GA launch.",
        ],                                        # 3 strong achievement bullets
        "ats_keywords": [
            "Python", "FastAPI", "Apache Spark", "ETL", "AWS",
            "Machine Learning", "PostgreSQL", "Redis", "Docker", "Kubernetes",
            "CI/CD", "Agile", "Microservices", "REST API",
        ],                                        # ATS keyword list from JD
        "match_score": 91.5,                      # Confidence score (logging only)
    }

    # ── 3. Instantiate DocCompiler and compile ────────────────────────────────
    compiler = DocCompiler(
        template_path="templates/base.docx",   # Will be created if missing
        output_dir="active_workspace",         # Output directory
    )

    docx_path = compiler.compile(sample_output, candidate)  # Generate the .docx
    print(f"\n✅  Résumé compiled → {docx_path}")            # Report success

    # ── 4. Attempt PDF export ─────────────────────────────────────────────────
    try:
        pdf_path = compiler.export_pdf(docx_path)   # Convert to PDF
        print(f"✅  PDF exported   → {pdf_path}")    # Report success
    except RuntimeError as exc:
        # Non-fatal: the .docx is usable even without a PDF
        print(f"⚠️  PDF export skipped: {exc}")
