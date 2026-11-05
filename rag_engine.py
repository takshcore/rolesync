"""
rag_engine.py – Module 3: The Truth Source
==========================================
Role in the RoleSync system
----------------------------
RoleSync tailors job applications by injecting *relevant* portfolio evidence
into every LLM prompt.  This module is the retrieval backbone that makes that
possible.

It does three things:
  1. Loads a structured portfolio of past projects from ``master_portfolio.json``.
  2. Embeds each project's textual content and stores the vectors in a local
     ChromaDB collection so that queries are fast and require no network call.
  3. Exposes a clean ``RAGEngine`` interface that the rest of the system (prompt
     builder, cover-letter writer, etc.) calls to get the most relevant context
     for any given job description.

Why local ChromaDB?
  ChromaDB can persist embeddings to disk, meaning the expensive embedding step
  only runs once (on first load / when new projects are added).  Subsequent
  cold-starts skip re-embedding and go straight to querying.

Why sentence-transformers / all-MiniLM-L6-v2?
  It is a compact (80 MB), fast, and high-quality bi-encoder that runs entirely
  on CPU.  It consistently ranks among the best small models on the MTEB
  retrieval benchmark, making it ideal for a local-first agent.

Dependencies
------------
  pip install chromadb sentence-transformers

File layout expected by this module
-------------------------------------
  f:/agent/
  ├── rag_engine.py          ← this file
  ├── master_portfolio.json  ← project data (seeded alongside this module)
  └── .chromadb/             ← ChromaDB persistent storage (auto-created)
"""

# ══════════════════════════════════════════════════════════════════════════════
# Standard-library imports
# ══════════════════════════════════════════════════════════════════════════════
import json        # For loading master_portfolio.json from disk
import os          # For resolving file paths in a cross-platform way
import textwrap    # For wrapping long strings when building the context block
from typing import Any  # Generic type alias used in return-type hints

# ══════════════════════════════════════════════════════════════════════════════
# Third-party imports
# ══════════════════════════════════════════════════════════════════════════════
import chromadb                                     # Local vector database
from chromadb.config import Settings                # ChromaDB config object
from sentence_transformers import SentenceTransformer  # CPU-friendly embedder

# ══════════════════════════════════════════════════════════════════════════════
# Path constants
# ══════════════════════════════════════════════════════════════════════════════

# Root of the RoleSync workspace — all paths are relative to this.
# Using os.path.dirname(__file__) makes the module portable even if the CWD
# changes (which happens inside some agent frameworks).
_AGENT_ROOT: str = os.path.dirname(os.path.abspath(__file__))

# Absolute path to the portfolio JSON that acts as the single source of truth
# for all project data.  Keeping it as a plain JSON file (not a DB) means a
# human can edit / review it without any tooling.
PORTFOLIO_PATH: str = os.path.join(_AGENT_ROOT, "master_portfolio.json")

# Directory where ChromaDB will persist its SQLite + binary vector files.
# A leading dot keeps it hidden on Unix-like systems and signals "managed data".
CHROMA_PERSIST_DIR: str = os.path.join(_AGENT_ROOT, ".chromadb")

# Name of the ChromaDB collection that holds portfolio embeddings.
# Using a constant avoids magic-string drift if the name changes later.
COLLECTION_NAME: str = "portfolio"

# The sentence-transformers model identifier.  all-MiniLM-L6-v2 is 80 MB,
# fully open-weight, and runs on CPU in ~5 ms per sentence — ideal for a local
# agent that must not depend on external API keys for retrieval.
EMBED_MODEL_NAME: str = "all-MiniLM-L6-v2"


# ══════════════════════════════════════════════════════════════════════════════
# Embedder wrapper
# ══════════════════════════════════════════════════════════════════════════════

