"""
RoleSync – Module 1: desktop_app.py
─────────────────────────────────────────────────────────────────────────────
Modern SaaS Studio Desktop Application (Pure Python PyQt6).
Linear / Vercel dark aesthetic with 100% Python implementation.

Layout Architecture:
  LEFT  │ AI Copilot Panel – Session switcher, real-time deadline bar,
        │ chat stream, quick action dock (Auto-Apply, PDF, Docx).
  RIGHT │ Document Studio – Segmented tabs:
        │   [📄 ATS Résumé]      Live document render with ATS formatting
        │   [✏️ Markdown Editor] Direct in-app text & bullet editor
        │   [🎯 ATS Analytics]   Visual match score, keyword cloud & gap analysis
        │   [👤 Truth Source]    Candidate profile & master résumé file ingestion

Public API consumed by main.py / orchestrator:
  RoleSyncApp                  – QApplication wrapper (.run())
  ChatPanel.push_msg()         – Thread-safe message bubble pusher
  ChatPanel.set_status()       – Update copilot state indicator
  ChatPanel.set_deadline_display() – Update deadline countdown and alert state
  ChatPanel.update_sessions_dropdown() – Refresh active chat sessions
  ChatPanel.load_messages()    – Load session history
  PreviewPane.load_md()        – Live markdown → styled document render
  PreviewPane.set_company()    – Active company indicator
  PreviewPane.update_ats_analytics() – Update match gauge & keyword cloud
  PreviewPane.show_profile_view() / show_resume_view() – Studio switching
"""

from __future__ import annotations

import json
import pathlib
import sys
import textwrap
from datetime import datetime
from typing import Any, Literal

