"""
RoleSync – Module 1: desktop_app.py
─────────────────────────────────────────────────────────────────────────────
PyQt6 transparent overlay agent shell.

Layout (horizontal split):
  LEFT  │ Chat panel  – conversational thread + input bar + action buttons
  RIGHT │ Preview pane – live Markdown→HTML render of the compiled résumé

Public API consumed by other modules
─────────────────────────────────────
  RoleSyncApp          – QApplication wrapper; call .run() to start the event loop
  ChatPanel.push_msg() – append a message bubble to the chat thread (thread-safe)
  PreviewPane.load_md()– re-render markdown in the right pane (thread-safe)
  signal: approved     – emitted when the user types / clicks "Approved"
"""

from __future__ import annotations

import sys
import textwrap
from datetime import datetime
from typing import Literal

import markdown
from PyQt6.QtCore import (
    QSize,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QIcon,
    QPainter,
    QPalette,
    QTextCharFormat,
)
from PyQt6.QtWidgets import (
    QApplication,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# ── Colour palette (dark glass-morphism) ──────────────────────────────────────
_PALETTE = {
    "bg_main":     "#0d0f14",
    "bg_panel":    "#12151c",
    "bg_input":    "#1a1e28",
    "bg_user":     "#1f3a5f",
    "bg_agent":    "#1a1e28",
    "accent":      "#4f8ef7",
    "accent_dark": "#2d5fc4",
    "text_primary":"#e8ecf4",
    "text_muted":  "#7a8299",
    "border":      "#2a2f3f",
    "success":     "#2ecc71",
    "warning":     "#f39c12",
    "danger":      "#e74c3c",
}

_GLOBAL_STYLESHEET = f"""
/* ── Root ──────────────────────────────────────────────── */
QWidget {{
    background: {_PALETTE['bg_main']};
    color: {_PALETTE['text_primary']};
    font-family: 'Inter', 'Segoe UI', sans-serif;
    font-size: 13px;
}}

/* ── Panels ─────────────────────────────────────────────── */
QFrame#panel {{
    background: {_PALETTE['bg_panel']};
    border: 1px solid {_PALETTE['border']};
    border-radius: 12px;
}}

/* ── Scroll areas ───────────────────────────────────────── */
QScrollArea {{
    border: none;
    background: transparent;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 6px;
}}
QScrollBar::handle:vertical {{
    background: {_PALETTE['border']};
    border-radius: 3px;
    min-height: 24px;
}}

/* ── Input bar ──────────────────────────────────────────── */
QLineEdit#chatInput {{
    background: {_PALETTE['bg_input']};
    border: 1px solid {_PALETTE['border']};
    border-radius: 8px;
    padding: 8px 14px;
    color: {_PALETTE['text_primary']};
    selection-background-color: {_PALETTE['accent']};
}}
QLineEdit#chatInput:focus {{
    border-color: {_PALETTE['accent']};
}}

/* ── Action buttons ─────────────────────────────────────── */
QPushButton.action {{
    background: {_PALETTE['accent']};
    color: #fff;
    border: none;
    border-radius: 8px;
    padding: 8px 20px;
    font-weight: 600;
}}
QPushButton.action:hover {{
    background: {_PALETTE['accent_dark']};
}}
QPushButton.action:pressed {{
    background: #1e3fa0;
}}
QPushButton#btnApply {{
    background: {_PALETTE['success']};
}}
QPushButton#btnApply:hover {{
    background: #27ae60;
}}
QPushButton#btnPdf {{
    background: {_PALETTE['bg_input']};
    border: 1px solid {_PALETTE['border']};
    color: {_PALETTE['text_primary']};
}}
QPushButton#btnPdf:hover {{
    border-color: {_PALETTE['accent']};
    color: {_PALETTE['accent']};
}}

/* ── Status bar ─────────────────────────────────────────── */
QLabel#statusLabel {{
    color: {_PALETTE['text_muted']};
    font-size: 11px;
    padding: 4px 10px;
}}

/* ── Preview pane ───────────────────────────────────────── */
QTextBrowser#preview {{
    background: {_PALETTE['bg_panel']};
    border: none;
    padding: 20px;
    color: {_PALETTE['text_primary']};
}}
"""

# ── Preview HTML wrapper ──────────────────────────────────────────────────────
_PREVIEW_CSS = """
<style>
  body  { font-family: 'Georgia', serif; line-height: 1.65;
          color: #e8ecf4; background: transparent; }
  h1    { font-size: 1.5em; border-bottom: 1px solid #2a2f3f; padding-bottom: 6px; }
  h2    { font-size: 1.2em; color: #4f8ef7; margin-top: 1.4em; }
  h3    { font-size: 1em; }
  ul    { padding-left: 1.4em; }
  li    { margin-bottom: 4px; }
  strong{ color: #ffffff; }
  code  { background: #1a1e28; padding: 2px 6px; border-radius: 4px;
          font-size: 0.88em; }
  hr    { border: none; border-top: 1px solid #2a2f3f; margin: 1em 0; }
  .placeholder {{ color: #7a8299; font-style: italic; }}
</style>
"""

_PLACEHOLDER_MD = textwrap.dedent("""\
    # Your Tailored Résumé

    > Highlight a job description and press **Ctrl + Shift + J** to begin.

    ---

    ## Experience

    *Tailored bullets will appear here after the LLM Reasoner processes your job description.*

    ## Skills

    *Relevant skills extracted from your portfolio will be injected automatically.*

    ## Projects

    *RAG-matched projects from your master portfolio will surface here.*
""")


# ══════════════════════════════════════════════════════════════════════════════
# Bubble widget – a single chat message
# ══════════════════════════════════════════════════════════════════════════════

class _Bubble(QFrame):
    """A single rounded chat bubble."""

    def __init__(
        self,
        text: str,
        role: Literal["user", "agent", "system"],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._role = role
        self._setup_ui(text)

    def _setup_ui(self, text: str) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)

        # Row with avatar label + bubble
        row = QHBoxLayout()
        row.setSpacing(8)

        avatar = QLabel(self._avatar_char())
        avatar.setFixedSize(28, 28)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        avatar.setStyleSheet(f"""
            background: {self._avatar_bg()};
            color: #fff;
            border-radius: 14px;
            font-weight: 700;
            font-size: 11px;
        """)

        bubble = QLabel(text)
        bubble.setWordWrap(True)
        bubble.setTextFormat(Qt.TextFormat.RichText)
        bubble.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        bubble.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        bubble.setStyleSheet(f"""
            background: {self._bubble_bg()};
            color: {_PALETTE['text_primary']};
            border-radius: 10px;
            padding: 10px 14px;
            line-height: 1.5;
        """)

        ts = QLabel(datetime.now().strftime("%H:%M"))
        ts.setStyleSheet(f"color: {_PALETTE['text_muted']}; font-size: 10px;")
        ts.setAlignment(Qt.AlignmentFlag.AlignBottom)

        if self._role == "user":
            row.addStretch()
            row.addWidget(bubble)
            row.addWidget(ts)
            row.addWidget(avatar)
        else:
            row.addWidget(avatar)
            row.addWidget(bubble)
            row.addWidget(ts)
            row.addStretch()

        layout.addLayout(row)

    def _avatar_char(self) -> str:
        return {"user": "U", "agent": "RS", "system": "⚙"}.get(self._role, "?")

    def _avatar_bg(self) -> str:
        return {
            "user":   _PALETTE["accent"],
            "agent":  "#6c3fc9",
            "system": _PALETTE["text_muted"],
        }.get(self._role, _PALETTE["border"])

    def _bubble_bg(self) -> str:
        return {
            "user":   _PALETTE["bg_user"],
            "agent":  _PALETTE["bg_agent"],
            "system": _PALETTE["bg_input"],
        }.get(self._role, _PALETTE["bg_input"])


# ══════════════════════════════════════════════════════════════════════════════
# Chat Panel
# ══════════════════════════════════════════════════════════════════════════════

class ChatPanel(QFrame):
    """
    Left pane: scrollable bubble thread + input bar + action buttons.

    Signals
    ───────
    message_sent(str)  – user pressed Enter or clicked Send
    approved()         – user triggered the approval workflow
    pdf_requested()    – user clicked Download PDF
    """

    message_sent   = pyqtSignal(str)
    approved       = pyqtSignal()
    pdf_requested  = pyqtSignal()
    docx_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        self._setup_ui()

    # ── UI construction ───────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        header = self._make_header()
        root.addWidget(header)

        # Scroll area for bubbles
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        self._bubble_container = QWidget()
        self._bubble_layout = QVBoxLayout(self._bubble_container)
        self._bubble_layout.setContentsMargins(14, 14, 14, 14)
        self._bubble_layout.setSpacing(8)
        self._bubble_layout.addStretch()   # pushes bubbles upward

        self._scroll.setWidget(self._bubble_container)
        root.addWidget(self._scroll, stretch=1)

        # Divider
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet(f"border-top: 1px solid {_PALETTE['border']};")
        root.addWidget(divider)

        # Input + buttons strip
        bottom = self._make_bottom_bar()
        root.addWidget(bottom)

    def _make_header(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet(f"background: {_PALETTE['bg_panel']};")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(16, 12, 16, 12)

        icon_lbl = QLabel("⚡")
        icon_lbl.setStyleSheet("font-size: 18px;")

        title = QLabel("RoleSync  Chat")
        title.setStyleSheet("font-size: 15px; font-weight: 700; letter-spacing: 0.5px;")

        self._status_lbl = QLabel("● Idle")
        self._status_lbl.setObjectName("statusLabel")
        self._status_lbl.setStyleSheet(f"color: {_PALETTE['text_muted']}; font-size: 11px;")

        lay.addWidget(icon_lbl)
        lay.addWidget(title)
        lay.addStretch()
        lay.addWidget(self._status_lbl)
        return w

    def _make_bottom_bar(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet(f"background: {_PALETTE['bg_panel']};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(14, 10, 14, 14)
        lay.setSpacing(10)

        # Input row
        input_row = QHBoxLayout()
        self._input = QLineEdit()
        self._input.setObjectName("chatInput")
        self._input.setPlaceholderText('Type a message… or "Approved" to submit')
        self._input.setMinimumHeight(40)
        self._input.returnPressed.connect(self._on_send)

        btn_send = QPushButton("Send ➤")
        btn_send.setProperty("class", "action")
        btn_send.setStyleSheet(
            f"background:{_PALETTE['accent']};color:#fff;"
            f"border:none;border-radius:8px;padding:8px 16px;font-weight:600;"
        )
        btn_send.clicked.connect(self._on_send)

        input_row.addWidget(self._input)
        input_row.addWidget(btn_send)

        # Action buttons row
        action_row = QHBoxLayout()
        action_row.setSpacing(6)

        btn_apply = QPushButton("🚀  Auto-Apply")
        btn_apply.setObjectName("btnApply")
        btn_apply.setProperty("class", "action")
        btn_apply.setMinimumHeight(38)
        btn_apply.clicked.connect(self._on_approved)
        btn_apply.setStyleSheet(
            f"background:{_PALETTE['success']};color:#fff;"
            f"border:none;border-radius:8px;padding:8px 14px;font-weight:600;"
        )

        btn_docx = QPushButton("📄  Word (.docx)")
        btn_docx.setObjectName("btnDocx")
        btn_docx.setMinimumHeight(38)
        btn_docx.clicked.connect(self.docx_requested.emit)
        btn_docx.setStyleSheet(
            f"background:{_PALETTE['bg_input']};color:{_PALETTE['text_primary']};"
            f"border:1px solid {_PALETTE['border']};border-radius:8px;"
            f"padding:8px 10px;"
        )

        btn_pdf = QPushButton("⬇  PDF (.pdf)")
        btn_pdf.setObjectName("btnPdf")
        btn_pdf.setMinimumHeight(38)
        btn_pdf.clicked.connect(self.pdf_requested.emit)
        btn_pdf.setStyleSheet(
            f"background:{_PALETTE['bg_input']};color:{_PALETTE['text_primary']};"
            f"border:1px solid {_PALETTE['border']};border-radius:8px;"
            f"padding:8px 10px;"
        )

        action_row.addWidget(btn_apply, stretch=2)
        action_row.addWidget(btn_docx, stretch=1)
        action_row.addWidget(btn_pdf, stretch=1)

        lay.addLayout(input_row)
        lay.addLayout(action_row)
        return w

    # ── Public API ────────────────────────────────────────────────────────────

    @pyqtSlot(str, str)
    def push_msg(self, text: str, role: Literal["user", "agent", "system"] = "agent") -> None:
        """Append a bubble to the thread. Safe to call from any thread via signal."""
        bubble = _Bubble(text, role)
        # Insert before the trailing stretch (last item)
        idx = self._bubble_layout.count() - 1
        self._bubble_layout.insertWidget(idx, bubble)
        # Auto-scroll to bottom
        QTimer.singleShot(50, self._scroll_to_bottom)

    def set_status(self, text: str, colour: str = _PALETTE["text_muted"]) -> None:
        self._status_lbl.setText(text)
        self._status_lbl.setStyleSheet(f"color:{colour}; font-size:11px; padding:4px 10px;")

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_send(self) -> None:
        text = self._input.text().strip()
        if not text:
            return
        self._input.clear()
        self.push_msg(text, role="user")
        if text.lower() == "approved":
            self._on_approved()
        else:
            self.message_sent.emit(text)

    def _on_approved(self) -> None:
        self.set_status("● Submitting…", _PALETTE["warning"])
        self.push_msg(
            "✅ <b>Approved</b> – handing off to Auto-Submit engine…",
            role="system",
        )
        self.approved.emit()

    def _scroll_to_bottom(self) -> None:
        sb = self._scroll.verticalScrollBar()
        sb.setValue(sb.maximum())


# ══════════════════════════════════════════════════════════════════════════════
# Preview Pane (With In-App Editing & Profile Manager)
# ══════════════════════════════════════════════════════════════════════════════

class PreviewPane(QFrame):
    """
    Right pane: live Markdown → HTML résumé preview, direct in-app editing,
    and integrated Profile & Portfolio Knowledge Base settings view.

    Signals
    ───────
    resume_edited(str) – emitted when user saves manual edits in the editor
    profile_updated()  – emitted when user updates profile/portfolio
    """

    resume_edited   = pyqtSignal(str)
    profile_updated = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        self._current_md: str = _PLACEHOLDER_MD
        self._profile_path = pathlib.Path("f:/agent/profile.json")
        self._portfolio_path = pathlib.Path("f:/agent/master_portfolio.json")

        self._setup_ui()
        self.load_md(_PLACEHOLDER_MD)

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        header = QWidget()
        header.setStyleSheet(f"background:{_PALETTE['bg_panel']};")
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(16, 10, 16, 10)
        h_lay.setSpacing(12)

        self._title_lbl = QLabel("📄  Résumé Preview")
        self._title_lbl.setStyleSheet("font-size: 15px; font-weight: 700;")

        self.btn_edit_toggle = QPushButton("✏️  Edit Résumé")
        self.btn_edit_toggle.setStyleSheet(
            f"background:{_PALETTE['bg_input']}; color:{_PALETTE['accent']};"
            f"border:1px solid {_PALETTE['border']}; border-radius:6px;"
            f"padding:5px 12px; font-size:11px; font-weight:600;"
        )
        self.btn_edit_toggle.clicked.connect(self._toggle_edit_mode)

        self.btn_back_resume = QPushButton("←  Back to Résumé")
        self.btn_back_resume.setStyleSheet(
            f"background:{_PALETTE['bg_input']}; color:{_PALETTE['accent']};"
            f"border:1px solid {_PALETTE['border']}; border-radius:6px;"
            f"padding:5px 12px; font-size:11px; font-weight:600;"
        )
        self.btn_back_resume.clicked.connect(self.show_resume_view)
        self.btn_back_resume.hide()

        self._company_lbl = QLabel("")
        self._company_lbl.setStyleSheet(f"color:{_PALETTE['accent']}; font-size:12px;")

        h_lay.addWidget(self._title_lbl)
        h_lay.addWidget(self.btn_edit_toggle)
        h_lay.addWidget(self.btn_back_resume)
        h_lay.addStretch()
        h_lay.addWidget(self._company_lbl)
        root.addWidget(header)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet(f"border-top: 1px solid {_PALETTE['border']};")
        root.addWidget(divider)

        # Main Stacked Widget
        # Page 0 = HTML Preview
        # Page 1 = In-App Markdown Editor
        # Page 2 = In-App Profile & Portfolio Settings
        self._stack = QStackedWidget()

        # Page 0: HTML Browser
        self._browser = QTextBrowser()
        self._browser.setObjectName("preview")
        self._browser.setOpenExternalLinks(False)
        self._stack.addWidget(self._browser)

        # Page 1: Editor container
        edit_container = QWidget()
        edit_lay = QVBoxLayout(edit_container)
        edit_lay.setContentsMargins(16, 14, 16, 14)
        edit_lay.setSpacing(10)

        edit_hint = QLabel("💡 <b>Direct Edit Mode:</b> Edit your résumé text, bullets, or skills below, then click <b>Apply Changes</b>.")
        edit_hint.setStyleSheet(f"color:{_PALETTE['text_muted']}; font-size:11px;")
        edit_lay.addWidget(edit_hint)

        self._editor = QTextEdit()
        self._editor.setStyleSheet(
            f"background:{_PALETTE['bg_input']}; color:{_PALETTE['text_primary']};"
            f"border:1px solid {_PALETTE['border']}; border-radius:8px;"
            f"font-family:'Consolas', 'Courier New', monospace; font-size:13px; line-height:1.5;"
        )
        edit_lay.addWidget(self._editor, stretch=1)

        # Editor action toolbar
        edit_actions = QHBoxLayout()
        edit_actions.addStretch()

        btn_cancel_edit = QPushButton("Cancel")
        btn_cancel_edit.setStyleSheet(
            f"background:{_PALETTE['bg_input']}; color:{_PALETTE['text_primary']};"
            f"border:1px solid {_PALETTE['border']}; border-radius:6px; padding:6px 14px;"
        )
        btn_cancel_edit.clicked.connect(self._cancel_edit)

        btn_save_edit = QPushButton("💾  Apply Changes")
        btn_save_edit.setStyleSheet(
            f"background:{_PALETTE['success']}; color:#fff;"
            f"border:none; border-radius:6px; padding:6px 18px; font-weight:600;"
        )
        btn_save_edit.clicked.connect(self._save_edit)

        edit_actions.addWidget(btn_cancel_edit)
        edit_actions.addWidget(btn_save_edit)
        edit_lay.addLayout(edit_actions)
        self._stack.addWidget(edit_container)

        # Page 2: In-App Profile & Portfolio Settings
        self._stack.addWidget(self._setup_profile_page())

        root.addWidget(self._stack, stretch=1)

    # ── Page 2: Profile & Portfolio Page Setup ────────────────────────────────

    def _setup_profile_page(self) -> QWidget:
        container = QWidget()
        lay = QVBoxLayout(container)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(10)

        hint = QLabel("👤 <b>Candidate Profile & Truth Source:</b> Update your real details and projects here for mathematical ATS matching.")
        hint.setStyleSheet(f"color:{_PALETTE['accent']}; font-size:12px;")
        lay.addWidget(hint)

        tabs = QTabWidget()
        tabs.setStyleSheet(f"""
            QTabWidget::pane {{ border: 1px solid {_PALETTE['border']}; background: {_PALETTE['bg_panel']}; border-radius: 8px; }}
            QTabBar::tab {{ background: {_PALETTE['bg_input']}; color: {_PALETTE['text_muted']}; padding: 6px 14px; border-radius: 4px; margin-right: 4px; }}
            QTabBar::tab:selected {{ background: {_PALETTE['bg_panel']}; color: {_PALETTE['accent']}; font-weight: 700; }}
        """)

        # Tab 1: Personal Info
        t1 = QWidget()
        f_lay = QFormLayout(t1)
        f_lay.setContentsMargins(14, 14, 14, 14)
        f_lay.setSpacing(8)

        self.prof_name = QLineEdit()
        self.prof_email = QLineEdit()
        self.prof_phone = QLineEdit()
        self.prof_linkedin = QLineEdit()
        self.prof_github = QLineEdit()
        self.prof_location = QLineEdit()
        self.prof_exp = QSpinBox()
        self.prof_exp.setRange(0, 40)
        self.prof_exp.setSuffix(" yrs")

        f_lay.addRow("Full Name:", self.prof_name)
        f_lay.addRow("Email:", self.prof_email)
        f_lay.addRow("Phone:", self.prof_phone)
        f_lay.addRow("LinkedIn:", self.prof_linkedin)
        f_lay.addRow("GitHub:", self.prof_github)
        f_lay.addRow("Location:", self.prof_location)
        f_lay.addRow("Experience:", self.prof_exp)
        tabs.addTab(t1, "👤 Personal & Contact Info")

        # Tab 2: Real Projects & Portfolio
        t2 = QWidget()
        v2 = QVBoxLayout(t2)
        v2.setContentsMargins(14, 14, 14, 14)
        v2.setSpacing(6)
        lbl_p = QLabel("Real Projects & Work History (JSON array matched by ChromaDB vector search):")
        lbl_p.setStyleSheet(f"color:{_PALETTE['text_muted']}; font-size:11px;")
        v2.addWidget(lbl_p)

        self.prof_portfolio = QTextEdit()
        self.prof_portfolio.setStyleSheet(f"font-family:'Consolas','Courier New',monospace; font-size:12px; background:{_PALETTE['bg_input']};")
        v2.addWidget(self.prof_portfolio, stretch=1)
        tabs.addTab(t2, "💼 Real Projects & Experience")

        lay.addWidget(tabs, stretch=1)

        # Bottom actions
        act_row = QHBoxLayout()
        btn_back = QPushButton("←  Cancel & Back")
        btn_back.setStyleSheet(f"background:{_PALETTE['bg_input']}; color:{_PALETTE['text_primary']}; border:1px solid {_PALETTE['border']}; border-radius:6px; padding:6px 14px;")
        btn_back.clicked.connect(self.show_resume_view)

        btn_save = QPushButton("💾  Save & Back to Résumé")
        btn_save.setStyleSheet(f"background:{_PALETTE['success']}; color:#fff; border:none; border-radius:6px; padding:6px 18px; font-weight:600;")
        btn_save.clicked.connect(self._save_profile)

        act_row.addWidget(btn_back)
        act_row.addStretch()
        act_row.addWidget(btn_save)
        lay.addLayout(act_row)

        return container

    # ── View Switching & Profile Data Logic ───────────────────────────────────

    def show_profile_view(self) -> None:
        """Switch to in-app profile view without opening any separate window."""
        self._load_profile_data()
        self._title_lbl.setText("⚙️  Profile & Portfolio Settings")
        self.btn_edit_toggle.hide()
        self.btn_back_resume.show()
        self._company_lbl.hide()
        self._stack.setCurrentIndex(2)

    def show_resume_view(self) -> None:
        """Switch back to live résumé preview."""
        self._title_lbl.setText("📄  Résumé Preview")
        self.btn_edit_toggle.show()
        self.btn_back_resume.hide()
        self._company_lbl.show()
        self._stack.setCurrentIndex(0)

    def _load_profile_data(self) -> None:
        """Load from profile.json and master_portfolio.json."""
        if self._profile_path.exists():
            try:
                data = json.loads(self._profile_path.read_text(encoding="utf-8"))
                self.prof_name.setText(data.get("name", ""))
                self.prof_email.setText(data.get("email", ""))
                self.prof_phone.setText(data.get("phone", ""))
                self.prof_linkedin.setText(data.get("linkedin", ""))
                self.prof_github.setText(data.get("github", ""))
                self.prof_location.setText(data.get("location", ""))
                self.prof_exp.setValue(data.get("years_exp", 0))
            except Exception as e:
                print(f"Error loading profile: {e}")

        if self._portfolio_path.exists():
            try:
                content = self._portfolio_path.read_text(encoding="utf-8")
                self.prof_portfolio.setPlainText(content)
            except Exception as e:
                print(f"Error loading portfolio: {e}")

    def _save_profile(self) -> None:
        """Save profile and portfolio, emit signal, and return to resume view."""
        profile_data = {
            "name": self.prof_name.text().strip() or "Candidate",
            "email": self.prof_email.text().strip(),
            "phone": self.prof_phone.text().strip(),
            "linkedin": self.prof_linkedin.text().strip(),
            "github": self.prof_github.text().strip(),
            "location": self.prof_location.text().strip(),
            "years_exp": self.prof_exp.value(),
        }

        portfolio_text = self.prof_portfolio.toPlainText().strip()
        try:
            parsed = json.loads(portfolio_text)
            if not isinstance(parsed, list):
                raise ValueError("Portfolio must be a JSON array of projects.")
        except Exception as err:
            QMessageBox.warning(self, "Invalid JSON", f"Portfolio JSON error:\n{err}")
            return

        try:
            self._profile_path.write_text(json.dumps(profile_data, indent=2), encoding="utf-8")
            self._portfolio_path.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
            self.profile_updated.emit()
            self.show_resume_view()
        except Exception as err:
            QMessageBox.critical(self, "Save Error", f"Failed saving data: {err}")

    # ── Edit Mode Logic ───────────────────────────────────────────────────────

    def _toggle_edit_mode(self) -> None:
        if self._stack.currentIndex() == 0:
            self._editor.setPlainText(self._current_md)
            self._stack.setCurrentIndex(1)
            self.btn_edit_toggle.setText("👁️  Preview Mode")
        else:
            self._stack.setCurrentIndex(0)
            self.btn_edit_toggle.setText("✏️  Edit Résumé")

    def _cancel_edit(self) -> None:
        self._stack.setCurrentIndex(0)
        self.btn_edit_toggle.setText("✏️  Edit Résumé")

    def _save_edit(self) -> None:
        new_md = self._editor.toPlainText().strip()
        if new_md:
            self._current_md = new_md
            self.load_md(new_md)
            self.resume_edited.emit(new_md)
        self._stack.setCurrentIndex(0)
        self.btn_edit_toggle.setText("✏️  Edit Résumé")

    # ── Public API ────────────────────────────────────────────────────────────

    @pyqtSlot(str)
    def load_md(self, md_text: str) -> None:
        self._current_md = md_text
        body = markdown.markdown(
            md_text,
            extensions=["extra", "codehilite", "nl2br"],
        )
        self.load_html(body)

    @pyqtSlot(str)
    def load_html(self, html_body: str) -> None:
        full = f"<html><head>{_PREVIEW_CSS}</head><body>{html_body}</body></html>"
        self._browser.setHtml(full)

    def set_company(self, company: str, role: str) -> None:
        self._company_lbl.setText(f"{company}  •  {role}")

    def get_markdown(self) -> str:
        return self._current_md


# ══════════════════════════════════════════════════════════════════════════════
# Main Window
# ══════════════════════════════════════════════════════════════════════════════

class _MainWindow(QMainWindow):
    """Frameless transparent overlay window."""

    profile_updated = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._drag_pos = None
        self._setup_window()
        self._setup_central()
        self._connect_internal()

    # ── Window chrome ─────────────────────────────────────────────────────────

    def _setup_window(self) -> None:
        self.setWindowTitle("RoleSync")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(1180, 720)
        self._center_on_screen()

    def _center_on_screen(self) -> None:
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            self.move(
                (geo.width()  - self.width())  // 2,
                (geo.height() - self.height()) // 2,
            )

    # ── Central widget ────────────────────────────────────────────────────────

    def _setup_central(self) -> None:
        # Outer container gives the window its rounded, semi-transparent look
        container = QWidget()
        container.setStyleSheet(
            f"background: rgba(13,15,20,0.96);"
            f"border-radius: 16px;"
            f"border: 1px solid {_PALETTE['border']};"
        )
        outer = QVBoxLayout(container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Title bar (draggable)
        outer.addWidget(self._make_titlebar())

        # Splitter
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setStyleSheet("QSplitter::handle { background: #2a2f3f; width: 2px; }")
        splitter.setHandleWidth(2)
        splitter.setChildrenCollapsible(False)

        self.chat_panel    = ChatPanel()
        self.preview_pane  = PreviewPane()

        splitter.addWidget(self.chat_panel)
        splitter.addWidget(self.preview_pane)
        splitter.setSizes([420, 760])

        outer.addWidget(splitter, stretch=1)
        self.setCentralWidget(container)

    def _make_titlebar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(44)
        bar.setStyleSheet(
            f"background: {_PALETTE['bg_panel']};"
            f"border-top-left-radius: 16px;"
            f"border-top-right-radius: 16px;"
            f"border-bottom: 1px solid {_PALETTE['border']};"
        )
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 0, 10, 0)

        logo = QLabel("⚡ RoleSync")
        logo.setStyleSheet("font-size:14px; font-weight:700; color:#e8ecf4;")

        self._drag_lbl = QLabel("── drag to move ──")
        self._drag_lbl.setStyleSheet(f"color:{_PALETTE['text_muted']}; font-size:11px;")
        self._drag_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Window control buttons
        def _ctrl_btn(symbol: str, colour: str, slot) -> QPushButton:
            b = QPushButton(symbol)
            b.setFixedSize(18, 18)
            b.setStyleSheet(
                f"background:{colour}; border-radius:9px; border:none;"
                f"color:transparent; font-size:9px;"
            )
            b.clicked.connect(slot)
            return b

        btn_close    = _ctrl_btn("✕", _PALETTE["danger"],  self.close)
        btn_minimise = _ctrl_btn("−", _PALETTE["warning"], self.showMinimized)

        btn_profile = QPushButton("⚙️  My Profile & Projects")
        btn_profile.setStyleSheet(
            f"background:{_PALETTE['bg_input']}; color:{_PALETTE['accent']};"
            f"border:1px solid {_PALETTE['border']}; border-radius:6px;"
            f"padding:4px 12px; font-size:11px; font-weight:600;"
        )
        btn_profile.clicked.connect(self._on_profile_clicked)

        lay.addWidget(logo)
        lay.addWidget(btn_profile)
        lay.addWidget(self._drag_lbl, stretch=1)
        lay.addWidget(btn_minimise)
        lay.addWidget(btn_close)
        return bar

    def _on_profile_clicked(self) -> None:
        """Switch right pane to Profile & Portfolio settings view without any popup window."""
        self.preview_pane.show_profile_view()

    # ── Drag-to-move ──────────────────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() == Qt.MouseButton.LeftButton and self._drag_pos is not None:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:
        self._drag_pos = None

    # ── Internal wiring ───────────────────────────────────────────────────────

    def _connect_internal(self) -> None:
        """Wire up panels; external modules connect to chat_panel / preview_pane directly."""
        self.chat_panel.message_sent.connect(self._on_chat_message)
        self.chat_panel.approved.connect(self._on_approved)
        self.chat_panel.pdf_requested.connect(self._on_pdf_requested)
        self.preview_pane.profile_updated.connect(self.profile_updated.emit)

    @pyqtSlot(str)
    def _on_chat_message(self, text: str) -> None:
        # Placeholder echo; real response injected by llm_reasoner
        self.chat_panel.push_msg(
            f"Processing: <i>{text}</i> …",
            role="agent",
        )

    @pyqtSlot()
    def _on_approved(self) -> None:
        # auto_submit.py listens to this signal externally
        self.chat_panel.set_status("● Submitting", _PALETTE["warning"])

    @pyqtSlot()
    def _on_pdf_requested(self) -> None:
        self.chat_panel.push_msg(
            "📥 PDF export queued – check <code>active_workspace/</code>.",
            role="system",
        )


# ══════════════════════════════════════════════════════════════════════════════
# Public entry-point
# ══════════════════════════════════════════════════════════════════════════════

class RoleSyncApp:
    """
    Thin wrapper consumed by main.py (or the orchestrator):

        app = RoleSyncApp()
        # wire external signals here …
        app.run()          # blocks until window closed
    """

    def __init__(self) -> None:
        self._qt_app = QApplication.instance() or QApplication(sys.argv)
        self._qt_app.setStyle("Fusion")
        self._qt_app.setStyleSheet(_GLOBAL_STYLESHEET)
        self._window = _MainWindow()

    # ── Convenience accessors for external modules ────────────────────────────

    @property
    def chat(self) -> ChatPanel:
        return self._window.chat_panel

    @property
    def preview(self) -> PreviewPane:
        return self._window.preview_pane

    @property
    def approved_signal(self):
        """pyqtSignal – connect auto_submit.py here."""
        return self._window.chat_panel.approved

    @property
    def pdf_requested_signal(self):
        return self._window.chat_panel.pdf_requested

    @property
    def docx_requested_signal(self):
        return self._window.chat_panel.docx_requested

    @property
    def resume_edited_signal(self):
        return self._window.preview_pane.resume_edited

    @property
    def profile_updated_signal(self):
        return self._window.profile_updated

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def show(self) -> None:
        self._window.show()

    def run(self) -> int:
        self._window.show()
        return self._qt_app.exec()


# ── Dev harness ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = RoleSyncApp()

    # Seed the chat with a welcome message
    app.chat.push_msg(
        "👋 Welcome to <b>RoleSync</b>!<br>"
        "Highlight any job description in your browser and press "
        "<b>Ctrl + Shift + J</b> to begin tailoring your résumé.",
        role="agent",
    )
    app.chat.push_msg(
        "💡 Tip: Once you're happy with the preview, click "
        "<b>Auto-Apply</b> or type <b>Approved</b>.",
        role="system",
    )

    sys.exit(app.run())