class PortfolioEmbedder:
    """
    Thin wrapper around ``SentenceTransformer`` that satisfies ChromaDB's
    requirement for plain Python ``list[float]`` embeddings (ChromaDB rejects
    NumPy arrays passed directly).

    Parameters
    ----------
    model_name : str
        Any sentence-transformers model identifier.  Defaults to the globally
        configured ``EMBED_MODEL_NAME``.

    Attributes
    ----------
    _model : SentenceTransformer
        The underlying transformer model held in memory for reuse.
    """

    def __init__(self, model_name: str = EMBED_MODEL_NAME) -> None:
        # Load the transformer model from the local HuggingFace cache (or
        # download it on first run).  Storing it as an instance attribute means
        # a single RAGEngine instance pays the load cost only once.
        print(f"[PortfolioEmbedder] Loading model '{model_name}' …")
        self._model: SentenceTransformer = SentenceTransformer(model_name)
        print("[PortfolioEmbedder] Model ready.")

    def embed(self, texts: list[str]) -> list[list[float]]:
        """
        Embed a batch of text strings into float vectors.

        ChromaDB's ``upsert`` and ``query`` methods both accept embeddings as
        ``list[list[float]]`` — NumPy arrays cause a runtime type error, so we
        call ``.tolist()`` on the output tensor.

        Parameters
        ----------
        texts : list[str]
            One or more strings to encode.

        Returns
        -------
        list[list[float]]
            A list of embedding vectors, one per input string.

        Raises
        ------
        ValueError
            If ``texts`` is empty (ChromaDB would raise an obscure error later).
        """
        if not texts:
            # Guard early so we surface a clear error rather than a cryptic
            # ChromaDB exception about empty upsert payloads.
            raise ValueError("embed() received an empty list — nothing to encode.")

        # encode() returns a NumPy ndarray of shape (N, dim).
        # .tolist() converts it to a nested Python list, which ChromaDB accepts.
        raw_embeddings = self._model.encode(texts, show_progress_bar=False)
        return raw_embeddings.tolist()  # type: ignore[return-value]

    def embed_one(self, text: str) -> list[float]:
        """
        Convenience wrapper to embed a single string and return a flat list.

        Parameters
        ----------
        text : str
            The string to embed.

        Returns
        -------
        list[float]
            A single embedding vector as a plain Python list.
        """
        # Reuse embed() so the type-conversion logic lives in one place.
        return self.embed([text])[0]  # Unwrap the outer list of one element


# ══════════════════════════════════════════════════════════════════════════════
# Portfolio loader
# ══════════════════════════════════════════════════════════════════════════════

def load_portfolio(path: str = PORTFOLIO_PATH) -> list[dict[str, Any]]:
    """
    Read and parse the ``master_portfolio.json`` file into a list of project
    dictionaries.

    Parameters
    ----------
    path : str
        Absolute path to the JSON file.  Defaults to ``PORTFOLIO_PATH``.

    Returns
    -------
    list[dict[str, Any]]
        A list of project dicts, each matching the schema defined in
        ``master_portfolio.json``.

    Raises
    ------
    FileNotFoundError
        If the portfolio JSON does not exist at ``path``.
    KeyError
        If the JSON does not contain a top-level ``"projects"`` key.
    """
    if not os.path.exists(path):
        # Fail early with a human-friendly message rather than a raw Python
        # FileNotFoundError with a cryptic trace.
        raise FileNotFoundError(
            f"Portfolio file not found at: {path}\n"
            "Please make sure master_portfolio.json exists in f:/agent/"
        )

    # Open with explicit UTF-8 so accented characters in project names survive
    # on Windows where the default encoding may be cp1252.
    with open(path, encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)  # Parse the entire JSON in one shot

    # The JSON has a top-level "projects" wrapper; extract the list.
    projects: list[dict[str, Any]] = data["projects"]

    print(f"[load_portfolio] Loaded {len(projects)} projects from {path}")
    return projects


# ══════════════════════════════════════════════════════════════════════════════
# Document serialiser
# ══════════════════════════════════════════════════════════════════════════════

