# ⚡ RoleSync Studio

> **100% Pure Python Autonomous Job Lifecycle Agent**  
> Native desktop AI agent operating via a global system hotkey (`Ctrl + Shift + J`) alongside an instant 1-tap Cloud Web Companion. Transforms raw Job Descriptions into ATS-optimized tailored résumés, generates real-time keyword match gauges, manages job applications in an SQLite state machine, and executes autonomous headless browser submissions via Playwright.

[![Open in Streamlit](https://static.streamlit.io/badges/streamlit_badge_black_white.svg)](https://share.streamlit.io)
[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

---

## 🌟 Instant Evaluation Options (For Instructors & Reviewers)

| Evaluation Method | Setup Required | Platforms Supported | Best For |
|---|---|---|---|
| **1. Live Web Companion (`web_app.py`)** | **0 seconds (1-Tap Link)** | Any Phone, Tablet, Mac, Windows, Linux | Instant presentation, zero installation, testing anywhere |
| **2. GitHub Codespaces** | **1-Tap Cloud Container** | Any Browser | Cloud evaluation inside a full Linux VM |
| **3. Windows 1-Click Launcher (`run_rolesync.bat`)** | **1-Click (.bat)** | Windows 10 / 11 | Full desktop OS overlay with global hotkeys & WinRT notifications |

---

### Option 1: 1-Tap Permanent Web Link (Streamlit Cloud — 100% Free)

If your instructor wants a **direct web link that opens with one tap on their phone or laptop with NO setup, NO downloads, and NO terminal**:

1. **Push this repository to your GitHub account** (e.g. `https://github.com/takshcore/rolesync`).
2. Go to **[share.streamlit.io](https://share.streamlit.io)** (log in with your GitHub account).
3. Click **"New app"**, select your repository, set the Main file path to **`web_app.py`**, and click **"Deploy"**!
4. Streamlit Cloud will automatically build and give you a permanent public URL:
   ```
   https://rolesync.streamlit.app
   ```
5. **Send that URL to your instructor.** They tap it once, and the full interactive RoleSync Studio opens instantly in their browser!

---

### Option 2: 1-Tap GitHub Codespaces

Click the button below to launch RoleSync in an isolated, pre-configured cloud container directly inside your browser:

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new)

Once loaded, Codespaces automatically forwards port `8501` and opens the RoleSync Web Studio in your browser.

---

### Option 3: 1-Click Desktop Launcher (Windows)

1. Clone or download the repository:
   ```bash
   git clone https://github.com/takshcore/rolesync.git
   cd rolesync
   ```
2. Double-click **`run_rolesync.bat`** (or **`run_web_studio.bat`**).
   - Automatically sets up the virtual environment, verifies dependencies, and launches RoleSync without typing any commands!

---

## 🏗️ 7-Module Decoupled Architecture

RoleSync is strictly structured into **7 core decoupled Python modules** coordinated by a master orchestrator:

```
rolesync/
├── desktop_app.py          ── Module 1: The GUI (Modern SaaS Studio in PyQt6)
├── hotkey_ingestor.py      ── Module 2: The Scraper (Global Ctrl+Shift+J Ingestion)
├── rag_engine.py           ── Module 3: The Truth Source (ChromaDB Vector Retrieval)
├── llm_reasoner.py         ── Module 4: The Brain (Pydantic v2 Schema + STAR Tailoring)
├── doc_compiler.py         ── Module 5: The Formatter (python-docx + PDF Exporter)
├── auto_submit.py          ── Module 6: The Executor (Playwright Headless Automation)
├── lifecycle_watchdog.py   ── Module 7: The State Machine (SQLite + WinRT Alerts + Vault)
├── main.py                 ── Master Orchestrator (Desktop GUI + Hotkey Engine)
└── web_app.py              ── 1-Tap Cloud Web Companion (Streamlit Cloud)
```

| Module | File | Role & Technology |
|---|---|---|
| **Module 1 (Desktop)** | [desktop_app.py](desktop_app.py) | Modern SaaS Studio (PyQt6) with 4-tab studio, inline zero-freeze deadline editor, and dark theme. |
| **Module 1 (Web)** | [web_app.py](web_app.py) | 1-Tap Cloud Studio (Streamlit) for browser-based evaluation on mobile/desktop. |
| **Module 2** | [hotkey_ingestor.py](hotkey_ingestor.py) | Global `Ctrl + Shift + J` listener + 9-stage regex sanitiser and non-destructive clipboard restore. |
| **Module 3** | [rag_engine.py](rag_engine.py) | Local ChromaDB vector store + `all-MiniLM-L6-v2` embeddings for matching against `master_portfolio.json`. |
| **Module 4** | [llm_reasoner.py](llm_reasoner.py) | Cognitive client with strict Pydantic v2 validation (`ReasonerOutput`), STAR bullet generator, and skill-gap analysis. |
| **Module 5** | [doc_compiler.py](doc_compiler.py) | ATS-compliant Word document generator (`python-docx`), reference-mode styling, and PDF exporter. |
| **Module 6** | [auto_submit.py](auto_submit.py) | Async Playwright headless Chromium engine with intelligent form field mapping and résumé uploader. |
| **Module 7** | [lifecycle_watchdog.py](lifecycle_watchdog.py) | SQLite3 state machine (`vault/rolesync.db`), Windows WinRT toast alerts, daily reminders, and vault archiver. |
| **Orchestrator** | [main.py](main.py) | Coordinates GUI, background workers, hotkeys, RAG, LLM, and multi-session chats. |

---

## 🖥️ Feature Walkthrough

### 1. Global System Ingestion (`Ctrl + Shift + J`)
* Open any browser or job board (LinkedIn, Indeed, Honeywell, etc.).
* Highlight the job description text and press **`Ctrl + Shift + J`**.
* RoleSync captures the text, sanitizes noise, extracts company and role details, and tailors the résumé.

### 2. Modern 4-Tab Document Studio
* **`📄 ATS Résumé`**: High-contrast, paper-styled document formatted for ATS compatibility.
* **`✏️ Edit Markdown`**: Direct in-app editor to modify bullets, summaries, or skills with instant re-compilation.
* **`🎯 ATS Analytics & Match Radar`**:
  * Real-time Match Score Gauge (e.g. `94% ATS Match`).
  * **Matched ATS Keywords Cloud**: Extracted keywords displayed in emerald green pills.
  * **Skill-Gap Analysis**: Missing skills displayed in amber pills.
  * **Tailored STAR Bullets**: Achievement bullets generated specifically for the job description.
* **`👤 Truth Source`**:
  * Drag-and-drop or click to upload your own résumé file (`.docx`, `.pdf`, `.txt`).
  * Live candidate profile and portfolio editor that hot-reloads across all active sessions with **zero app relaunch**.

### 3. Native Device Alerts & Deadlines
* **Inline Deadline Editor**: Click `[✏️ Set / Edit]` on the deadline bar to enter deadlines with zero modal popups or freezing.
* **Native Windows WinRT Toasts**: Automated daily reminders and 48-hour alerts delivered directly to the Windows Notification Center.

### 4. Autonomous Submission (`Auto-Apply`)
* Click **`⚡ Auto-Apply`** or type **`Approved`** in the copilot chat.
* Playwright opens headless Chromium, maps profile fields, uploads the compiled résumé, submits the form, and archives the dossier in `vault/` for interview prep.

---

## 📁 Repository Directory Structure

```
rolesync/
├── .devcontainer/               # 1-Tap GitHub Codespaces configuration
├── active_workspace/            # Generated Word documents & PDF exports
├── templates/                   # ATS Word base templates
├── vault/                       # SQLite database (rolesync.db) & archived dossiers
├── logs/                        # Rotating log files
├── utils/                       # Auxiliary extraction scripts
├── auto_submit.py               # Module 6: Playwright Headless Submitter
├── desktop_app.py               # Module 1: PyQt6 Modern SaaS Studio
├── doc_compiler.py              # Module 5: python-docx ATS Formatter
├── hotkey_ingestor.py           # Module 2: Global Hotkey Listener
├── lifecycle_watchdog.py        # Module 7: SQLite3 State Machine & WinRT Alerts
├── llm_reasoner.py              # Module 4: Pydantic v2 Reasoner
├── main.py                      # Master Orchestrator
├── master_portfolio.json        # ChromaDB truth-source project portfolio
├── profile.json                 # Candidate profile data
├── rag_engine.py                # Module 3: ChromaDB Vector Engine
├── requirements.txt             # Pinned package requirements
├── run_rolesync.bat             # 1-Click Windows Batch Launcher (PyQt6 Desktop)
├── run_web_studio.bat           # 1-Click Windows Batch Launcher (Streamlit Web)
├── web_app.py                   # 1-Tap Cloud Web Companion
├── .gitignore                   # Ignore caches, virtualenvs, and runtime databases
└── README.md                    # Project documentation
```

---

## 📄 License
MIT License. Built with pure Python & PyQt6.