import markdown
from PyQt6.QtCore import (
    QPoint,
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
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSizeGrip,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# ══════════════════════════════════════════════════════════════════════════════
# Design System: Linear / Vercel Modern SaaS Dark Palette
# ══════════════════════════════════════════════════════════════════════════════
_PALETTE = {
    "bg_window":     "#090d16",       # Deepest canvas
    "bg_main":       "#0d121f",       # App shell background
    "bg_panel":      "#111827",       # Elevated cards & panels
    "bg_card":       "#162033",       # Secondary cards / bubble surfaces
    "bg_input":      "#0b101c",       # Input background
    "bg_user":       "#22304d",       # User message bubble
    "bg_agent":      "#131c2e",       # Agent message bubble
    "accent":        "#6366f1",       # Electric Indigo primary
    "accent_hover":  "#4f46e5",       # Indigo hover state
    "accent_glow":   "rgba(99, 102, 241, 0.22)",
    "accent_cyan":   "#06b6d4",       # Teal / Cyan secondary
    "text_primary":  "#f8fafc",       # High contrast white
    "text_secondary":"#cbd5e1",       # Soft light text
    "text_muted":    "#64748b",       # Slate grey
    "border":        "#1f293d",       # Refined dark border
    "border_light":  "#2e3d5b",       # Focused border
    "success":       "#10b981",       # Emerald green
    "success_bg":    "rgba(16, 185, 129, 0.12)",
    "warning":       "#f59e0b",       # Amber warning
    "warning_bg":    "rgba(245, 158, 11, 0.12)",
    "danger":        "#ef4444",       # Crimson danger
    "danger_bg":     "rgba(239, 68, 68, 0.12)",
}

_GLOBAL_STYLESHEET = f"""
/* ── Root Application ───────────────────────────────────── */
QWidget {{
    background: {_PALETTE['bg_main']};
    color: {_PALETTE['text_primary']};
    font-family: 'Segoe UI', 'Inter', -apple-system, sans-serif;
    font-size: 13px;
}}

/* ── Panels ─────────────────────────────────────────────── */
QFrame#panel {{
    background: {_PALETTE['bg_panel']};
    border: 1px solid {_PALETTE['border']};
    border-radius: 12px;
}}

/* ── Modern Scrollbars ──────────────────────────────────── */
QScrollArea {{
    border: none;
    background: transparent;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 6px;
    margin: 4px 0px 4px 0px;
}}
QScrollBar::handle:vertical {{
    background: {_PALETTE['border']};
    border-radius: 3px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{
    background: {_PALETTE['accent']};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}

/* ── Inputs ─────────────────────────────────────────────── */
QLineEdit {{
    background: {_PALETTE['bg_input']};
    border: 1px solid {_PALETTE['border']};
    border-radius: 8px;
    padding: 8px 12px;
    color: {_PALETTE['text_primary']};
    font-size: 13px;
}}
QLineEdit:focus {{
    border: 1px solid {_PALETTE['accent']};
    background: #0f1628;
}}

QTextEdit {{
    background: {_PALETTE['bg_input']};
    border: 1px solid {_PALETTE['border']};
    border-radius: 8px;
    padding: 10px;
    color: {_PALETTE['text_primary']};
    selection-background-color: {_PALETTE['accent']};
}}
QTextEdit:focus {{
    border: 1px solid {_PALETTE['accent']};
}}

/* ── Dropdown Combos ────────────────────────────────────── */
QComboBox {{
    background: {_PALETTE['bg_input']};
    border: 1px solid {_PALETTE['border']};
    border-radius: 6px;
    padding: 5px 12px;
    color: {_PALETTE['text_primary']};
    font-size: 12px;
    font-weight: 600;
}}
QComboBox:hover {{
    border-color: {_PALETTE['accent']};
}}
QComboBox::drop-down {{
    border: none;
    width: 20px;
}}
QComboBox QAbstractItemView {{
    background: {_PALETTE['bg_card']};
    border: 1px solid {_PALETTE['border']};
    selection-background-color: {_PALETTE['accent']};
    selection-color: #ffffff;
    border-radius: 6px;
    padding: 4px;
}}
"""

_PREVIEW_CSS = """
<style>
  body {
    font-family: 'Segoe UI', -apple-system, Arial, sans-serif;
    line-height: 1.6;
    color: #e2e8f0;
    background: #0e1424;
    padding: 24px;
    margin: 0;
  }
  .doc-container {
    background: #111827;
    border: 1px solid #1f293d;
    border-radius: 12px;
    padding: 32px 36px;
    box-shadow: 0 4px 20px rgba(0,0,0,0.3);
  }
  h1 {
    font-size: 1.85em;
    font-weight: 800;
    color: #ffffff;
    margin-top: 0;
    margin-bottom: 6px;
    letter-spacing: -0.5px;
  }
  .contact-bar {
    color: #94a3b8;
    font-size: 0.9em;
    border-bottom: 2px solid #1f293d;
    padding-bottom: 14px;
    margin-bottom: 20px;
  }
  h2 {
    font-size: 1.15em;
    font-weight: 700;
    color: #818cf8;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    border-bottom: 1px solid #1f293d;
    padding-bottom: 6px;
    margin-top: 24px;
    margin-bottom: 12px;
  }
  h3 {
    font-size: 1.02em;
    font-weight: 600;
    color: #f1f5f9;
    margin-top: 14px;
    margin-bottom: 4px;
  }
  p {
    margin: 6px 0;
    color: #cbd5e1;
    font-size: 0.95em;
  }
  ul {
    padding-left: 20px;
    margin: 8px 0;
  }
  li {
    margin-bottom: 6px;
    color: #cbd5e1;
    font-size: 0.94em;
    line-height: 1.55;
  }
  strong {
    color: #ffffff;
    font-weight: 600;
  }
  code {
    background: #1e293b;
    color: #38bdf8;
    padding: 2px 7px;
    border-radius: 5px;
    font-size: 0.88em;
    font-family: 'Consolas', monospace;
  }
  hr {
    border: none;
    border-top: 1px solid #1f293d;
    margin: 20px 0;
  }
  blockquote {
    border-left: 3px solid #6366f1;
    margin: 0;
    padding: 8px 16px;
    background: rgba(99, 102, 241, 0.08);
    border-radius: 0 8px 8px 0;
    color: #94a3b8;
  }
</style>
"""

_PLACEHOLDER_MD = textwrap.dedent("""\
    # Your Tailored Résumé
    
    > Highlight any job description in your browser and press **Ctrl + Shift + J** to begin automatic RAG tailoring.
    
    ---
    
    ## Executive Summary
    
    *Tailored summary will be synthesized here based on your master candidate profile and target job requirements.*
    
    ## Core Competencies & ATS Keywords
    
    *Extracted high-density keywords and skills from your portfolio will populate automatically.*
    
    ## Professional Experience
    
    *STAR-format achievement bullets mathematically aligned with the employer's needs will appear here.*
    
    ## Highlighted Projects
    
    *RAG-matched projects from your master truth source will be injected with metrics and technology stacks.*
""")


# ══════════════════════════════════════════════════════════════════════════════
# Bubble Widget – Modern SaaS Message Card
# ══════════════════════════════════════════════════════════════════════════════

class _Bubble(QFrame):
    """A single modern conversational card with distinct roles and avatars."""

    def __init__(
        self,
        text: str,
        role: Literal["user", "agent", "system"],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._role = role
        if isinstance(text, (tuple, list)):
            clean_text = " ".join(str(item) for item in text)
        else:
            clean_text = str(text) if text is not None else ""
        self._setup_ui(clean_text)

    def _setup_ui(self, text: str) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 3, 0, 3)

        row = QHBoxLayout()
        row.setSpacing(10)

        # Avatar Pill
        avatar = QLabel(self._avatar_char())
        avatar.setFixedSize(28, 28)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        avatar.setStyleSheet(f"""
            background: {self._avatar_bg()};
            color: #ffffff;
            border-radius: 14px;
            font-weight: 700;
            font-size: 11px;
            border: 1px solid rgba(255, 255, 255, 0.1);
        """)

        # Message Content Card
        bubble = QLabel(text)
        bubble.setWordWrap(True)
        bubble.setTextFormat(Qt.TextFormat.RichText)
        bubble.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bubble.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bubble.setStyleSheet(f"""
            QLabel {{
                background: {self._bubble_bg()};
                color: {_PALETTE['text_primary']};
                border: 1px solid {self._bubble_border()};
                border-radius: 10px;
                padding: 10px 14px;
                line-height: 1.5;
                font-size: 12.5px;
            }}
        """)

        # Timestamp
        ts = QLabel(datetime.now().strftime("%H:%M"))
        ts.setStyleSheet(f"color:{_PALETTE['text_muted']}; font-size:10px;")
        ts.setAlignment(Qt.AlignmentFlag.AlignBottom)

        if self._role == "user":
            row.addStretch()
            row.addWidget(ts)
            row.addWidget(bubble, stretch=4)
            row.addWidget(avatar)
        else:
            row.addWidget(avatar)
            row.addWidget(bubble, stretch=4)
            row.addWidget(ts)
            row.addStretch()

        layout.addLayout(row)

    def _avatar_char(self) -> str:
        if self._role == "user":
            return "👤"
        if self._role == "agent":
            return "⚡"
        return "⚙️"

    def _avatar_bg(self) -> str:
        if self._role == "user":
            return _PALETTE["accent"]
        if self._role == "agent":
            return "#1e293b"
        return "#334155"

    def _bubble_bg(self) -> str:
        if self._role == "user":
            return _PALETTE["bg_user"]
        if self._role == "agent":
            return _PALETTE["bg_agent"]
        return "#141a29"

    def _bubble_border(self) -> str:
        if self._role == "user":
            return "#3b4d75"
        if self._role == "agent":
            return _PALETTE["border"]
        return _PALETTE["border"]


# ══════════════════════════════════════════════════════════════════════════════
# Left Panel: AI Copilot & Job Application Manager
# ══════════════════════════════════════════════════════════════════════════════

class ChatPanel(QFrame):
    """
    Left panel: Conversational AI thread, session switcher, inline deadline bar,
    and bottom action dock.
    """

    message_sent                = pyqtSignal(str)
    approved                    = pyqtSignal()
    pdf_requested               = pyqtSignal()
    docx_requested              = pyqtSignal()
    session_switched            = pyqtSignal(str)
    new_chat_requested          = pyqtSignal()
    deadline_changed            = pyqtSignal(str)
    test_notification_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        self._current_deadline: str = "Not specified"
        self._setup_ui()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header Bar
        root.addWidget(self._make_header())

        # Session Switcher Strip
        root.addWidget(self._make_session_bar())

        # Inline Deadline & Reminder Strip
        root.addWidget(self._make_deadline_bar())

        # Chat Bubble Thread
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._bubble_container = QWidget()
        self._bubble_layout = QVBoxLayout(self._bubble_container)
        self._bubble_layout.setContentsMargins(14, 14, 14, 14)
        self._bubble_layout.setSpacing(10)
        self._bubble_layout.addStretch()

        self._scroll.setWidget(self._bubble_container)
        root.addWidget(self._scroll, stretch=1)

        # Divider
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet(f"border-top: 1px solid {_PALETTE['border']};")
        root.addWidget(divider)

        # Bottom Input & Action Dock
        root.addWidget(self._make_bottom_bar())

    def _make_header(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet(f"background: {_PALETTE['bg_panel']}; border-bottom: 1px solid {_PALETTE['border']};")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(16, 10, 16, 10)

        icon_lbl = QLabel("🎯")
        icon_lbl.setStyleSheet("font-size: 16px;")

        title = QLabel("Application Copilot")
        title.setStyleSheet("font-size: 14px; font-weight: 700; letter-spacing: 0.3px; color: #ffffff;")

        self._status_lbl = QLabel("● Ready")
        self._status_lbl.setObjectName("statusLabel")
        self._status_lbl.setStyleSheet(f"""
            color: {_PALETTE['success']};
            background: {_PALETTE['success_bg']};
            border-radius: 10px;
            padding: 3px 10px;
            font-size: 11px;
            font-weight: 600;
        """)

        lay.addWidget(icon_lbl)
        lay.addWidget(title)
        lay.addStretch()
        lay.addWidget(self._status_lbl)
        return w

    def _make_session_bar(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet(f"background: {_PALETTE['bg_main']}; border-bottom: 1px solid {_PALETTE['border']};")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(12, 6, 12, 6)
        lay.setSpacing(8)

        lbl = QLabel("🏢")
        lbl.setStyleSheet("font-size: 13px;")

        self.session_combo = QComboBox()
        self.session_combo.setObjectName("sessionCombo")
        self.session_combo.currentIndexChanged.connect(self._on_combo_index_changed)

        self.btn_new_chat = QPushButton("＋ New Chat")
        self.btn_new_chat.setToolTip("Start a new job application chat session")
        self.btn_new_chat.setStyleSheet(f"""
            QPushButton {{
                background: {_PALETTE['bg_input']};
                color: {_PALETTE['accent']};
                border: 1px solid {_PALETTE['border']};
                border-radius: 6px;
                padding: 4px 10px;
                font-size: 11px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                background: {_PALETTE['accent']};
                color: #ffffff;
            }}
        """)
        self.btn_new_chat.clicked.connect(self.new_chat_requested.emit)

        lay.addWidget(lbl)
        lay.addWidget(self.session_combo, stretch=1)
        lay.addWidget(self.btn_new_chat)
        return w

    def _make_deadline_bar(self) -> QWidget:
        w = QWidget()
        w.setObjectName("deadlineBar")
        w.setStyleSheet(f"background: {_PALETTE['bg_main']}; border-bottom: 1px solid {_PALETTE['border']};")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(12, 4, 12, 4)
        lay.setSpacing(6)

        self._deadline_lbl = QLabel("⏰ Deadline: Not specified")
        self._deadline_lbl.setStyleSheet(f"color: {_PALETTE['text_muted']}; font-size: 11px; font-weight: 600;")

        self.btn_edit_deadline = QPushButton("✏️ Set")
        self.btn_edit_deadline.setToolTip("Set or edit deadline for this application")
        self.btn_edit_deadline.setStyleSheet(f"""
            QPushButton {{
                background: {_PALETTE['bg_input']};
                color: {_PALETTE['accent']};
                border: 1px solid {_PALETTE['border']};
                border-radius: 4px;
                padding: 2px 8px;
                font-size: 10px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                background: {_PALETTE['accent']};
                color: #ffffff;
            }}
        """)
        self.btn_edit_deadline.clicked.connect(self._on_edit_deadline_clicked)

        self.btn_test_reminder = QPushButton("🔔 Test Alert")
        self.btn_test_reminder.setToolTip("Trigger a test native notification on this device")
        self.btn_test_reminder.setStyleSheet(f"""
            QPushButton {{
                background: {_PALETTE['bg_input']};
                color: {_PALETTE['text_muted']};
                border: 1px solid {_PALETTE['border']};
                border-radius: 4px;
                padding: 2px 8px;
                font-size: 10px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                color: {_PALETTE['warning']};
                border-color: {_PALETTE['warning']};
            }}
        """)
        self.btn_test_reminder.clicked.connect(self.test_notification_requested.emit)

        # Inline editor widgets (100% in-app, zero modal dialogs, zero UI freezing)
        self._deadline_edit_input = QLineEdit()
        self._deadline_edit_input.setPlaceholderText("YYYY-MM-DD (e.g. 2026-11-20)")
        self._deadline_edit_input.setStyleSheet(f"""
            QLineEdit {{
                background: {_PALETTE['bg_input']};
                color: {_PALETTE['text_primary']};
                border: 1px solid {_PALETTE['accent']};
                border-radius: 4px;
                padding: 2px 8px;
                font-size: 11px;
            }}
        """)
        self._deadline_edit_input.returnPressed.connect(self._save_inline_deadline)
        self._deadline_edit_input.hide()

        self._deadline_save_btn = QPushButton("✓ Save")
        self._deadline_save_btn.setStyleSheet(f"""
            QPushButton {{
                background: {_PALETTE['success']};
                color: #ffffff;
                border: none;
                border-radius: 4px;
                padding: 2px 8px;
                font-size: 10px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                background: #059669;
            }}
        """)
        self._deadline_save_btn.clicked.connect(self._save_inline_deadline)
        self._deadline_save_btn.hide()

        self._deadline_cancel_btn = QPushButton("✕")
        self._deadline_cancel_btn.setToolTip("Cancel edit")
        self._deadline_cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: {_PALETTE['bg_input']};
                color: {_PALETTE['text_muted']};
                border: 1px solid {_PALETTE['border']};
                border-radius: 4px;
                padding: 2px 6px;
                font-size: 10px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                color: {_PALETTE['danger']};
                border-color: {_PALETTE['danger']};
            }}
        """)
        self._deadline_cancel_btn.clicked.connect(self._cancel_inline_deadline)
        self._deadline_cancel_btn.hide()

        lay.addWidget(self._deadline_lbl, stretch=1)
        lay.addWidget(self._deadline_edit_input, stretch=1)
        lay.addWidget(self._deadline_save_btn)
        lay.addWidget(self._deadline_cancel_btn)
        lay.addWidget(self.btn_edit_deadline)
        lay.addWidget(self.btn_test_reminder)
        return w

    def _on_edit_deadline_clicked(self) -> None:
        """Switch to inline editing mode within the bar — zero modal popups or freezing."""
        current = self._current_deadline if self._current_deadline != "Not specified" else ""
        self._deadline_edit_input.setText(current)
        self._deadline_lbl.hide()
        self.btn_edit_deadline.hide()
        self.btn_test_reminder.hide()
        self._deadline_edit_input.show()
        self._deadline_save_btn.show()
        self._deadline_cancel_btn.show()
        self._deadline_edit_input.setFocus()
        self._deadline_edit_input.selectAll()

    def _save_inline_deadline(self) -> None:
        """Save deadline entered in inline field."""
        text = self._deadline_edit_input.text().strip()
        self._cancel_inline_deadline()
        if text:
            self.deadline_changed.emit(text)

    def _cancel_inline_deadline(self) -> None:
        """Return to normal display."""
        self._deadline_edit_input.hide()
        self._deadline_save_btn.hide()
        self._deadline_cancel_btn.hide()
        self._deadline_lbl.show()
        self.btn_edit_deadline.show()
        self.btn_test_reminder.show()

    def _make_bottom_bar(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet(f"background: {_PALETTE['bg_panel']};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(8)

        # Quick Actions Row
        quick_row = QHBoxLayout()
        quick_row.setSpacing(8)

        self._btn_apply = QPushButton("⚡  Auto-Apply")
        self._btn_apply.setToolTip("Submit tailored application autonomously via Playwright")
        self._btn_apply.setStyleSheet(f"""
            QPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {_PALETTE['accent']}, stop:1 #4f46e5);
                color: #ffffff;
                border: none;
                border-radius: 6px;
                padding: 6px 14px;
                font-weight: 700;
                font-size: 11px;
            }}
            QPushButton:hover {{
                background: #4338ca;
            }}
        """)
        self._btn_apply.clicked.connect(self._on_approved)

        self._btn_docx = QPushButton("📄 Word")
        self._btn_docx.setToolTip("Export compiled Word document")
        self._btn_docx.setStyleSheet(f"""
            QPushButton {{
                background: {_PALETTE['bg_input']};
                color: {_PALETTE['text_primary']};
                border: 1px solid {_PALETTE['border']};
                border-radius: 6px;
                padding: 5px 10px;
                font-size: 11px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                border-color: {_PALETTE['accent']};
                color: {_PALETTE['accent']};
            }}
        """)
        self._btn_docx.clicked.connect(self.docx_requested.emit)

        self._btn_pdf = QPushButton("📥 PDF")
        self._btn_pdf.setToolTip("Export ATS-compliant PDF document")
        self._btn_pdf.setStyleSheet(f"""
            QPushButton {{
                background: {_PALETTE['bg_input']};
                color: {_PALETTE['text_primary']};
                border: 1px solid {_PALETTE['border']};
                border-radius: 6px;
                padding: 5px 10px;
                font-size: 11px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                border-color: {_PALETTE['accent']};
                color: {_PALETTE['accent']};
            }}
        """)
        self._btn_pdf.clicked.connect(self.pdf_requested.emit)

        quick_row.addWidget(self._btn_apply)
        quick_row.addWidget(self._btn_docx)
        quick_row.addWidget(self._btn_pdf)
        quick_row.addStretch()
        lay.addLayout(quick_row)

        # Input Row
        input_row = QHBoxLayout()
        input_row.setSpacing(6)

        self._input = QLineEdit()
        self._input.setPlaceholderText("Type a message, \"set deadline...\", or \"Approved\"...")
        self._input.returnPressed.connect(self._on_send)

        btn_send = QPushButton("➤")
        btn_send.setFixedSize(36, 36)
        btn_send.setStyleSheet(f"""
            QPushButton {{
                background: {_PALETTE['accent']};
                color: #ffffff;
                border: none;
                border-radius: 8px;
                font-size: 14px;
                font-weight: 700;
            }}
            QPushButton:hover {{
                background: {_PALETTE['accent_hover']};
            }}
        """)
        btn_send.clicked.connect(self._on_send)

        input_row.addWidget(self._input, stretch=1)
        input_row.addWidget(btn_send)
        lay.addLayout(input_row)

        return w

    # ── Public API ────────────────────────────────────────────────────────────

    def update_sessions_dropdown(self, sessions: list[tuple[str, str]], current_id: str) -> None:
        """Update session dropdown without triggering recursive index-change events."""
        self.session_combo.blockSignals(True)
        self.session_combo.clear()
        target_idx = 0
        for idx, (sid, title) in enumerate(sessions):
            self.session_combo.addItem(title, userData=sid)
            if sid == current_id:
                target_idx = idx
        self.session_combo.setCurrentIndex(target_idx)
        self.session_combo.blockSignals(False)

    def _on_combo_index_changed(self, index: int) -> None:
        if index >= 0:
            sid = self.session_combo.itemData(index)
            if sid:
                self.session_switched.emit(str(sid))

    def clear_messages(self) -> None:
        """Clear all message bubbles from the thread."""
        while self._bubble_layout.count() > 1:
            item = self._bubble_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

    def load_messages(self, messages: list[dict[str, str]]) -> None:
        """Render a list of messages for a session."""
        self.clear_messages()
        for msg in messages:
            self._insert_bubble(msg.get("text", ""), msg.get("role", "agent"))
        QTimer.singleShot(50, self._scroll_to_bottom)

    def _insert_bubble(self, text: str, role: Literal["user", "agent", "system"] = "agent") -> None:
        bubble = _Bubble(text, role)
        idx = max(0, self._bubble_layout.count() - 1)
        self._bubble_layout.insertWidget(idx, bubble)

    @pyqtSlot(str, str)
    def push_msg(self, text: str, role: Literal["user", "agent", "system"] = "agent") -> None:
        """Append a bubble to the thread. Safe to call from any thread via signal."""
        self._insert_bubble(text, role)
        QTimer.singleShot(50, self._scroll_to_bottom)

    def set_status(self, text: str, colour: str = _PALETTE["text_muted"]) -> None:
        self._status_lbl.setText(text)
        self._status_lbl.setStyleSheet(f"""
            color: {colour};
            background: rgba(255, 255, 255, 0.05);
            border-radius: 10px;
            padding: 3px 10px;
            font-size: 11px;
            font-weight: 600;
        """)

    def set_deadline_display(self, deadline: str, status_text: str = "", is_urgent: bool = False) -> None:
        """Update deadline bar with date, countdown status, and alert color."""
        self._current_deadline = deadline or "Not specified"
        if not deadline or deadline in ("Not specified", "None", ""):
            self._deadline_lbl.setText("⏰ Deadline: Not specified")
            self._deadline_lbl.setStyleSheet(f"color: {_PALETTE['text_muted']}; font-size: 11px; font-weight: 600;")
            self.btn_edit_deadline.setText("✏️ Set")
        else:
            status_part = f" — {status_text}" if status_text else ""
            self._deadline_lbl.setText(f"⏰ Deadline: {deadline}{status_part}")
            if is_urgent:
                color = _PALETTE["danger"]
            elif "left" in status_text:
                color = _PALETTE["warning"]
            else:
                color = _PALETTE["success"]
            self._deadline_lbl.setStyleSheet(f"color: {color}; font-size: 11px; font-weight: 600;")
            self.btn_edit_deadline.setText("✏️ Edit")

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
# Right Panel: Document Studio & ATS Analytics
# ══════════════════════════════════════════════════════════════════════════════

class PreviewPane(QFrame):
    """
    Right pane: Multi-tab Document Studio with live ATS render, markdown editor,
    visual ATS match analytics, and candidate profile manager.
    """

    resume_edited   = pyqtSignal(str)
    profile_updated = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        self._current_md: str = _PLACEHOLDER_MD
        workspace_root = pathlib.Path(__file__).resolve().parent
        self._profile_path = workspace_root / "profile.json"
        self._portfolio_path = workspace_root / "master_portfolio.json"

        self._setup_ui()
        self.load_md(_PLACEHOLDER_MD)

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header Toolbar with Segmented Tab Controls
        header = QWidget()
        header.setStyleSheet(f"background:{_PALETTE['bg_panel']}; border-bottom: 1px solid {_PALETTE['border']};")
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(14, 8, 14, 8)
        h_lay.setSpacing(6)

        # Segmented Studio Buttons
        self.btn_tab_resume = QPushButton("📄  ATS Résumé")
        self.btn_tab_editor = QPushButton("✏️  Edit Markdown")
        self.btn_tab_ats    = QPushButton("🎯  ATS Analytics")
        self.btn_tab_profile = QPushButton("👤  Truth Source")

        for idx, btn in enumerate([self.btn_tab_resume, self.btn_tab_editor, self.btn_tab_ats, self.btn_tab_profile]):
            btn.setStyleSheet(self._tab_btn_style(idx == 0))
            btn.clicked.connect(lambda checked, i=idx: self._switch_studio_tab(i))
            h_lay.addWidget(btn)

        h_lay.addStretch()

        self._company_lbl = QLabel("")
        self._company_lbl.setStyleSheet(f"color:{_PALETTE['accent_cyan']}; font-size:12px; font-weight:600;")
        h_lay.addWidget(self._company_lbl)

        root.addWidget(header)

        # Main Stacked Studio
        # Page 0 = HTML Preview
        # Page 1 = In-App Markdown Editor
        # Page 2 = ATS Analytics & Keyword Cloud (NEW)
        # Page 3 = Candidate Profile & Portfolio Manager
        self._stack = QStackedWidget()

        # Page 0: HTML Browser
        self._browser = QTextBrowser()
        self._browser.setObjectName("preview")
        self._browser.setOpenExternalLinks(False)
        self._stack.addWidget(self._browser)

        # Page 1: Markdown Editor
        self._stack.addWidget(self._setup_editor_page())

        # Page 2: ATS Analytics Page
        self._stack.addWidget(self._setup_ats_analytics_page())

        # Page 3: Candidate Truth Source Page
        self._stack.addWidget(self._setup_profile_page())

        root.addWidget(self._stack, stretch=1)

    def _tab_btn_style(self, active: bool) -> str:
        if active:
            return f"""
                QPushButton {{
                    background: {_PALETTE['accent']};
                    color: #ffffff;
                    border: none;
                    border-radius: 6px;
                    padding: 5px 12px;
                    font-size: 11.5px;
                    font-weight: 600;
                }}
            """
        return f"""
            QPushButton {{
                background: {_PALETTE['bg_input']};
                color: {_PALETTE['text_secondary']};
                border: 1px solid {_PALETTE['border']};
                border-radius: 6px;
                padding: 5px 12px;
                font-size: 11.5px;
                font-weight: 500;
            }}
            QPushButton:hover {{
                border-color: {_PALETTE['accent']};
                color: #ffffff;
            }}
        """

    def _switch_studio_tab(self, index: int) -> None:
        """Switch current studio tab and refresh button states."""
        self._stack.setCurrentIndex(index)
        buttons = [self.btn_tab_resume, self.btn_tab_editor, self.btn_tab_ats, self.btn_tab_profile]
        for idx, btn in enumerate(buttons):
            btn.setStyleSheet(self._tab_btn_style(idx == index))

    # ── Page 1: Markdown Editor ───────────────────────────────────────────────

    def _setup_editor_page(self) -> QWidget:
        container = QWidget()
        lay = QVBoxLayout(container)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        hint = QLabel("✏️ <b>Direct Markdown Studio:</b> Fine-tune any section or bullet, then click <b>Apply Changes</b> to recompile instantly.")
        hint.setStyleSheet(f"color:{_PALETTE['accent']}; font-size:11.5px;")
        lay.addWidget(hint)

        self._editor = QTextEdit()
        self._editor.setStyleSheet(f"""
            QTextEdit {{
                background: {_PALETTE['bg_input']};
                color: {_PALETTE['text_primary']};
                border: 1px solid {_PALETTE['border']};
                border-radius: 8px;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 12.5px;
                line-height: 1.5;
                padding: 10px;
            }}
        """)
        lay.addWidget(self._editor, stretch=1)

        actions = QHBoxLayout()
        actions.addStretch()

        btn_cancel = QPushButton("Cancel")
        btn_cancel.setStyleSheet(f"""
            QPushButton {{
                background: {_PALETTE['bg_input']};
                color: {_PALETTE['text_primary']};
                border: 1px solid {_PALETTE['border']};
                border-radius: 6px;
                padding: 6px 14px;
                font-size: 11px;
            }}
        """)
        btn_cancel.clicked.connect(lambda: self._switch_studio_tab(0))

        btn_save = QPushButton("💾  Apply Changes")
        btn_save.setStyleSheet(f"""
            QPushButton {{
                background: {_PALETTE['success']};
                color: #ffffff;
                border: none;
                border-radius: 6px;
                padding: 6px 18px;
                font-weight: 700;
                font-size: 11px;
            }}
            QPushButton:hover {{
                background: #059669;
            }}
        """)
        btn_save.clicked.connect(self._save_edit)

        actions.addWidget(btn_cancel)
        actions.addWidget(btn_save)
        lay.addLayout(actions)
        return container

    def _save_edit(self) -> None:
        new_md = self._editor.toPlainText().strip()
        if new_md:
            self._current_md = new_md
            self.load_md(new_md)
            self.resume_edited.emit(new_md)
        self._switch_studio_tab(0)

    # ── Page 2: ATS Analytics & Match Radar (NEW) ─────────────────────────────

    def _setup_ats_analytics_page(self) -> QWidget:
        container = QWidget()
        lay = QVBoxLayout(container)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(14)

        # Top Score Summary Card
        score_card = QFrame()
        score_card.setStyleSheet(f"""
            QFrame {{
                background: {_PALETTE['bg_card']};
                border: 1px solid {_PALETTE['border']};
                border-radius: 10px;
                padding: 14px;
            }}
        """)
        s_lay = QHBoxLayout(score_card)

        score_info_lay = QVBoxLayout()
        self.lbl_ats_title = QLabel("🎯 <b>ATS Match & Skills Breakdown</b>")
        self.lbl_ats_title.setStyleSheet("font-size: 14px; color: #ffffff;")
        self.lbl_ats_desc = QLabel("Cosine similarity and keyword density analysis between job description and candidate portfolio.")
        self.lbl_ats_desc.setStyleSheet(f"font-size: 11px; color: {_PALETTE['text_muted']};")
        score_info_lay.addWidget(self.lbl_ats_title)
        score_info_lay.addWidget(self.lbl_ats_desc)

        self.lbl_ats_gauge = QLabel("94%")
        self.lbl_ats_gauge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_ats_gauge.setStyleSheet(f"""
            QLabel {{
                background: {_PALETTE['success_bg']};
                color: {_PALETTE['success']};
                border: 2px solid {_PALETTE['success']};
                border-radius: 26px;
                font-size: 18px;
                font-weight: 800;
                padding: 6px 14px;
            }}
        """)

        s_lay.addLayout(score_info_lay, stretch=1)
        s_lay.addWidget(self.lbl_ats_gauge)
        lay.addWidget(score_card)

        # Scrollable Analytics Content
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        c_lay = QVBoxLayout(content)
        c_lay.setContentsMargins(0, 0, 0, 0)
        c_lay.setSpacing(12)

        # Card 1: Matched ATS Keywords Cloud
        kw_box = QFrame()
        kw_box.setStyleSheet(f"background:{_PALETTE['bg_card']}; border:1px solid {_PALETTE['border']}; border-radius:10px; padding:12px;")
        kw_lay = QVBoxLayout(kw_box)
        kw_title = QLabel("✅ <b>Matched ATS Keywords</b>")
        kw_title.setStyleSheet("font-size:12px; color:#ffffff;")
        self.lbl_ats_keywords = QLabel("Python, PyQt6, AsyncIO, ChromaDB, FastAPI, Playwright")
        self.lbl_ats_keywords.setWordWrap(True)
        self.lbl_ats_keywords.setStyleSheet(f"font-size:11.5px; color:{_PALETTE['success']}; line-height: 1.6;")
        kw_lay.addWidget(kw_title)
        kw_lay.addWidget(self.lbl_ats_keywords)
        c_lay.addWidget(kw_box)

        # Card 2: Skill-Gap & Missing Skills
        gap_box = QFrame()
        gap_box.setStyleSheet(f"background:{_PALETTE['bg_card']}; border:1px solid {_PALETTE['border']}; border-radius:10px; padding:12px;")
        gap_lay = QVBoxLayout(gap_box)
        gap_title = QLabel("⚠️ <b>Missing Skills & Skill-Gap Analysis</b>")
        gap_title.setStyleSheet("font-size:12px; color:#ffffff;")
        self.lbl_missing_skills = QLabel("Docker, Kubernetes (Tip: Add these in the 'Truth Source' tab to boost score)")
        self.lbl_missing_skills.setWordWrap(True)
        self.lbl_missing_skills.setStyleSheet(f"font-size:11.5px; color:{_PALETTE['warning']}; line-height: 1.6;")
        gap_lay.addWidget(gap_title)
        gap_lay.addWidget(self.lbl_missing_skills)
        c_lay.addWidget(gap_box)

        # Card 3: Tailored STAR Bullets Preview
        bullets_box = QFrame()
        bullets_box.setStyleSheet(f"background:{_PALETTE['bg_card']}; border:1px solid {_PALETTE['border']}; border-radius:10px; padding:12px;")
        b_lay = QVBoxLayout(bullets_box)
        b_title = QLabel("✨ <b>Generated STAR Achievement Bullets</b>")
        b_title.setStyleSheet("font-size:12px; color:#ffffff;")
        self.lbl_tailored_bullets = QLabel("• Engineered real-time PyQt6 multi-threaded agent architecture...")
        self.lbl_tailored_bullets.setWordWrap(True)
        self.lbl_tailored_bullets.setStyleSheet(f"font-size:11.5px; color:{_PALETTE['text_secondary']}; line-height: 1.6;")
        b_lay.addWidget(b_title)
        b_lay.addWidget(self.lbl_tailored_bullets)
        c_lay.addWidget(bullets_box)

        c_lay.addStretch()
        scroll.setWidget(content)
        lay.addWidget(scroll, stretch=1)

        return container

    def update_ats_analytics(self, reasoner_output: Any) -> None:
        """Update ATS match gauge, matched keyword cloud, and skill gap cards."""
        if not reasoner_output:
            return

        company = getattr(reasoner_output, "company", None) or (reasoner_output.get("company", "") if isinstance(reasoner_output, dict) else "")
        role = getattr(reasoner_output, "role", None) or (reasoner_output.get("role", "") if isinstance(reasoner_output, dict) else "")
        score = getattr(reasoner_output, "match_score", None) or (reasoner_output.get("match_score", 0.9) if isinstance(reasoner_output, dict) else 0.9)
        keywords = getattr(reasoner_output, "ats_keywords", None) or (reasoner_output.get("ats_keywords", []) if isinstance(reasoner_output, dict) else [])
        missing = getattr(reasoner_output, "missing_skills", None) or (reasoner_output.get("missing_skills", []) if isinstance(reasoner_output, dict) else [])
        bullets = getattr(reasoner_output, "tailored_bullets", None) or (reasoner_output.get("tailored_bullets", []) if isinstance(reasoner_output, dict) else [])

        # Format percentage score
        try:
            val = float(score)
            pct = int(val * 100) if val <= 1.0 else int(val)
        except Exception:
            pct = 90

        self.lbl_ats_gauge.setText(f"{pct}%")
        if pct >= 80:
            gauge_color = _PALETTE["success"]
            gauge_bg = _PALETTE["success_bg"]
        elif pct >= 65:
            gauge_color = _PALETTE["warning"]
            gauge_bg = _PALETTE["warning_bg"]
        else:
            gauge_color = _PALETTE["danger"]
            gauge_bg = _PALETTE["danger_bg"]

        self.lbl_ats_gauge.setStyleSheet(f"""
            QLabel {{
                background: {gauge_bg};
                color: {gauge_color};
                border: 2px solid {gauge_color};
                border-radius: 26px;
                font-size: 18px;
                font-weight: 800;
                padding: 6px 14px;
            }}
        """)

        if company or role:
            self.lbl_ats_title.setText(f"🎯 <b>ATS Match for {role or 'Candidate'} @ {company or 'Target Role'}</b>")

        # Keywords tags
        if keywords:
            chips = "  ".join(f"<span style='background:#142921; color:#10b981; border:1px solid #10b981; border-radius:4px; padding:2px 7px;'><b>{kw}</b></span>" for kw in keywords)
            self.lbl_ats_keywords.setText(chips)
        else:
            self.lbl_ats_keywords.setText("<i>Ingest a job description to extract top ATS keywords.</i>")

        # Missing skills tags
        if missing:
            chips_m = "  ".join(f"<span style='background:#2d1f0d; color:#f59e0b; border:1px solid #f59e0b; border-radius:4px; padding:2px 7px;'><b>{sk}</b></span>" for sk in missing)
            self.lbl_missing_skills.setText(chips_m)
        else:
            self.lbl_missing_skills.setText("<i>No missing skills detected! 100% portfolio coverage.</i>")

        # Bullets
        if bullets:
            b_text = "<br><br>".join(f"• {b}" for b in bullets[:5])
            self.lbl_tailored_bullets.setText(b_text)

    # ── Page 3: Candidate Profile & Portfolio Manager ─────────────────────────

    def _setup_profile_page(self) -> QWidget:
        container = QWidget()
        lay = QVBoxLayout(container)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(10)

        hint = QLabel("👤 <b>Candidate Truth Source:</b> Feed your real resume file or edit projects for mathematical ChromaDB ATS matching.")
        hint.setStyleSheet(f"color:{_PALETTE['accent']}; font-size:12px;")
        lay.addWidget(hint)

        # Master Resume File Ingestion Banner
        upload_box = QFrame()
        upload_box.setStyleSheet(f"""
            QFrame {{
                background: {_PALETTE['bg_input']};
                border: 1px dashed {_PALETTE['accent']};
                border-radius: 8px;
            }}
        """)
        u_lay = QHBoxLayout(upload_box)
        u_lay.setContentsMargins(14, 10, 14, 10)
        u_lay.setSpacing(14)

        u_info_lay = QVBoxLayout()
        u_info_lay.setSpacing(2)
        u_title = QLabel("📄 <b>Feed Your Own Master Résumé (.docx, .pdf, .txt)</b>")
        u_title.setStyleSheet("font-size:12px; font-weight:700; color:#ffffff;")
        self.lbl_master_resume = QLabel("Import your original resume to extract your real projects, skills, and contact details as ground-truth.")
        self.lbl_master_resume.setStyleSheet(f"font-size:11px; color:{_PALETTE['text_muted']};")
        u_info_lay.addWidget(u_title)
        u_info_lay.addWidget(self.lbl_master_resume)

        self.btn_upload_resume = QPushButton("📂  Import Resume File")
        self.btn_upload_resume.setStyleSheet(f"""
            QPushButton {{
                background: {_PALETTE['accent']};
                color: #ffffff;
                border: none;
                border-radius: 6px;
                padding: 7px 16px;
                font-weight: 700;
                font-size: 11px;
            }}
            QPushButton:hover {{
                background: {_PALETTE['accent_hover']};
            }}
        """)
        self.btn_upload_resume.clicked.connect(self._on_import_resume_clicked)

        u_lay.addLayout(u_info_lay, stretch=1)
        u_lay.addWidget(self.btn_upload_resume)
        lay.addWidget(upload_box)

        tabs = QTabWidget()
        tabs.setStyleSheet(f"""
            QTabWidget::pane {{ border: 1px solid {_PALETTE['border']}; background: {_PALETTE['bg_panel']}; border-radius: 8px; }}
            QTabBar::tab {{ background: {_PALETTE['bg_input']}; color: {_PALETTE['text_muted']}; padding: 6px 14px; border-radius: 4px; margin-right: 4px; font-size:11px; }}
            QTabBar::tab:selected {{ background: {_PALETTE['accent']}; color: #ffffff; font-weight: 700; }}
        """)

        # Tab 1: Personal Contact Info
        t1 = QWidget()
        t1_lay = QFormLayout(t1)
        t1_lay.setContentsMargins(14, 12, 14, 12)
        t1_lay.setSpacing(8)

        self.prof_name     = QLineEdit()
        self.prof_email    = QLineEdit()
        self.prof_phone    = QLineEdit()
        self.prof_linkedin = QLineEdit()
        self.prof_github   = QLineEdit()
        self.prof_location = QLineEdit()
        self.prof_exp      = QSpinBox()
        self.prof_exp.setRange(0, 40)
        self.prof_exp.setStyleSheet(f"background:{_PALETTE['bg_input']}; border:1px solid {_PALETTE['border']}; border-radius:6px; padding:4px 8px; color:#fff;")

        t1_lay.addRow("Full Name:", self.prof_name)
        t1_lay.addRow("Email:", self.prof_email)
        t1_lay.addRow("Phone:", self.prof_phone)
        t1_lay.addRow("LinkedIn URL:", self.prof_linkedin)
        t1_lay.addRow("GitHub URL:", self.prof_github)
        t1_lay.addRow("Location / Degree:", self.prof_location)
        t1_lay.addRow("Years of Experience:", self.prof_exp)
        tabs.addTab(t1, "Personal Info")

        # Tab 2: Summary & Bio
        t2 = QWidget()
        t2_lay = QVBoxLayout(t2)
        t2_lay.setContentsMargins(14, 12, 14, 12)
        self.prof_summary = QTextEdit()
        self.prof_summary.setPlaceholderText("Professional summary or career profile...")
        t2_lay.addWidget(self.prof_summary)
        tabs.addTab(t2, "Summary & Bio")

        # Tab 3: Education & Experience
        t3 = QWidget()
        t3_lay = QVBoxLayout(t3)
        t3_lay.setContentsMargins(14, 12, 14, 12)
        self.prof_education = QTextEdit()
        self.prof_education.setPlaceholderText("Degrees, university, GPA, leadership, and past work history...")
        t3_lay.addWidget(self.prof_education)
        tabs.addTab(t3, "Education & Experience")

        # Tab 4: Skills & Certifications
        t4 = QWidget()
        t4_lay = QVBoxLayout(t4)
        t4_lay.setContentsMargins(14, 12, 14, 12)
        t4_lay.addWidget(QLabel("Raw Skills (Categorized):"))
        self.prof_skills = QTextEdit()
        self.prof_skills.setMaximumHeight(90)
        t4_lay.addWidget(self.prof_skills)

        t4_lay.addWidget(QLabel("Certifications & Trainings:"))
        self.prof_certs = QTextEdit()
        self.prof_certs.setMaximumHeight(90)
        t4_lay.addWidget(self.prof_certs)
        tabs.addTab(t4, "Skills & Certifications")

        # Tab 5: Portfolio Projects (ChromaDB Truth Source)
        t5 = QWidget()
        t5_lay = QVBoxLayout(t5)
        t5_lay.setContentsMargins(14, 12, 14, 12)
        t5_hint = QLabel("💡 <i>JSON array of real projects loaded into ChromaDB vector store for mathematical matching:</i>")
        t5_hint.setStyleSheet(f"color:{_PALETTE['text_muted']}; font-size:11px;")
        self.prof_projects_json = QTextEdit()
        self.prof_projects_json.setStyleSheet(f"""
            QTextEdit {{
                background:{_PALETTE['bg_input']};
                color:{_PALETTE['text_primary']};
                font-family:'Consolas', monospace;
                font-size:12px;
                border:1px solid {_PALETTE['border']};
            }}
        """)
        t5_lay.addWidget(t5_hint)
        t5_lay.addWidget(self.prof_projects_json)
        tabs.addTab(t5, "Portfolio Projects (JSON)")

        lay.addWidget(tabs, stretch=1)

        # Inline status indicator for errors or notifications (zero modal freezes)
        self.lbl_profile_status = QLabel("")
        self.lbl_profile_status.setStyleSheet("font-size: 11px; padding: 2px 0px;")
        lay.addWidget(self.lbl_profile_status)

        # Bottom actions
        act_row = QHBoxLayout()
        btn_back = QPushButton("← Cancel & Back")
        btn_back.setStyleSheet(f"""
            QPushButton {{
                background: {_PALETTE['bg_input']};
                color: {_PALETTE['text_primary']};
                border: 1px solid {_PALETTE['border']};
                border-radius: 6px;
                padding: 6px 14px;
                font-size: 11px;
            }}
        """)
        btn_back.clicked.connect(lambda: self._switch_studio_tab(0))

        btn_save = QPushButton("💾  Save & Hot-Reload All Applications")
        btn_save.setStyleSheet(f"""
            QPushButton {{
                background: {_PALETTE['success']};
                color: #ffffff;
                border: none;
                border-radius: 6px;
                padding: 6px 18px;
                font-weight: 700;
                font-size: 11px;
            }}
            QPushButton:hover {{
                background: #059669;
            }}
        """)
        btn_save.clicked.connect(self._save_profile)

        act_row.addWidget(btn_back)
        act_row.addStretch()
        act_row.addWidget(btn_save)
        lay.addLayout(act_row)

        return container

    def show_profile_view(self) -> None:
        """Switch to in-app profile view without opening any separate window."""
        self._load_profile_data()
        self._switch_studio_tab(3)

    def show_resume_view(self) -> None:
        """Switch back to live résumé preview."""
        self._switch_studio_tab(0)

    def show_editor_view(self) -> None:
        """Switch to direct markdown editor."""
        self._editor.setPlainText(self._current_md)
        self._switch_studio_tab(1)

    def show_ats_view(self) -> None:
        """Switch to ATS Analytics."""
        self._switch_studio_tab(2)

    def _on_import_resume_clicked(self) -> None:
        """Upload candidate's original resume file (.docx, .pdf, .txt) and extract profile ground-truth."""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Your Master Résumé File",
            str(pathlib.Path.home()),
            "Resume Files (*.docx *.pdf *.txt);;All Files (*)"
        )
        if not file_path:
            return

        source = pathlib.Path(file_path)
        dest_dir = pathlib.Path("vault/master_resume")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = dest_dir / f"master_resume{source.suffix}"

        import shutil
        try:
            shutil.copy2(source, dest_file)
        except Exception as e:
            self.lbl_profile_status.setText(f"❌ Copy failed: {e}")
            self.lbl_profile_status.setStyleSheet(f"color:{_PALETTE['danger']}; font-size:11px;")
            return

        extracted_text = ""
        try:
            if source.suffix.lower() == ".docx":
                import docx
                d = docx.Document(source)
                extracted_text = "\n".join(p.text for p in d.paragraphs if p.text.strip())
            elif source.suffix.lower() == ".pdf":
                try:
                    import pypdf
                    reader = pypdf.PdfReader(source)
                    extracted_text = "\n".join(page.extract_text() or "" for page in reader.pages)
                except Exception:
                    pass
            elif source.suffix.lower() == ".txt":
                extracted_text = source.read_text(encoding="utf-8", errors="ignore")
        except Exception as parse_err:
            self.lbl_profile_status.setText(f"⚠️ Parsing issue: {parse_err}")

        # Update labels and profile fields
        self.lbl_master_resume.setText(f"✅ Ingested: <b>{source.name}</b> ({len(extracted_text)} chars parsed)")
        self.lbl_master_resume.setStyleSheet(f"color:{_PALETTE['success']}; font-size:11px;")

        # Auto-fill summary if present
        if extracted_text and not self.prof_summary.toPlainText().strip():
            self.prof_summary.setPlainText(extracted_text[:600])

        # Save profile metadata
        try:
            p_data = {}
            if self._profile_path.exists():
                p_data = json.loads(self._profile_path.read_text(encoding="utf-8"))
            p_data["master_resume_path"] = str(dest_file.resolve())
            p_data["master_resume_filename"] = source.name
            self._profile_path.write_text(json.dumps(p_data, indent=2), encoding="utf-8")
        except Exception:
            pass

        self.lbl_profile_status.setText(f"✅ Master résumé '{source.name}' saved and ready to recompile!")
        self.lbl_profile_status.setStyleSheet(f"color:{_PALETTE['success']}; font-size:11px; font-weight:600;")
        self.profile_updated.emit()
        self.show_resume_view()

    def _load_profile_data(self) -> None:
        """Load profile.json and master_portfolio.json into UI fields."""
        if self._profile_path.exists():
            try:
                data = json.loads(self._profile_path.read_text(encoding="utf-8"))
                self.prof_name.setText(data.get("name", ""))
                self.prof_email.setText(data.get("email", ""))
                self.prof_phone.setText(data.get("phone", ""))
                self.prof_linkedin.setText(data.get("linkedin", ""))
                self.prof_github.setText(data.get("github", ""))
                self.prof_location.setText(data.get("location", ""))
                self.prof_exp.setValue(int(data.get("years_exp", 0)))
                self.prof_summary.setPlainText(data.get("summary", ""))
                self.prof_education.setPlainText(data.get("education", ""))
                self.prof_skills.setPlainText(data.get("skills_raw", ""))
                self.prof_certs.setPlainText(data.get("certifications", ""))
                res_name = data.get("master_resume_filename", "")
                if res_name:
                    self.lbl_master_resume.setText(f"✅ Active Master Résumé: <b>{res_name}</b>")
                    self.lbl_master_resume.setStyleSheet(f"color:{_PALETTE['success']}; font-size:11px;")
            except Exception as e:
                self.lbl_profile_status.setText(f"Error loading profile: {e}")

        if self._portfolio_path.exists():
            try:
                p_raw = self._portfolio_path.read_text(encoding="utf-8")
                self.prof_projects_json.setPlainText(p_raw)
            except Exception as e:
                self.lbl_profile_status.setText(f"Error loading portfolio: {e}")

    def _save_profile(self) -> None:
        """Validate and write updated profile and portfolio back to disk."""
        existing_profile = {}
        if self._profile_path.exists():
            try:
                existing_profile = json.loads(self._profile_path.read_text(encoding="utf-8"))
            except Exception:
                existing_profile = {}

        profile_data = {
            **existing_profile,
            "name": self.prof_name.text().strip() or "Candidate",
            "email": self.prof_email.text().strip(),
            "phone": self.prof_phone.text().strip(),
            "linkedin": self.prof_linkedin.text().strip(),
            "github": self.prof_github.text().strip(),
            "location": self.prof_location.text().strip(),
            "years_exp": self.prof_exp.value(),
            "summary": self.prof_summary.toPlainText().strip(),
            "education": self.prof_education.toPlainText().strip(),
            "skills_raw": self.prof_skills.toPlainText().strip(),
            "certifications": self.prof_certs.toPlainText().strip(),
        }

        try:
            portfolio_to_save = json.loads(self.prof_projects_json.toPlainText().strip())
        except Exception as err:
            self.lbl_profile_status.setText(f"❌ Portfolio JSON Error: {err}")
            self.lbl_profile_status.setStyleSheet(f"color:{_PALETTE['danger']}; font-size:11px; font-weight:600;")
            return

        try:
            self._profile_path.write_text(json.dumps(profile_data, indent=2), encoding="utf-8")
            self._portfolio_path.write_text(json.dumps(portfolio_to_save, indent=2), encoding="utf-8")
            self.profile_updated.emit()
            self.show_resume_view()
        except Exception as err:
            self.lbl_profile_status.setText(f"❌ Save Error: {err}")
            self.lbl_profile_status.setStyleSheet(f"color:{_PALETTE['danger']}; font-size:11px; font-weight:600;")

    # ── Public API ────────────────────────────────────────────────────────────

    @pyqtSlot(str)
    def load_md(self, md_text: str) -> None:
        self._current_md = md_text
        self._editor.setPlainText(md_text)
        body = markdown.markdown(
            md_text,
            extensions=["extra", "codehilite", "nl2br"],
        )
        self.load_html(f"<div class='doc-container'>{body}</div>")

    @pyqtSlot(str)
    def load_html(self, html_body: str) -> None:
        full = f"<html><head>{_PREVIEW_CSS}</head><body>{html_body}</body></html>"
        self._browser.setHtml(full)

    @pyqtSlot(str)
    def set_company(self, company: str) -> None:
        self._company_lbl.setText(f"Target: {company}" if company else "")


# ══════════════════════════════════════════════════════════════════════════════
# Main Window: Modern SaaS Studio Shell
# ══════════════════════════════════════════════════════════════════════════════

class _MainWindow(QMainWindow):
    """
    Frameless, resizable modern desktop application with custom title bar,
    studio navigation, pin toggle, and smooth window controls.
    """

    profile_updated = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._drag_pos: QPoint | None = None
        self._is_pinned: bool = False

        self._setup_window()
        self._setup_central()
        self._connect_internal()

    def _setup_window(self) -> None:
        self.setWindowTitle("RoleSync Studio — Autonomous Job Lifecycle Agent")
        # Modern Frameless Window (Not Tool Window, so it has taskbar presence)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Window
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(1280, 800)
        self.setMinimumSize(1040, 640)
        self._center_on_screen()

    def _center_on_screen(self) -> None:
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            self.move(
                (geo.width()  - self.width())  // 2,
                (geo.height() - self.height()) // 2,
            )

    def _setup_central(self) -> None:
        container = QWidget()
        container.setObjectName("windowContainer")
        container.setStyleSheet(f"""
            QWidget#windowContainer {{
                background: {_PALETTE['bg_main']};
                border-radius: 12px;
                border: 1px solid {_PALETTE['border']};
            }}
        """)

        outer = QVBoxLayout(container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Workspace panels instantiated first so titlebar can wire navigation slots
        self.chat_panel    = ChatPanel()
        self.preview_pane  = PreviewPane()

        # Custom Top Header / Titlebar
        outer.addWidget(self._make_titlebar())

        # Main Splitter Workspace
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setStyleSheet(f"QSplitter::handle {{ background: {_PALETTE['border']}; width: 2px; }}")
        splitter.setHandleWidth(2)
        splitter.setChildrenCollapsible(False)

        splitter.addWidget(self.chat_panel)
        splitter.addWidget(self.preview_pane)
        splitter.setSizes([450, 830])

        outer.addWidget(splitter, stretch=1)

        # Bottom Status & Resize Bar
        bottom_bar = QWidget()
        bottom_bar.setFixedHeight(24)
        bottom_bar.setStyleSheet(f"background: {_PALETTE['bg_panel']}; border-top: 1px solid {_PALETTE['border']}; border-bottom-left-radius: 12px; border-bottom-right-radius: 12px;")
        b_lay = QHBoxLayout(bottom_bar)
        b_lay.setContentsMargins(12, 0, 4, 0)

        hint_lbl = QLabel("⚡ RoleSync 100% Pure Python Agent • Global Ingestion Hotkey: Ctrl + Shift + J")
        hint_lbl.setStyleSheet(f"color: {_PALETTE['text_muted']}; font-size: 10.5px;")

        size_grip = QSizeGrip(bottom_bar)
        size_grip.setStyleSheet("width: 16px; height: 16px;")

        b_lay.addWidget(hint_lbl)
        b_lay.addStretch()
        b_lay.addWidget(size_grip)

        outer.addWidget(bottom_bar)

        self.setCentralWidget(container)

    def _make_titlebar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(46)
        bar.setStyleSheet(f"""
            background: {_PALETTE['bg_panel']};
            border-top-left-radius: 12px;
            border-top-right-radius: 12px;
            border-bottom: 1px solid {_PALETTE['border']};
        """)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 0, 12, 0)
        lay.setSpacing(12)

        # Logo & App Title
        logo = QLabel("⚡ RoleSync Studio")
        logo.setStyleSheet("font-size: 14px; font-weight: 800; color: #ffffff; letter-spacing: 0.4px;")

        badge = QLabel("PRO AGENT")
        badge.setStyleSheet(f"""
            background: {_PALETTE['accent_glow']};
            color: {_PALETTE['accent']};
            border: 1px solid {_PALETTE['accent']};
            border-radius: 4px;
            padding: 2px 6px;
            font-size: 9.5px;
            font-weight: 800;
        """)

        # Navigation Switcher
        btn_nav_workspace = QPushButton("💬 Studio")
        btn_nav_workspace.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {_PALETTE['text_primary']};
                border: none;
                font-weight: 600;
                font-size: 12px;
                padding: 4px 8px;
            }}
            QPushButton:hover {{
                color: {_PALETTE['accent']};
            }}
        """)
        btn_nav_workspace.clicked.connect(self.preview_pane.show_resume_view)

        btn_nav_profile = QPushButton("👤 Profile & Truth Source")
        btn_nav_profile.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {_PALETTE['text_secondary']};
                border: none;
                font-weight: 600;
                font-size: 12px;
                padding: 4px 8px;
            }}
            QPushButton:hover {{
                color: {_PALETTE['accent']};
            }}
        """)
        btn_nav_profile.clicked.connect(self.preview_pane.show_profile_view)

        # Drag handle area
        self._drag_lbl = QLabel("")
        self._drag_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        # Pin to Top button
        self.btn_pin = QPushButton("📌 Pin")
        self.btn_pin.setToolTip("Toggle Always-on-Top overlay mode")
        self.btn_pin.setStyleSheet(f"""
            QPushButton {{
                background: {_PALETTE['bg_input']};
                color: {_PALETTE['text_muted']};
                border: 1px solid {_PALETTE['border']};
                border-radius: 6px;
                padding: 4px 10px;
                font-size: 11px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                border-color: {_PALETTE['accent']};
                color: {_PALETTE['accent']};
            }}
        """)
        self.btn_pin.clicked.connect(self._toggle_pin)

        # Modern Window Controls
        btn_min = QPushButton("─")
        btn_min.setFixedSize(28, 28)
        btn_min.setStyleSheet(f"""
            QPushButton {{
                background: {_PALETTE['bg_input']};
                color: {_PALETTE['text_muted']};
                border: 1px solid {_PALETTE['border']};
                border-radius: 6px;
                font-size: 12px;
                font-weight: 700;
            }}
            QPushButton:hover {{
                background: {_PALETTE['border']};
                color: #ffffff;
            }}
        """)
        btn_min.clicked.connect(self.showMinimized)

        self.btn_max = QPushButton("◻")
        self.btn_max.setFixedSize(28, 28)
        self.btn_max.setStyleSheet(f"""
            QPushButton {{
                background: {_PALETTE['bg_input']};
                color: {_PALETTE['text_muted']};
                border: 1px solid {_PALETTE['border']};
                border-radius: 6px;
                font-size: 12px;
                font-weight: 700;
            }}
            QPushButton:hover {{
                background: {_PALETTE['border']};
                color: #ffffff;
            }}
        """)
        self.btn_max.clicked.connect(self._toggle_maximize)

        btn_close = QPushButton("✕")
        btn_close.setFixedSize(28, 28)
        btn_close.setStyleSheet(f"""
            QPushButton {{
                background: {_PALETTE['bg_input']};
                color: {_PALETTE['text_muted']};
                border: 1px solid {_PALETTE['border']};
                border-radius: 6px;
                font-size: 12px;
                font-weight: 700;
            }}
            QPushButton:hover {{
                background: {_PALETTE['danger']};
                color: #ffffff;
                border-color: {_PALETTE['danger']};
            }}
        """)
        btn_close.clicked.connect(self.close)

        lay.addWidget(logo)
        lay.addWidget(badge)
        lay.addSpacing(14)
        lay.addWidget(btn_nav_workspace)
        lay.addWidget(btn_nav_profile)
        lay.addWidget(self._drag_lbl, stretch=1)
        lay.addWidget(self.btn_pin)
        lay.addWidget(btn_min)
        lay.addWidget(self.btn_max)
        lay.addWidget(btn_close)
        return bar

    def _toggle_pin(self) -> None:
        """Toggle Always-on-Top overlay state dynamically."""
        self._is_pinned = not self._is_pinned
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, self._is_pinned)
        if self._is_pinned:
            self.btn_pin.setText("📌 Pinned")
            self.btn_pin.setStyleSheet(f"""
                QPushButton {{
                    background: {_PALETTE['accent_glow']};
                    color: {_PALETTE['accent']};
                    border: 1px solid {_PALETTE['accent']};
                    border-radius: 6px;
                    padding: 4px 10px;
                    font-size: 11px;
                    font-weight: 700;
                }}
            """)
        else:
            self.btn_pin.setText("📌 Pin")
            self.btn_pin.setStyleSheet(f"""
                QPushButton {{
                    background: {_PALETTE['bg_input']};
                    color: {_PALETTE['text_muted']};
                    border: 1px solid {_PALETTE['border']};
                    border-radius: 6px;
                    padding: 4px 10px;
                    font-size: 11px;
                    font-weight: 600;
                }}
            """)
        self.show()

    def _toggle_maximize(self) -> None:
        """Toggle between maximized and normal window state."""
        if self.isMaximized():
            self.showNormal()
            self.btn_max.setText("◻")
        else:
            self.showMaximized()
            self.btn_max.setText("❐")

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
        self.preview_pane.profile_updated.connect(self.profile_updated.emit)


# ══════════════════════════════════════════════════════════════════════════════
# Public Entry-Point
# ══════════════════════════════════════════════════════════════════════════════

class RoleSyncApp:
    """
    Pure Python RoleSync SaaS Studio application wrapper consumed by main.py.
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

    @property
    def session_switched_signal(self):
        return self._window.chat_panel.session_switched

    @property
    def new_chat_requested_signal(self):
        return self._window.chat_panel.new_chat_requested

    @property
    def deadline_changed_signal(self):
        return self._window.chat_panel.deadline_changed

    @property
    def test_notification_requested_signal(self):
        return self._window.chat_panel.test_notification_requested

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def show(self) -> None:
        self._window.show()

    def run(self) -> int:
        self._window.show()
        return self._qt_app.exec()


if __name__ == "__main__":
    app = RoleSyncApp()
    app.chat.push_msg(
        "👋 Welcome to <b>RoleSync Studio</b>!<br>"
        "Highlight any job description in your browser and press "
        "<code>Ctrl + Shift + J</code> to begin tailoring your résumé.",
        role="agent",
    )
    sys.exit(app.run())
