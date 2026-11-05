# ⚡ RoleSync

> **100% Pure Python Autonomous Job Lifecycle Agent**  
> Native desktop AI agent operating via a global system hotkey (`Ctrl + Shift + J`). Bypasses browser ingestion, provides a glassmorphic PyQt6 desktop overlay for live preview and conversational edits, maintains its own state machine, and executes headless job applications via Playwright.

---

## 🏗️ Architecture

RoleSync is structured into 7 decoupled Python modules and a central orchestrator:

| Module | File | Role & Technology |
|---|---|---|
| **Orchestrator** | `main.py` | Thread-safe coordinator connecting GUI, hotkeys, RAG, LLM, Word compiler, and Playwright. |
| **Module 1** | `desktop_app.py` | PyQt6 transparent overlay widget with chat interface, markdown preview, and action buttons. |
| **Module 2** | `hotkey_ingestor.py` | Global `Ctrl + Shift + J` keyboard listener + 9-stage regex sanitiser and non-destructive clipboard restore. |
| **Module 3** | `rag_engine.py` | Local ChromaDB vector store + `all-MiniLM-L6-v2` embeddings for matching against `master_portfolio.json`. |
| **Module 4** | `llm_reasoner.py` | OpenAI API client with strict Pydantic v2 validation (`ReasonerOutput`), STAR bullet generator, and retry backoff. |
| **Module 5** | `doc_compiler.py` | ATS-compliant Word document generator (`python-docx`), self-bootstrapping template, and PDF exporter. |
| **Module 6** | `auto_submit.py` | Async Playwright headless Chromium engine with portal sniffers (Greenhouse, Lever, Workday, Ashby, Generic). |
| **Module 7** | `lifecycle_watchdog.py` | SQLite3 database (`vault/rolesync.db`), 48h deadline desktop notifications (`plyer`), and post-apply dossier vault. |

---

## ⚡ Quick Start

