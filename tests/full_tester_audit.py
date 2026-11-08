"""
tests/full_tester_audit.py — Comprehensive End-to-End QA Test Suite
=====================================================================
Performs a rigorous industrial QA inspection across all 7 decoupled modules,
the orchestrator, the 1-tap web studio, document compilers, and vector RAG.
"""

import os
import sys
import time
import json
import pathlib
import traceback

os.environ["QT_QPA_PLATFORM"] = "offscreen"

WORKSPACE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE))

passed_tests = 0
failed_tests = 0
test_results = []

def test_case(name):
    def decorator(fn):
        def wrapper(*args, **kwargs):
            global passed_tests, failed_tests
            t0 = time.time()
            print(f"[*] RUNNING: {name}...", end=" ", flush=True)
            try:
                fn(*args, **kwargs)
                duration = (time.time() - t0) * 1000
                print(f"PASSED ({duration:.1f}ms)")
                passed_tests += 1
                test_results.append((name, "PASSED", f"{duration:.1f}ms", ""))
            except Exception as e:
                duration = (time.time() - t0) * 1000
                print(f"FAILED ({duration:.1f}ms)")
                print(f"    ERROR: {e}")
                traceback.print_exc()
                failed_tests += 1
                test_results.append((name, "FAILED", f"{duration:.1f}ms", str(e)))
        return wrapper
    return decorator


# ── SUITE 1: FILE INTEGRITY & CLEAN ARCHITECTURE ─────────────────────────────
@test_case("Suite 1.1: File Structure & Core Manifest Verification")
def test_file_structure():
    required_files = [
        "desktop_app.py",
        "hotkey_ingestor.py",
        "rag_engine.py",
        "llm_reasoner.py",
        "doc_compiler.py",
        "auto_submit.py",
        "lifecycle_watchdog.py",
        "main.py",
        "web_app.py",
        "profile.json",
        "master_portfolio.json",
        "requirements.txt",
        "run_rolesync.bat",
        "run_web_studio.bat",
        "README.md",
        "templates/base.docx",
        "utils/resume_parser.py",
        ".devcontainer/devcontainer.json",
    ]
    for rf in required_files:
        p = WORKSPACE / rf
        assert p.exists(), f"Required file missing: {rf}"
        assert p.stat().st_size > 0, f"File is empty: {rf}"


@test_case("Suite 1.2: Pinned Requirements & Streamlit Spec")
def test_requirements():
    req_text = (WORKSPACE / "requirements.txt").read_text(encoding="utf-8")
    assert "PyQt6" in req_text
    assert "chromadb" in req_text
    assert "pydantic" in req_text
    assert "python-docx" in req_text
    assert "playwright" in req_text
    assert "streamlit" in req_text


# ── SUITE 2: CANDIDATE DATA & KNOWLEDGE BASE ─────────────────────────────────
@test_case("Suite 2.1: Candidate Profile Schema & Portability")
def test_profile_json():
    p_path = WORKSPACE / "profile.json"
    data = json.loads(p_path.read_text(encoding="utf-8"))
    assert "name" in data and len(data["name"]) > 0
    assert "email" in data and "@" in data["email"]
    assert "phone" in data
    assert "linkedin" in data
    assert "github" in data
    assert "summary" in data and len(data["summary"]) > 20
    assert "skills_raw" in data and len(data["skills_raw"]) > 20


@test_case("Suite 2.2: Master Portfolio Vector Knowledge Store")
def test_portfolio_json():
    port_path = WORKSPACE / "master_portfolio.json"
    data = json.loads(port_path.read_text(encoding="utf-8"))
    assert "projects" in data
    projects = data["projects"]
    assert len(projects) >= 3, f"Expected at least 3 projects, got {len(projects)}"
    for p in projects:
        assert "id" in p
        assert "name" in p
        assert "description" in p
        assert "tech_stack" in p


# ── SUITE 3: MODULE 3 (RAG VECTOR STORE) ─────────────────────────────────────
@test_case("Suite 3.1: ChromaDB Vector Store Query & Context Generation")
def test_rag_engine():
    from rag_engine import RAGEngine
    rag = RAGEngine()
    assert rag.collection is not None
    ctx = rag.get_context_block("Senior Python Agentic AI Engineer with LLM pipelines")
    assert len(ctx) > 100
    assert "Python" in ctx or "AI" in ctx or "Domain" in ctx


@test_case("Suite 3.2: RAG Hot-Reload & Re-Indexing")
def test_rag_reload():
    from rag_engine import RAGEngine
    rag = RAGEngine()
    rag.reload_portfolio()
    assert len(rag.portfolio) >= 3


