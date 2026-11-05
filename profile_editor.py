"""
profile_editor.py — In-App Profile & Portfolio Knowledge Base Editor
=====================================================================
Provides a modern PyQt6 dialog allowing candidates to edit their personal
details and portfolio projects directly inside RoleSync without touching JSON.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

_PALETTE = {
    "bg_main": "#0d0f14",
    "bg_panel": "#12151c",
    "bg_input": "#1a1e28",
    "accent": "#4f8ef7",
    "accent_dark": "#2d5fc4",
    "text_primary": "#e8ecf4",
    "text_muted": "#7a8299",
    "border": "#2a2f3f",
    "success": "#2ecc71",
    "danger": "#e74c3c",
}

_DIALOG_STYLE = f"""
QDialog {{
    background: {_PALETTE['bg_main']};
    color: {_PALETTE['text_primary']};
    font-family: 'Segoe UI', sans-serif;
}}
QTabWidget::pane {{
    border: 1px solid {_PALETTE['border']};
    background: {_PALETTE['bg_panel']};
    border-radius: 8px;
}}
QTabBar::tab {{
    background: {_PALETTE['bg_input']};
    color: {_PALETTE['text_muted']};
    padding: 8px 16px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    margin-right: 4px;
}}
QTabBar::tab:selected {{
    background: {_PALETTE['bg_panel']};
    color: {_PALETTE['accent']};
    font-weight: 700;
}}
QLineEdit, QTextEdit, QSpinBox {{
    background: {_PALETTE['bg_input']};
    color: {_PALETTE['text_primary']};
    border: 1px solid {_PALETTE['border']};
    border-radius: 6px;
    padding: 6px 10px;
}}
QLineEdit:focus, QTextEdit:focus, QSpinBox:focus {{
    border-color: {_PALETTE['accent']};
}}
QLabel {{
    color: {_PALETTE['text_primary']};
    font-size: 12px;
}}
QPushButton {{
    background: {_PALETTE['bg_input']};
    color: {_PALETTE['text_primary']};
    border: 1px solid {_PALETTE['border']};
    border-radius: 6px;
    padding: 8px 16px;
    font-weight: 600;
}}
QPushButton:hover {{
    border-color: {_PALETTE['accent']};
    color: {_PALETTE['accent']};
}}
QPushButton#btnSave {{
    background: {_PALETTE['success']};
    color: #fff;
    border: none;
}}
QPushButton#btnSave:hover {{
    background: #27ae60;
}}
"""


class ProfileEditorDialog(QDialog):
    """Dialog for editing Candidate Profile (profile.json) and Portfolio (master_portfolio.json)."""

    data_saved = pyqtSignal()

    def __init__(
        self,
        profile_path: str = "f:/agent/profile.json",
        portfolio_path: str = "f:/agent/master_portfolio.json",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.profile_path = pathlib.Path(profile_path)
        self.portfolio_path = pathlib.Path(portfolio_path)

        self.setWindowTitle("RoleSync — Candidate Profile & Portfolio Manager")
        self.resize(750, 580)
        self.setStyleSheet(_DIALOG_STYLE)

        self._setup_ui()
        self._load_data()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # Title
        title = QLabel("⚙️ My Profile & Knowledge Base")
        title.setStyleSheet("font-size: 16px; font-weight: 700; color: #4f8ef7;")
        subtitle = QLabel("Your real details will be used for ATS tailoring and form submission.")
        subtitle.setStyleSheet(f"color: {_PALETTE['text_muted']}; font-size: 11px;")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        # Tabs
        self.tabs = QTabWidget()

        # Tab 1: Personal Profile
        self.tab_profile = QWidget()
        self._setup_profile_tab()
        self.tabs.addTab(self.tab_profile, "👤 Personal & Contact Info")

        # Tab 2: Projects & Experience
        self.tab_portfolio = QWidget()
        self._setup_portfolio_tab()
        self.tabs.addTab(self.tab_portfolio, "💼 Portfolio & Real Projects")

        layout.addWidget(self.tabs, stretch=1)

        # Action Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.reject)

        self.btn_save = QPushButton("💾 Save & Apply")
        self.btn_save.setObjectName("btnSave")
        self.btn_save.clicked.connect(self._save_data)

        btn_layout.addWidget(self.btn_cancel)
        btn_layout.addWidget(self.btn_save)
        layout.addLayout(btn_layout)

    def _setup_profile_tab(self) -> None:
        form = QFormLayout(self.tab_profile)
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(10)

        self.in_name = QLineEdit()
        self.in_email = QLineEdit()
        self.in_phone = QLineEdit()
        self.in_linkedin = QLineEdit()
        self.in_github = QLineEdit()
        self.in_location = QLineEdit()

        self.in_exp = QSpinBox()
        self.in_exp.setRange(0, 40)
        self.in_exp.setSuffix(" years")

        form.addRow("Full Name:", self.in_name)
        form.addRow("Email Address:", self.in_email)
        form.addRow("Phone Number:", self.in_phone)
        form.addRow("LinkedIn URL:", self.in_linkedin)
        form.addRow("GitHub URL:", self.in_github)
        form.addRow("Location (City, Country):", self.in_location)
        form.addRow("Experience / Degree:", self.in_exp)

    def _setup_portfolio_tab(self) -> None:
        vbox = QVBoxLayout(self.tab_portfolio)
        vbox.setContentsMargins(16, 16, 16, 16)
        vbox.setSpacing(8)

        lbl_desc = QLabel(
            "Add your real projects, tools, and quantified achievements (JSON format).\n"
            "ChromaDB uses this to select relevant experience matching the job description:"
        )
        lbl_desc.setStyleSheet(f"color: {_PALETTE['text_muted']}; font-size: 11px;")
        vbox.addWidget(lbl_desc)

        self.txt_portfolio = QTextEdit()
        self.txt_portfolio.setPlaceholderText("Paste or edit your master portfolio JSON...")
        vbox.addWidget(self.txt_portfolio, stretch=1)

    def _load_data(self) -> None:
        """Load from profile.json and master_portfolio.json."""
        # Load profile
        if self.profile_path.exists():
            try:
                data = json.loads(self.profile_path.read_text(encoding="utf-8"))
                self.in_name.setText(data.get("name", ""))
                self.in_email.setText(data.get("email", ""))
                self.in_phone.setText(data.get("phone", ""))
                self.in_linkedin.setText(data.get("linkedin", ""))
                self.in_github.setText(data.get("github", ""))
                self.in_location.setText(data.get("location", ""))
                self.in_exp.setValue(data.get("years_exp", 0))
            except Exception as e:
                print(f"Error loading profile: {e}")

        # Load portfolio
        if self.portfolio_path.exists():
            try:
                content = self.portfolio_path.read_text(encoding="utf-8")
                self.txt_portfolio.setPlainText(content)
            except Exception as e:
                print(f"Error loading portfolio: {e}")

    def _save_data(self) -> None:
        """Validate and write back to files."""
        # Validate profile
        profile_data = {
            "name": self.in_name.text().strip() or "Candidate",
            "email": self.in_email.text().strip(),
            "phone": self.in_phone.text().strip(),
            "linkedin": self.in_linkedin.text().strip(),
            "github": self.in_github.text().strip(),
            "location": self.in_location.text().strip(),
            "years_exp": self.in_exp.value(),
        }

        # Validate portfolio JSON
        portfolio_text = self.txt_portfolio.toPlainText().strip()
        try:
            parsed_portfolio = json.loads(portfolio_text)
            if not isinstance(parsed_portfolio, list):
                raise ValueError("Portfolio must be a JSON array of projects.")
        except Exception as err:
            QMessageBox.warning(
                self,
                "Invalid JSON",
                f"Please fix the portfolio JSON format:\n{err}",
            )
            return

        try:
            # Save files
            self.profile_path.write_text(json.dumps(profile_data, indent=2), encoding="utf-8")
            self.portfolio_path.write_text(json.dumps(parsed_portfolio, indent=2), encoding="utf-8")
            self.data_saved.emit()
            QMessageBox.information(
                self,
                "Saved Successfully",
                "Your profile and portfolio have been updated!\nFuture résumé generation will use your updated experience.",
            )
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "Save Error", f"Failed saving data: {e}")