### 1. Prerequisites
- Python 3.10, 3.11, or 3.12 (Install from [python.org](https://www.python.org/downloads/))
- Windows 10/11 (Global hotkey & transparent overlay are native Windows-optimized)

### 2. Setup Virtual Environment & Install Dependencies
Open PowerShell or your terminal and run:

```powershell
# Clone the repository (if downloaded from GitHub)
git clone https://github.com/takshcore/rolesync.git
cd rolesync

# Create a virtual environment
python -m venv .venv

# Activate the virtual environment
# On Windows PowerShell:
.venv\Scripts\Activate.ps1
# On Windows Command Prompt:
.venv\Scripts\activate.bat
# On macOS/Linux:
source .venv/bin/activate

# Upgrade pip and install all requirements
pip install --upgrade pip
pip install -r requirements.txt

# Install Playwright browser engines
playwright install chromium
```

### 3. Configure Candidate Profile & API Keys
1. **Candidate Profile**: Edit `profile.json` with your real details:
   ```json
   {
     "name": "Your Name",
     "email": "your.email@example.com",
     "phone": "+1-555-019-2834",
     "linkedin": "https://linkedin.com/in/yourprofile",
     "github": "https://github.com/yourprofile",
     "location": "San Francisco, CA (Open to Remote)",
     "years_exp": 5
   }
   ```
2. **API Keys** (Optional for live LLM reasoning; mock fallback is active if omitted):
   - **Recommended: Google Gemini API** (Free tier available at [Google AI Studio](https://aistudio.google.com/)):
     ```powershell
     # Copy the example file or create your .env:
     Set-Content -Path .env -Value "GEMINI_API_KEY=AIzaSyYourGeminiApiKeyHere"
     ```
   - **Alternative: OpenAI API**:
     ```powershell
     Set-Content -Path .env -Value "OPENAI_API_KEY=sk-your-openai-key-here"
     ```

### 4. Run RoleSync
```powershell
python main.py
```

---

## 🧪 Step-by-Step Testing Guide

### Test 1: Full System End-to-End
1. Run `python main.py`.
2. The dark transparent overlay will appear on your desktop with:
   - Chat interface on the left.
   - Live Markdown / HTML résumé preview on the right.
3. Open any web browser or document containing a job posting.
4. **Highlight the job description text** and press **`Ctrl + Shift + J`**.
5. Observe:
   - The chat displays: `📋 Job description captured! (X words). Running RAG retrieval and LLM tailoring pipeline...`
   - ChromaDB extracts relevant projects from `master_portfolio.json`.
   - The reasoner tailors 5-7 STAR bullets and extracts ATS keywords.
   - The right preview pane updates with the live tailored document.
   - A Word document is compiled into `active_workspace/`.
6. Click **Auto-Apply** or type **`Approved`** in the chat:
   - Playwright launches headless Chromium.
   - The application is submitted and registered in `vault/rolesync.db`.
   - All assets (résumé, JD, chat log, and screenshot) are archived to `vault/{Company}_{Role}/`.

---

### Test 2: Unit Testing Individual Modules

Each module is fully decoupled and can be tested in isolation:

| Module to Test | Command | What it validates |
|---|---|---|
| **Syntax & Structure** | `python check_syntax.py` | Verifies all files compile and pass AST parsing. |
| **GUI Overlay** | `python desktop_app.py` | Tests PyQt6 window chrome, glassmorphism, bubbles, and preview. |
| **Hotkey & Sanitiser** | `python hotkey_ingestor.py` | Highlight any text and press `Ctrl+Shift+J` to test regex cleaning. |
| **RAG Vector Search** | `python rag_engine.py` | Tests ChromaDB embeddings and cosine similarity retrieval. |
| **LLM Reasoner** | `python llm_reasoner.py` | Validates Pydantic schema generation with sample input. |
| **Docx Compiler** | `python doc_compiler.py` | Builds `templates/base.docx` and generates a sample `.docx` in `active_workspace/`. |
| **Playwright Submitter** | `python auto_submit.py` | Navigates headless Chromium and captures a verification screenshot. |
| **State Machine & Watchdog** | `python lifecycle_watchdog.py status` | Queries SQLite3 table and prints formatted ASCII jobs table. |

---

## 🚀 Publishing to GitHub & Sharing

### Option A: Using Git CLI

1. **Initialize Git repository**:
   ```powershell
   cd f:\agent
   git init
   git add .
   git commit -m "feat: initial commit of RoleSync autonomous agent"
   ```

2. **Create a new repository on GitHub**:
   - Go to [github.com/new](https://github.com/new).
   - Name it `rolesync`.
   - Choose **Public** or **Private**.
   - Do **NOT** initialize with README or .gitignore (we already built them).
   - Click **Create repository**.

3. **Link and Push**:
   ```powershell
   git branch -M main
   git remote add origin https://github.com/takshcore/rolesync.git
   git push -u origin main
   ```

---

### Option B: Using GitHub Desktop

1. Download and open [GitHub Desktop](https://desktop.github.com/).
2. Click **File** > **Add Local Repository...**.
3. Select `F:\agent` and click **Add Repository** (if prompted to create a repository, click **Create Repository**).
4. Enter commit summary: `Initial commit of RoleSync`.
5. Click **Commit to main**.
6. Click **Publish repository** to push it directly to your GitHub account.

---

### How Others Can Use It via Your GitHub Link

Once published, anyone can install and run RoleSync with one block of commands:

```bash
# 1. Clone your repository
git clone https://github.com/takshcore/rolesync.git
cd rolesync

# 2. Set up virtual environment
python -m venv .venv
source .venv/bin/activate    # or .venv\Scripts\Activate.ps1 on Windows

# 3. Install dependencies
pip install -r requirements.txt
playwright install chromium

# 4. Launch
python main.py
```

---

## 📁 Repository Directory Structure

```
rolesync/
├── .github/
│   └── workflows/
│       └── ci.yml               # Automated CI test suite
├── active_workspace/            # Generated Word documents & screenshots
├── templates/                   # ATS Word templates
├── vault/                       # SQLite database & archived job dossiers
├── logs/                        # Log files
├── auto_submit.py               # Module 6: Playwright Headless Submitter
├── check_syntax.py              # Self-test validation script
├── desktop_app.py               # Module 1: PyQt6 Desktop Overlay
├── doc_compiler.py              # Module 5: python-docx ATS Formatter
├── hotkey_ingestor.py           # Module 2: Global Hotkey Listener
├── lifecycle_watchdog.py        # Module 7: SQLite3 State Machine & Archiver
├── llm_reasoner.py              # Module 4: Pydantic & OpenAI Reasoner
├── main.py                      # Master Orchestrator
├── master_portfolio.json        # Seed projects for RAG retrieval
├── profile.json                 # Candidate profile settings
├── rag_engine.py                # Module 3: ChromaDB Vector Engine
├── requirements.txt             # Pinned package requirements
├── .gitignore                   # Ignore caches, DBs, and virtualenvs
└── README.md                    # Project documentation
```