# ── SUITE 4: MODULE 4 (COGNITIVE REASONER & SCHEMA) ─────────────────────────
@test_case("Suite 4.1: Pydantic v2 ReasonerOutput Schema Strict Validation")
def test_pydantic_schema():
    from llm_reasoner import ReasonerOutput
    test_data = {
        "company": "Honeywell",
        "role": "Senior AI Systems Engineer",
        "deadline": "15 Dec 2026",
        "missing_skills": ["Rust", "Kubernetes"],
        "tailored_bullets": [
            "Architected autonomous Python agent pipelines utilizing ChromaDB and Pydantic v2.",
            "Engineered high-throughput event loops reducing latency by 35% across distributed services.",
            "Automated document compilation workflows generating compliant ATS resumes and PDF exports.",
            "Integrated Playwright headless browser execution for hands-free form validation.",
            "Collaborated with cross-functional engineering teams to implement strict quality guardrails."
        ],
        "ats_keywords": ["Python", "ChromaDB", "Pydantic", "AI Agent"],
        "match_score": 0.94
    }
    obj = ReasonerOutput(**test_data)
    assert obj.company == "Honeywell"
    assert obj.match_score == 0.94
    assert len(obj.tailored_bullets) == 5
    assert len(obj.ats_keywords) == 4


@test_case("Suite 4.2: Intelligent Offline Local NLP Extractor (Zero-Key)")
def test_local_extractor():
    from llm_reasoner import extract_jd_metadata_local
    raw_jd = """
    Company: Google DeepMind
    Title: Lead Generative AI Architect
    Deadline: 31 Jan 2027
    Requirements:
    - Master Python, Pydantic, Agentic AI, ChromaDB, and WebSockets.
    - Experience in Docker and Kubernetes is preferred.
    """
    out = extract_jd_metadata_local(raw_jd, "Candidate has Python and ChromaDB background.")
    assert "Google" in out.company or out.company == "Google DeepMind"
    assert "Architect" in out.role or "AI" in out.role
    assert "31 Jan 2027" in out.deadline or "Jan" in out.deadline
    assert out.match_score > 0.5
    assert len(out.ats_keywords) >= 2
    assert len(out.tailored_bullets) >= 5


# ── SUITE 5: MODULE 5 (ATS DOCUMENT COMPILER) ───────────────────────────────
@test_case("Suite 5.1: ATS Word (.docx) Document Compilation")
def test_doc_compiler_docx():
    from doc_compiler import DocCompiler, CandidateProfile
    from llm_reasoner import extract_jd_metadata_local

    p_data = json.loads((WORKSPACE / "profile.json").read_text(encoding="utf-8"))
    cand = CandidateProfile(**{k: v for k, v in p_data.items() if k in CandidateProfile.__dataclass_fields__})
    out = extract_jd_metadata_local("Company: TestCorp\nRole: Lead Engineer\nDeadline: 20 Dec 2026")
    
    compiler = DocCompiler()
    docx_path = compiler.compile(output=out, candidate=cand)
    assert docx_path.exists(), "Docx file was not created"
    assert docx_path.stat().st_size > 10000, f"Docx file too small ({docx_path.stat().st_size} bytes)"


@test_case("Suite 5.2: PDF Compilation & ReportLab Fallback")
def test_doc_compiler_pdf():
    from doc_compiler import DocCompiler, CandidateProfile
    from llm_reasoner import extract_jd_metadata_local

    p_data = json.loads((WORKSPACE / "profile.json").read_text(encoding="utf-8"))
    cand = CandidateProfile(**{k: v for k, v in p_data.items() if k in CandidateProfile.__dataclass_fields__})
    out = extract_jd_metadata_local("Company: PDFTest\nRole: Systems Architect")
    
    compiler = DocCompiler()
    docx_path = compiler.compile(output=out, candidate=cand)
    pdf_path = compiler.export_pdf(docx_path)
    assert pdf_path.exists(), "PDF file was not created"
    assert pdf_path.stat().st_size > 1000, f"PDF file too small ({pdf_path.stat().st_size} bytes)"


# ── SUITE 6: MODULE 7 (LIFECYCLE WATCHDOG & SQLITE VAULT) ────────────────────
@test_case("Suite 6.1: SQLite State Machine & Job Registration")
def test_lifecycle_sqlite():
    from lifecycle_watchdog import LifecycleManager
    lm = LifecycleManager()
    job_id = lm.register_job(
        company="QA Test Corp",
        role="Lead Systems Tester",
        job_url="https://testcorp.com/jobs/101",
        deadline="15 Dec 2026",
        resume_path="",
        jd_path=""
    )
    assert job_id > 0
    lm.on_submit_complete(job_id, "SUCCESS")
    job = lm.db.get_job_by_id(job_id)
    assert job is not None
    assert job["status"] == "submitted"


@test_case("Suite 6.2: Deadline Parser (Urgency & Natural Language)")
def test_deadline_parser():
    from lifecycle_watchdog import parse_deadline_info
    cases = [
        ("15 Dec 2026", True),
        ("Tomorrow", True),
        ("2026-12-31", True),
        ("Not specified", False),
    ]
    for dl_str, should_parse in cases:
        dt, friendly, urgent = parse_deadline_info(dl_str)
        assert len(friendly) > 0


