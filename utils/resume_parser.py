"""
RoleSync — Master Resume Ingestion & Parsing Engine
===================================================
Enables candidates to feed their existing original resume (.pdf, .docx, .txt)
into RoleSync. The parser extracts ground-truth candidate details, past work
experiences, technical projects, skills, and education, and converts them into
the structured formats required by profile.json, master_portfolio.json, and
the ChromaDB vector retrieval engine.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import shutil
from typing import Any, Dict, List, Optional

logger = logging.getLogger("RoleSync.ResumeParser")


# ── Text Extraction Helpers ───────────────────────────────────────────────────

def extract_text_from_pdf(file_path: pathlib.Path) -> str:
    """Extract plain text from a PDF file using pypdf."""
    import pypdf
    text_chunks: List[str] = []
    try:
        reader = pypdf.PdfReader(str(file_path))
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text_chunks.append(page_text)
    except Exception as e:
        logger.error("Error reading PDF %s: %s", file_path, e)
        raise ValueError(f"Could not parse PDF file: {e}")
    return "\n\n".join(text_chunks).strip()


def extract_text_from_docx(file_path: pathlib.Path) -> str:
    """Extract plain text from a Word document (.docx)."""
    import docx
    text_chunks: List[str] = []
    try:
        doc = docx.Document(str(file_path))
        for p in doc.paragraphs:
            if p.text.strip():
                text_chunks.append(p.text.strip())
        for table in doc.tables:
            for row in table.rows:
                row_text = " | ".join(c.text.strip() for c in row.cells if c.text.strip())
                if row_text:
                    text_chunks.append(row_text)
    except Exception as e:
        logger.error("Error reading DOCX %s: %s", file_path, e)
        raise ValueError(f"Could not parse DOCX file: {e}")
    return "\n\n".join(text_chunks).strip()


def extract_text_from_file(file_path: str | pathlib.Path) -> str:
    """Universal text extractor supporting .pdf, .docx, and .txt files."""
    p = pathlib.Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"Resume file not found at: {file_path}")

    ext = p.suffix.lower()
    if ext == ".pdf":
        return extract_text_from_pdf(p)
    elif ext in (".docx", ".doc"):
        return extract_text_from_docx(p)
    elif ext in (".txt", ".md", ".rtf"):
        try:
            return p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return p.read_text(encoding="latin-1")
    else:
        raise ValueError(f"Unsupported file format: {ext}. Please use .pdf, .docx, or .txt")


# ── Section & Heuristic Extraction ────────────────────────────────────────────

def parse_contact_info(text: str) -> Dict[str, Any]:
    """Extract email, phone, linkedin, github, location, and candidate name using regex heuristics."""
    info: Dict[str, Any] = {
        "name": "",
        "email": "",
        "phone": "",
        "linkedin": "",
        "github": "",
        "location": "",
        "years_exp": 0,
    }

    # 1. Email
    email_m = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", text)
    if email_m:
        info["email"] = email_m.group(0).strip()

    # 2. LinkedIn
    linkedin_m = re.search(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[\w\-]+", text, re.IGNORECASE)
    if linkedin_m:
        val = linkedin_m.group(0).strip()
        if not val.startswith("http"):
            val = "https://" + val
        info["linkedin"] = val

    # 3. GitHub
    github_m = re.search(r"(?:https?://)?(?:www\.)?github\.com/[\w\-]+", text, re.IGNORECASE)
    if github_m:
        val = github_m.group(0).strip()
        if not val.startswith("http"):
            val = "https://" + val
        info["github"] = val

    # 4. Phone (exclude matches that are part of a URL)
    phone_m = re.search(r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", text)
    if phone_m:
        p_val = phone_m.group(0).strip()
        if not any(x in p_val for x in ["http", "linkedin", "github"]):
            info["phone"] = p_val

    # 5. Name and Location from first non-empty lines
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines:
        first_line = lines[0]
        cleaned = re.sub(r"[^a-zA-Z\s]", "", first_line).strip()
        words = cleaned.split()
        if 1 <= len(words) <= 4:
            info["name"] = cleaned.title()

        # Check subsequent header lines for location (e.g., 'Jabalpur, Madhya Pradesh | email | phone')
        for h_line in lines[1:4]:
            parts = [p.strip() for p in h_line.split("|")]
            for p in parts:
                if not any(k in p.lower() for k in ["@", "http", "linkedin", "github", "resume", "page", "+91", "phone"]):
                    cleaned_loc = re.sub(r"[^a-zA-Z0-9,\s\-]", "", p).strip()
                    if 3 < len(cleaned_loc) < 50 and not info["location"]:
                        info["location"] = cleaned_loc

    # 6. Fallback name from email if not detected
    if not info["name"] and info["email"]:
        prefix = info["email"].split("@")[0]
        parts = re.split(r"[._-]", prefix)
        info["name"] = " ".join([p.capitalize() for p in parts if p.isalpha()]) or "Candidate"

    return info


def parse_full_resume_sections(text: str) -> Dict[str, Any]:
    """
    Extract high-level sections (Summary, Education, Technical Skills, Certifications)
    from candidate's master resume text so they can serve as ground-truth reference.
    """
    sec_names = [
        "SUMMARY", "PROFESSIONAL SUMMARY", "CAREER OBJECTIVE",
        "EDUCATION", "ACADEMIC BACKGROUND",
        "TECHNICAL SKILLS", "SKILLS", "CORE COMPETENCIES",
        "TECHNICAL PROJECTS", "PROJECTS", "KEY PROJECTS",
        "CERTIFICATIONS & TRAINING", "CERTIFICATIONS", "TRAINING & CERTIFICATIONS",
        "EXPERIENCE", "WORK EXPERIENCE", "EMPLOYMENT HISTORY"
    ]
    sec_regex = r"(?:\n|\A)(" + "|".join(sec_names) + r")\s*\n"
    splits = re.split(sec_regex, text)

    sections: Dict[str, str] = {}
    for i in range(1, len(splits), 2):
        sec_title = splits[i].strip().upper()
        sec_body = splits[i+1].strip() if i+1 < len(splits) else ""
        # Remove trailing footer artifacts like "Page 1 of 1"
        sec_body = re.sub(r"\n.*Page \d+ of \d+.*", "", sec_body, flags=re.IGNORECASE).strip()
        sections[sec_title] = sec_body

    summary = sections.get("SUMMARY", sections.get("PROFESSIONAL SUMMARY", ""))
    education = sections.get("EDUCATION", sections.get("ACADEMIC BACKGROUND", ""))
    skills_raw = sections.get("TECHNICAL SKILLS", sections.get("SKILLS", sections.get("CORE COMPETENCIES", "")))
    certifications = sections.get(
        "CERTIFICATIONS & TRAINING",
        sections.get("CERTIFICATIONS", sections.get("TRAINING & CERTIFICATIONS", ""))
    )

    return {
        "summary": summary,
        "education": education,
        "skills_raw": skills_raw,
        "certifications": certifications,
    }


def extract_projects_and_experience(text: str) -> List[Dict[str, Any]]:
    """
    Segment the resume text into projects and experience entries formatted
    for master_portfolio.json and ChromaDB vector search.
    Handles both multi-paragraph blocks and bullet lists.
    """
    entries: List[Dict[str, Any]] = []

    tech_keywords = [
        "Python", "JavaScript", "TypeScript", "C++", "Java", "C#", "Go", "Rust",
        "React", "Vue", "Angular", "Django", "FastAPI", "Flask", "Node.js", "Express",
        "PyQt6", "PyQt5", "Docker", "Kubernetes", "AWS", "Azure", "GCP", "PostgreSQL",
        "MySQL", "MongoDB", "Redis", "SQLite", "GraphQL", "REST", "Git", "Linux",
        "ChromaDB", "Playwright", "Selenium", "Pandas", "NumPy", "PyTorch", "TensorFlow",
        "AsyncIO", "CI/CD", "WebRTC", "SQLAlchemy", "TailwindCSS", "Next.js"
    ]

    # Normalize newlines
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return entries

    # State tracking
    current_entry_name = ""
    current_bullets: List[str] = []
    current_desc = ""
    item_counter = 1

    generic_skip_headers = {
        "SKILLS", "TECHNICAL SKILLS", "EDUCATION", "ACADEMIC BACKGROUND",
        "SUMMARY", "PROFESSIONAL SUMMARY", "CONTACT", "AWARDS", "CERTIFICATIONS",
        "PROJECTS", "TECHNICAL PROJECTS", "WORK EXPERIENCE", "EXPERIENCE", "EMPLOYMENT"
    }

    def _flush_current():
        nonlocal current_entry_name, current_bullets, current_desc, item_counter
        if not current_entry_name and not current_bullets:
            return
        
        entry_title = current_entry_name or f"Experience / Project {item_counter}"
        # Skip if entry_title looks like contact info or candidate name header
        if "@" in entry_title or "http" in entry_title.lower() or "linkedin" in entry_title.lower() or "github" in entry_title.lower():
            current_entry_name = ""
            current_bullets = []
            current_desc = ""
            return
        # Skip top-of-resume candidate name or brief intro line if no bullets
        if not current_bullets and (len(entry_title.split()) <= 4 or len(current_desc) < 20):
            current_entry_name = ""
            current_bullets = []
            current_desc = ""
            return
        blob = entry_title + " " + " ".join(current_bullets) + " " + current_desc
        found_tech = []
        for tk in tech_keywords:
            if re.search(r"\b" + re.escape(tk) + r"\b", blob, re.IGNORECASE):
                if tk not in found_tech:
                    found_tech.append(tk)

        slug_id = re.sub(r"[^a-z0-9]+", "_", entry_title.lower()).strip("_") or f"proj_{item_counter}"
        tagline = f"Experience with {', '.join(found_tech[:3])}" if found_tech else "Software development and engineering experience"

        entries.append({
            "id": slug_id,
            "name": entry_title,
            "tagline": tagline,
            "description": current_desc or (current_bullets[0] if current_bullets else entry_title),
            "tech_stack": found_tech if found_tech else ["Python", "Software Engineering"],
            "domain": "Software Engineering & Systems Development",
            "highlights": current_bullets if current_bullets else [current_desc or entry_title],
            "role": "Software Developer / Engineer",
            "year": 2024,
            "keywords": found_tech + ["software", "engineering", "development", "architecture"]
        })
        item_counter += 1
        current_entry_name = ""
        current_bullets = []
        current_desc = ""

    for line in lines:
        # Check if line is a bullet
        is_bullet = bool(re.match(r"^[-•*–\d.]+\s+", line))
        clean_text = re.sub(r"^[-•*–\d.]+\s*", "", line).strip()

        # Check if line is a generic section header
        upper_line = clean_text.upper().strip(":")
        if upper_line in generic_skip_headers:
            # Flushes previous project when section switches
            _flush_current()
            continue

        if is_bullet:
            if len(clean_text) > 8:
                current_bullets.append(clean_text)
        else:
            # If line is short and looks like a project or job title
            if len(clean_text) < 70 and not clean_text.endswith("."):
                # If we already have bullets accumulated, this new line marks a new project!
                if current_bullets:
                    _flush_current()
                current_entry_name = clean_text
            else:
                if not current_desc:
                    current_desc = clean_text
                else:
                    current_bullets.append(clean_text)

    # Flush final accumulated block
    _flush_current()

    # Safety fallback
    if not entries:
        entries.append({
            "id": "master_experience",
            "name": "Master Professional Experience",
            "tagline": "Ground-truth work experience parsed from candidate's original resume",
            "description": text[:300].strip() + "...",
            "tech_stack": ["Python", "Software Engineering"],
            "domain": "Software Development & Architecture",
            "highlights": [l.strip() for l in text.splitlines() if len(l.strip()) > 30][:6],
            "role": "Software Engineer",
            "year": 2024,
            "keywords": ["experience", "software", "development"]
        })

    return entries

    # Safety: If parsing did not find distinct bullet blocks, create a holistic ground-truth block
    if not entries:
        entries.append({
            "id": "master_experience",
            "name": "Master Professional Experience",
            "tagline": "Ground-truth work experience parsed from candidate's original resume",
            "description": text[:300].strip() + "...",
            "tech_stack": [k for k in tech_keywords if re.search(r"\b" + re.escape(k) + r"\b", text, re.IGNORECASE)][:8] or ["Python", "Software Engineering"],
            "domain": "Software Development & Architecture",
            "highlights": [l.strip() for l in text.splitlines() if len(l.strip()) > 30][:6],
            "role": "Software Engineer",
            "year": 2024,
            "keywords": ["experience", "software", "development"]
        })

    return entries


# ── High-Level Ingestion Orchestrator ─────────────────────────────────────────

def ingest_master_resume(
    file_path: str | pathlib.Path,
    workspace_root: str = "f:/agent",
) -> Dict[str, Any]:
    """
    Main function called when user selects their resume file:
      1. Extracts full text (.pdf, .docx, .txt).
      2. Archives a master copy into `vault/master_resume/`.
      3. Parses contact details and updates `profile.json`.
      4. Parses projects/experience and updates `master_portfolio.json`.
      5. Returns an ingestion report dict with stats for UI presentation.
    """
    src_path = pathlib.Path(file_path).resolve()
    ws_root = pathlib.Path(workspace_root).resolve()

    if not src_path.exists():
        raise FileNotFoundError(f"File not found: {src_path}")

    # 1. Extract text
    raw_text = extract_text_from_file(src_path)
    if not raw_text.strip():
        raise ValueError("The selected resume file contains no readable text.")

    # 2. Archive copy in vault/master_resume/
    vault_dir = ws_root / "vault" / "master_resume"
    vault_dir.mkdir(parents=True, exist_ok=True)
    target_archive = vault_dir / f"master_resume{src_path.suffix.lower()}"
    if src_path.resolve() != target_archive.resolve():
        shutil.copy2(str(src_path), str(target_archive))
    logger.info("Archived original resume to %s", target_archive)

    # If user uploaded a Word document, copy directly to templates/master_template.docx
    templates_dir = ws_root / "templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    if src_path.suffix.lower() in (".docx", ".doc"):
        shutil.copy2(str(src_path), str(templates_dir / "master_template.docx"))
        logger.info("Saved user Word resume as master template in templates/master_template.docx")

    # 3. Parse contact info and full sections
    contact_data = parse_contact_info(raw_text)
    sec_data = parse_full_resume_sections(raw_text)

    profile_path = ws_root / "profile.json"
    existing_profile: Dict[str, Any] = {}
    if profile_path.exists():
        try:
            existing_profile = json.loads(profile_path.read_text(encoding="utf-8"))
        except Exception:
            existing_profile = {}

    # Merge contact & section data preserving existing values if extraction missed
    merged_profile = {
        **existing_profile,
        "name": (contact_data.get("name") or existing_profile.get("name") or "Candidate").strip(),
        "email": (contact_data.get("email") or existing_profile.get("email") or "candidate@example.com").strip(),
        "phone": (contact_data.get("phone") or existing_profile.get("phone") or "+1-555-010-0000").strip(),
        "linkedin": (contact_data.get("linkedin") or existing_profile.get("linkedin") or "https://linkedin.com").strip(),
        "github": (contact_data.get("github") or existing_profile.get("github") or "https://github.com").strip(),
        "location": (contact_data.get("location") or existing_profile.get("location") or "").strip(),
        "years_exp": contact_data.get("years_exp") or existing_profile.get("years_exp") or 2,
        "summary": (sec_data.get("summary") or existing_profile.get("summary") or "").strip(),
        "education": (sec_data.get("education") or existing_profile.get("education") or "").strip(),
        "skills_raw": (sec_data.get("skills_raw") or existing_profile.get("skills_raw") or "").strip(),
        "certifications": (sec_data.get("certifications") or existing_profile.get("certifications") or "").strip(),
        "master_resume_path": str(target_archive),
        "master_resume_filename": src_path.name,
    }
    profile_path.write_text(json.dumps(merged_profile, indent=2), encoding="utf-8")
    logger.info("Updated %s with parsed resume contact & section details.", profile_path)

    # 4. Parse projects/experience and update master_portfolio.json
    parsed_projects = extract_projects_and_experience(raw_text)
    # Ensure every single project dict strictly contains all required schema keys
    clean_projects = []
    for idx, p in enumerate(parsed_projects, start=1):
        clean_p = {
            "id": p.get("id") or f"project_{idx}",
            "name": p.get("name") or f"Project / Experience {idx}",
            "tagline": p.get("tagline") or "Software engineering experience",
            "description": p.get("description") or (p.get("name") or f"Project {idx}"),
            "tech_stack": p.get("tech_stack") if isinstance(p.get("tech_stack"), list) else ["Python", "Software Engineering"],
            "domain": p.get("domain") or "Software Engineering & Systems Development",
            "highlights": p.get("highlights") if isinstance(p.get("highlights"), list) else [p.get("description") or f"Project {idx}"],
            "role": p.get("role") or "Software Developer / Engineer",
            "year": int(p.get("year", 2024)),
            "keywords": p.get("keywords") if isinstance(p.get("keywords"), list) else ["software", "engineering", "development"],
        }
        clean_projects.append(clean_p)

    portfolio_path = ws_root / "master_portfolio.json"
    portfolio_payload = {"projects": clean_projects}
    portfolio_path.write_text(json.dumps(portfolio_payload, indent=2), encoding="utf-8")
    logger.info("Updated %s with %d parsed projects/experiences.", portfolio_path, len(clean_projects))

    # Also save raw resume text to disk for direct reference
    raw_txt_path = vault_dir / "master_resume_text.txt"
    raw_txt_path.write_text(raw_text, encoding="utf-8")

    return {
        "status": "success",
        "filename": src_path.name,
        "archive_path": str(target_archive),
        "name": merged_profile.get("name", "Candidate"),
        "email": merged_profile.get("email", "candidate@example.com"),
        "project_count": len(clean_projects),
        "projects_count": len(clean_projects),
        "skills_count": len(merged_profile.get("skills_raw", "").split("\n")),
        "raw_text_len": len(raw_text),
        "projects": clean_projects,
    }