def _project_to_document(project: dict[str, Any]) -> str:
    """
    Serialise a project dict into a single rich text string suitable for
    embedding.

    Why not embed raw JSON?
    Raw JSON contains structural noise (braces, quotes, colons) that the
    transformer tokeniser wastes capacity on.  A clean prose representation
    packs more semantic signal into the model's context window.

    Parameters
    ----------
    project : dict[str, Any]
        A single project dict loaded from ``master_portfolio.json``.

    Returns
    -------
    str
        A multi-line text document combining the project's most discriminating
        fields.
    """
    # Join list fields with commas so they read as natural language rather than
    # Python repr (e.g. "PyTorch, ResNet50, FastAPI" not "['PyTorch', ...]").
    tech_str: str = ", ".join(project.get("tech_stack", []))
    keywords_str: str = ", ".join(project.get("keywords", []))
    highlights_str: str = "\n- ".join(project.get("highlights", []))
    domain_str: str = project.get("domain", "")

    # Build a structured prose block.  Order matters: the most distinctive
    # fields (name, tagline, description) come first so they anchor the
    # embedding's semantic direction.
    document: str = (
        f"Project: {project['name']}\n"
        f"Tagline: {project['tagline']}\n"
        f"Domain: {domain_str}\n"
        f"Description: {project['description']}\n"
        f"Tech stack: {tech_str}\n"
        f"Key achievements:\n- {highlights_str}\n"
        f"Keywords: {keywords_str}"
    )
    return document


# ══════════════════════════════════════════════════════════════════════════════
# RAGEngine – the main public class
# ══════════════════════════════════════════════════════════════════════════════