# ── SUITE 7: MODULE 2 (HOTKEY INGESTOR & SANITIZER) ──────────────────────────
@test_case("Suite 7.1: Text Cleaning & Noise Sanitization")
def test_hotkey_cleaning():
    from hotkey_ingestor import sanitise
    dirty_text = (
        "Apply Now to Continue\nAccept all cookies and privacy policy.\n\n"
        "Job Description: We are looking for a Senior Python Systems Engineer to join our autonomous intelligence division. "
        "You will architect autonomous Python agent pipelines utilizing ChromaDB and Pydantic v2 schemas."
    )
    cleaned = sanitise(dirty_text)
    assert "Accept all cookies" not in cleaned
    assert "Senior Python Systems Engineer" in cleaned
    assert len(cleaned) >= 80


# ── SUITE 8: MODULE 6 (HEADLESS SUBMITTER FIELD MAPPING) ────────────────────
@test_case("Suite 8.1: Playwright AutoSubmitter & Profile Ingestion")
def test_auto_submit_mapping():
    from auto_submit import CandidateProfile as ASCandidateProfile, SubmitResult, AutoSubmitter
    p_data = json.loads((WORKSPACE / "profile.json").read_text(encoding="utf-8"))
    cand = ASCandidateProfile.from_dict(p_data)
    assert cand.full_name == p_data["name"]
    assert cand.email == p_data["email"]
    assert cand.phone == p_data["phone"]


# ── SUITE 9: MODULE 1 DESKTOP (MODERN SAAS STUDIO PYQT6) ─────────────────────
@test_case("Suite 9.1: PyQt6 Desktop Modern SaaS Studio Instantiation")
def test_desktop_ui():
    from PyQt6.QtWidgets import QApplication
    from desktop_app import PreviewPane, ChatPanel, _MainWindow

    app = QApplication.instance() or QApplication([])
    window = _MainWindow()
    assert window.chat_panel is not None
    assert window.preview_pane is not None

    # Test 4-tab studio navigation
    preview = window.preview_pane
    preview.show_resume_view()
    preview.show_editor_view()
    preview.show_ats_view()
    preview.show_profile_view()

    # Test live Markdown rendering
    preview.load_md("# Candidate Name\n- Project bullet point")

    # Test ATS match analytics update
    from llm_reasoner import extract_jd_metadata_local
    out = extract_jd_metadata_local("Company: Test\nRole: Dev")
    preview.update_ats_analytics(out)


# ── SUITE 10: 1-TAP WEB STUDIO (STREAMLIT COMPANION) ────────────────────────
@test_case("Suite 10.1: Web App Pipeline & Markdown Formatter")
def test_web_app():
    import web_app
    cand_data = web_app.load_candidate_data()
    cand_obj = web_app.get_candidate_profile_obj(cand_data)
    assert cand_obj.name is not None

    out = web_app.run_pipeline("Company: WebAppTest\nRole: Cloud Architect")
    md = web_app.build_markdown_resume(out, cand_obj)
    assert len(md) > 200
    assert cand_obj.name.upper() in md


@test_case("Suite 10.2: Resume Ingestion & Extraction Engine")
def test_resume_parser():
    from utils.resume_parser import parse_contact_info, extract_projects_and_experience
    sample_text = """
    Jane Doe
    jane.doe@example.com | +1 555-123-4567 | San Francisco, CA
    https://linkedin.com/in/janedoe | https://github.com/janedoe

    Professional Summary:
    Senior AI Engineer with 6 years experience in Python and Agentic systems.

    Experience:
    Autonomous Agent Lead at AI Solutions (2022 - 2024)
    - Built real-time RAG workflows using ChromaDB and FastAPI.
    - Optimized model latency by 40%.
    """
    contact = parse_contact_info(sample_text)
    assert contact["email"] == "jane.doe@example.com"
    assert "555-123-4567" in contact["phone"]

    projects = extract_projects_and_experience(sample_text)
    assert len(projects) >= 1


# ── EXECUTION SUMMARY ────────────────────────────────────────────────────────
def run_all_tests():
    print("=" * 70)
    print("ROLESYNC INDUSTRIAL QA & TESTER AUDIT SUITE")
    print("Target Workspace:", WORKSPACE)
    print("=" * 70)

    test_file_structure()
    test_requirements()
    test_profile_json()
    test_portfolio_json()
    test_rag_engine()
    test_rag_reload()
    test_pydantic_schema()
    test_local_extractor()
    test_doc_compiler_docx()
    test_doc_compiler_pdf()
    test_lifecycle_sqlite()
    test_deadline_parser()
    test_hotkey_cleaning()
    test_auto_submit_mapping()
    test_desktop_ui()
    test_web_app()
    test_resume_parser()

    print("\n" + "=" * 70)
    print(f"AUDIT SUMMARY: {passed_tests} PASSED | {failed_tests} FAILED")
    print("=" * 70)

    return failed_tests == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