class RAGEngine:
    """
    Retrieval-Augmented Generation engine for the RoleSync project.

    On construction it:
      * Loads the portfolio from ``master_portfolio.json``.
      * Opens (or creates) a persistent ChromaDB collection called "portfolio".
      * Upserts every project's text embedding into that collection so that
        fast vector similarity queries are available immediately.

    At query time it:
      * Embeds the incoming job description.
      * Retrieves the top-N most cosine-similar projects from ChromaDB.
      * Returns the matched project dicts with distance scores attached.

    Parameters
    ----------
    portfolio_path : str
        Path to the portfolio JSON.  Defaults to ``PORTFOLIO_PATH``.
    chroma_persist_dir : str
        Directory where ChromaDB stores its data files.  Defaults to
        ``CHROMA_PERSIST_DIR``.
    embed_model_name : str
        sentence-transformers model identifier.  Defaults to
        ``EMBED_MODEL_NAME``.

    Attributes
    ----------
    portfolio : list[dict[str, Any]]
        The raw project data loaded from JSON.
    _embedder : PortfolioEmbedder
        The embedder instance shared across upsert and query operations.
    _collection : chromadb.Collection
        The ChromaDB collection object used for all vector operations.
    """

    # ── Construction ──────────────────────────────────────────────────────────

    def __init__(
        self,
        portfolio_path: str = PORTFOLIO_PATH,
        chroma_persist_dir: str = CHROMA_PERSIST_DIR,
        embed_model_name: str = EMBED_MODEL_NAME,
    ) -> None:
        """
        Initialise the RAG engine: load data, connect to ChromaDB, seed vectors.

        Parameters
        ----------
        portfolio_path : str
            Filesystem path to ``master_portfolio.json``.
        chroma_persist_dir : str
            Directory for ChromaDB's persistent storage.
        embed_model_name : str
            Identifier for the sentence-transformers model to use.
        """
        # ── Step 1: Load the portfolio from disk ─────────────────────────────
        # This is the single source of truth; all other state derives from it.
        self.portfolio: list[dict[str, Any]] = load_portfolio(portfolio_path)

        # ── Step 2: Instantiate the embedder ─────────────────────────────────
        # Stored as an instance attribute so we don't reload the model weights
        # each time query() or get_context_block() is called.
        self._embedder: PortfolioEmbedder = PortfolioEmbedder(embed_model_name)

        # ── Step 3: Connect to (or create) the ChromaDB persistent client ─────
        # PersistentClient automatically creates chroma_persist_dir if absent
        # and re-opens an existing database if present, giving us an
        # incremental upsert semantic at no extra cost.
        print(f"[RAGEngine] Opening ChromaDB at: {chroma_persist_dir}")
        self._client: chromadb.ClientAPI = chromadb.PersistentClient(
            path=chroma_persist_dir  # Where SQLite + HNSW index files live
        )

        # ── Step 4: Get or create the "portfolio" collection ──────────────────
        # get_or_create_collection is idempotent: safe to call on every startup.
        # We specify cosine distance because sentence-transformer embeddings are
        # directional (magnitude carries little information); cosine similarity
        # is the standard metric for semantic search with these models.
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,          # Logical name for the collection
            metadata={"hnsw:space": "cosine"},  # Use cosine distance for ANN
        )
        print(f"[RAGEngine] Collection '{COLLECTION_NAME}' ready.")

        # ── Step 5: Upsert all project documents into the collection ──────────
        # Using upsert (not add) means re-running __init__ never creates
        # duplicates; it just refreshes vectors if a project document changed.
        self._upsert_portfolio()

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _upsert_portfolio(self) -> None:
        """
        Embed every project and upsert into the ChromaDB collection.

        Called once during ``__init__``.  On subsequent startups ChromaDB's
        HNSW index is already built from disk, so only changed / new documents
        trigger re-embedding (upsert is diff-aware on the ``id`` field).

        Returns
        -------
        None
        """
        # Build parallel lists that ChromaDB's upsert() expects.
        ids: list[str] = []            # Unique string ID per document
        documents: list[str] = []      # Raw text that was embedded (stored for retrieval)
        embeddings: list[list[float]] = []  # Corresponding float vectors
        metadatas: list[dict[str, Any]] = []  # Structured metadata for post-filter queries

        for project in self.portfolio:
            # Use the project's own "id" field as the ChromaDB document ID so
            # that upserts are deterministic and human-readable.
            ids.append(project["id"])

            # Serialise the project to a rich prose document before embedding,
            # so the vector captures the full context of the project.
            doc_text: str = _project_to_document(project)
            documents.append(doc_text)

            # Embed the document text to a float vector.
            # We embed one at a time here (rather than batching) for clarity;
            # for large portfolios you would batch for efficiency.
            vec: list[float] = self._embedder.embed_one(doc_text)
            embeddings.append(vec)

            # Store lightweight metadata alongside the vector so callers can
            # filter by domain, year, etc., in future without touching the
            # portfolio JSON.
            metadatas.append(
                {
                    "name": project["name"],           # Human-readable label
                    "domain": project.get("domain", ""),  # Domain string for display
                    "year": int(project.get("year", 0)),  # Year for recency sorting
                    "tech_stack": ", ".join(project.get("tech_stack", [])),  # Searchable tech list
                }
            )

        # Upsert in a single batch call — ChromaDB batches internal writes,
        # making this much faster than N individual upsert() calls.
        self._collection.upsert(
            ids=ids,               # Document identifiers
            documents=documents,   # Raw text (stored, not just indexed)
            embeddings=embeddings, # Pre-computed float vectors
            metadatas=metadatas,   # Structured metadata for future filtering
        )
        print(f"[RAGEngine] Upserted {len(ids)} project(s) into '{COLLECTION_NAME}'.")

    # ── Public API ─────────────────────────────────────────────────────────────

    def query(
        self,
        job_description: str,
        n_results: int = 3,
    ) -> list[dict[str, Any]]:
        """
        Retrieve the top-N portfolio projects most relevant to a job description.

        The job description is embedded with the same model used at index time,
        then ChromaDB performs approximate nearest-neighbour (ANN) search using
        the HNSW index and cosine distance.

        Parameters
        ----------
        job_description : str
            The raw text of a job posting.  Can be long; the embedder handles
            truncation internally (models max at 512 tokens).
        n_results : int, optional
            How many projects to return, by default 3.  Clipped to the total
            number of projects so ChromaDB doesn't raise on small collections.

        Returns
        -------
        list[dict[str, Any]]
            A list of dicts, each containing:
              - ``"id"`` (str): project ID
              - ``"name"`` (str): project name
              - ``"document"`` (str): full serialised text that was embedded
              - ``"metadata"`` (dict): lightweight metadata stored at upsert
              - ``"distance"`` (float): cosine distance (lower = more similar)
              - ``"project"`` (dict): the original portfolio entry for this id

        Raises
        ------
        ValueError
            If ``job_description`` is an empty string.
        """
        if not job_description.strip():
            # Surface a clear error; an empty query produces meaningless results.
            raise ValueError("job_description must not be empty.")

        # Clip n_results so we never ask for more results than items in the
        # collection (ChromaDB raises an error in that case).
        effective_n: int = min(n_results, len(self.portfolio))

        # Embed the job description using the same model (and therefore the same
        # vector space) that was used to embed the portfolio documents.
        jd_vector: list[float] = self._embedder.embed_one(job_description)

        # Query the HNSW index.  We pass the embedding directly instead of a
        # text string so ChromaDB does not try to use its own (default) embedder.
        raw_results = self._collection.query(
            query_embeddings=[jd_vector],  # Outer list because query accepts a batch
            n_results=effective_n,         # Number of neighbours to return
            include=["documents", "metadatas", "distances"],  # Fields to return
        )

        # Build a convenience lookup so we can join ChromaDB results back to the
        # full portfolio dict (which has richer data than the stored metadata).
        portfolio_lookup: dict[str, dict[str, Any]] = {
            p["id"]: p for p in self.portfolio  # Key by project id
        }

        # ChromaDB returns parallel lists wrapped in an outer list (because query
        # accepts batches).  Index 0 unwraps the single-query batch.
        result_ids: list[str] = raw_results["ids"][0]
        result_docs: list[str] = raw_results["documents"][0]
        result_metas: list[dict] = raw_results["metadatas"][0]
        result_dists: list[float] = raw_results["distances"][0]

        # Zip the parallel lists into a list of unified result dicts.
        results: list[dict[str, Any]] = []
        for doc_id, doc_text, meta, dist in zip(
            result_ids, result_docs, result_metas, result_dists
        ):
            results.append(
                {
                    "id": doc_id,            # Project identifier
                    "name": meta["name"],    # Human-readable name from metadata
                    "document": doc_text,    # Full text that was embedded
                    "metadata": meta,        # Lightweight metadata dict
                    "distance": dist,        # Cosine distance; 0 = identical
                    # Attach the original full portfolio dict so callers have
                    # access to highlights, keywords, role, etc.
                    "project": portfolio_lookup.get(doc_id, {}),
                }
            )

        return results

    def get_context_block(self, job_description: str) -> str:
        """
        Return a formatted markdown block of the top matching projects for
        injection into an LLM system prompt.

        This is the primary method consumed by the prompt-builder module.  It
        wraps ``query()`` and formats the results so the LLM can easily parse
        them without further pre-processing.

        Parameters
        ----------
        job_description : str
            The raw job posting text to retrieve context for.

        Returns
        -------
        str
            A markdown-formatted string containing numbered project summaries,
            ready for direct inclusion in a prompt template.

        Example output
        --------------
        ## Relevant Portfolio Projects

        ### 1. NeuralNotes (distance: 0.1234)
        **Domain:** LLM Applications, RAG, NLP, EdTech
        **Tech:** LangChain, ChromaDB, OpenAI GPT-4o, ...
        **Tagline:** LLM-powered study assistant that answers questions from your own uploaded notes
        **Highlights:**
        - RAG pipeline reduced hallucination rate from 34 % (vanilla GPT) to <3 % ...
        ...
        """
        # Retrieve the most relevant projects for this JD.
        matches: list[dict[str, Any]] = self.query(job_description)

        # Start the block with a clear header so the LLM knows what section
        # it is reading and can cite projects by number.
        lines: list[str] = [
            "## Relevant Portfolio Projects",
            "",  # Blank line for readability
            "The following projects from the candidate's portfolio are most "
            "relevant to the job description above. Use them as evidence when "
            "writing the application.",
            "",
        ]

        for rank, match in enumerate(matches, start=1):
            project: dict[str, Any] = match["project"]  # Full portfolio entry
            dist: float = match["distance"]              # Cosine distance score

            # Section header with rank and distance for the LLM's reference.
            lines.append(f"### {rank}. {project['name']} *(similarity distance: {dist:.4f})*")

            # Domain line — helps the LLM understand the technical context.
            lines.append(f"**Domain:** {project.get('domain', 'N/A')}")

            # Tech stack on one line — directly addressable in cover letters.
            tech_str: str = ", ".join(project.get("tech_stack", []))
            lines.append(f"**Tech Stack:** {tech_str}")

            # Tagline — a one-sentence description safe to quote in applications.
            lines.append(f"**Tagline:** {project.get('tagline', '')}")

            # Role — important for demonstrating individual contribution level.
            lines.append(f"**Role:** {project.get('role', 'N/A')}")

            # Highlights — the most compelling evidence; each is a quantified
            # achievement that can be mapped directly to job requirements.
            lines.append("**Key Highlights:**")
            for highlight in project.get("highlights", []):
                # Wrap long highlights at 100 chars with hanging indent so the
                # LLM's context window is used efficiently.
                wrapped: str = textwrap.fill(
                    f"- {highlight}",
                    width=100,
                    subsequent_indent="  ",  # Hanging indent for wrapped lines
                )
                lines.append(wrapped)

            lines.append("")  # Blank line between projects for visual separation

        # Join all lines with newlines into a single string ready for prompt injection.
        context_block: str = "\n".join(lines)
        return context_block

    def reload_portfolio(self) -> None:
        """Reload master_portfolio.json from disk and re-embed all documents into ChromaDB."""
        self.portfolio = load_portfolio(self.portfolio_path)
        self._upsert_portfolio()
        print(f"[RAGEngine] Portfolio reloaded & re-indexed ({len(self.portfolio)} projects).")


# ══════════════════════════════════════════════════════════════════════════════
# Standalone smoke test
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    """
    Smoke test: instantiate the RAGEngine and query it with a sample ML/AI job
    description.  Run this module directly to verify the full pipeline end-to-end:

        python f:/agent/rag_engine.py

    Expected behaviour:
      - Model weights are downloaded (~80 MB) on first run, cached thereafter.
      - ChromaDB is seeded / opened at f:/agent/.chromadb/
      - Three matching projects are printed with distance scores < 0.5.
    """

    # Sample ML-focused job description that should surface:
    #   1. Cattle Classify (ResNet50, computer vision)
    #   2. NeuralNotes (LLM, embeddings, vector DB)
    #   3. PulseAPI or DataWeave (depending on weighting)
    SAMPLE_JD: str = """
    We are looking for a Machine Learning Engineer with hands-on experience in
    deep learning and computer vision.  The ideal candidate will have:

    - Strong proficiency in Python and PyTorch or TensorFlow.
    - Experience with transfer learning techniques (ResNet, EfficientNet, ViT).
    - Familiarity with MLOps: experiment tracking (W&B / MLflow), model serving,
      and containerisation with Docker.
    - Bonus: experience with LLMs, vector databases (ChromaDB, Pinecone), or
      retrieval-augmented generation (RAG) pipelines.
    - Understanding of data pipelines and cloud infrastructure (AWS preferred).

    You will develop and deploy production-ready CV and NLP models, working
    closely with the data engineering team.
    """

    print("=" * 70)
    print("RoleSync RAGEngine – Smoke Test")
    print("=" * 70)
    print()

    # ── Instantiate the engine ────────────────────────────────────────────────
    # This seeds ChromaDB on the first run; subsequent runs skip re-embedding.
    engine = RAGEngine()

    print()
    print("── query() result (top 3 matches) ──")
    # Call query() to get raw result dicts for programmatic inspection.
    results = engine.query(SAMPLE_JD, n_results=3)
    for i, result in enumerate(results, start=1):
        # Print the project name and cosine distance so we can sanity-check
        # that the right projects are surfacing for this ML-focused JD.
        print(f"  {i}. {result['name']:25s}  distance={result['distance']:.4f}")

    print()
    print("── get_context_block() output (formatted for LLM prompt) ──")
    # Call get_context_block() to see the final prompt-ready markdown.
    block: str = engine.get_context_block(SAMPLE_JD)
    print(block)

    print("=" * 70)
    print("Smoke test complete.")
    print("=" * 70)
