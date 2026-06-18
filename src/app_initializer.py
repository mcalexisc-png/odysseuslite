# src/app_initializer.py
"""Initialize all application components and dependencies."""
import os
import logging
from typing import Dict, Any

from src.constants import (
    DATA_DIR, PERSONAL_DIR, RUNBOOK_DIR, UPLOAD_DIR,
    SESSIONS_FILE, DEFAULT_HOST, OPENAI_API_KEY
)
from src.memory import MemoryManager
from src.memory_provider import MemoryProviderRegistry, NativeMemoryProvider
from services.memory.skills import SkillsManager
from core.session_manager import SessionManager
from core.models import set_session_manager
from src.personal_docs import PersonalDocsManager
from src.api_key_manager import APIKeyManager
from src.preset_manager import PresetManager
from src.chat_processor import ChatProcessor
from src.model_discovery import ModelDiscovery
from src.chat_handler import ChatHandler
from src.research_handler import ResearchHandler
from src.upload_handler import UploadHandler
from src.search import update_search_config

logger = logging.getLogger(__name__)


def _init_memory_vector(data_dir: str, memory_manager, rag_manager):
    """Select + build the memory vector store per MEMORY_BACKEND (lite overlay).

    Returns a store with the MemoryVectorStore interface, or None for
    keyword-only recall. Always degrades to None (keyword-only) on any failure
    rather than raising — the app must boot even with no embedding model.
    """
    backend = (os.getenv("MEMORY_BACKEND") or "sqlite_fts").strip().lower()

    if backend == "sqlite_fts":
        # Ultra-minimal default: keyword/BM25 recall only (chat_processor handles it).
        logger.info("MEMORY_BACKEND=sqlite_fts — keyword-only recall (no vector store)")
        return None

    store = None
    try:
        if backend == "hybrid":
            from services.memory_hybrid import SqliteVecMemoryStore
            store = SqliteVecMemoryStore(data_dir)
        else:  # chromadb (or any legacy value) -> upstream vector store
            from src.memory_vector import MemoryVectorStore
            embedding_model = getattr(rag_manager, "_model", None) if rag_manager else None
            store = MemoryVectorStore(data_dir, embedding_model=embedding_model)
    except Exception as e:
        logger.warning(
            "MEMORY_BACKEND=%s init failed (%s) — falling back to keyword-only sqlite_fts",
            backend, e,
        )
        return None

    if not store or not store.healthy:
        logger.warning(
            "MEMORY_BACKEND=%s unavailable — falling back to keyword-only sqlite_fts",
            backend,
        )
        return None

    # Migration: re-index existing memories into the (empty) vector store.
    try:
        if store.count() == 0:
            existing = memory_manager.load()
            if existing:
                store.rebuild(existing)
                logger.info("Re-indexed %d existing memories into %s vector store",
                            len(existing), backend)
    except Exception as e:
        logger.warning("Memory vector re-index skipped: %s", e)

    logger.info("MEMORY_BACKEND=%s vector store initialized", backend)
    return store


def create_directories():
    """Create necessary directories if they don't exist."""
    for directory in (DATA_DIR, PERSONAL_DIR, RUNBOOK_DIR, UPLOAD_DIR):
        os.makedirs(directory, exist_ok=True)
        
def initialize_managers(base_dir: str, rag_manager=None) -> Dict[str, Any]:
    """
    Initialize all manager and handler instances.

    Args:
        base_dir: Base directory path
        rag_manager: RAG manager instance (optional)
    Returns:
        Dictionary containing all initialized components
    """
    # Create directories first
    create_directories()

    # Initialize core managers
    memory_manager = MemoryManager(DATA_DIR)
    skills_manager = SkillsManager(DATA_DIR)
    session_manager = SessionManager(SESSIONS_FILE)
    set_session_manager(session_manager)  # Enable Session.add_message() persistence
    upload_handler = UploadHandler(base_dir, UPLOAD_DIR)
    personal_docs_manager = PersonalDocsManager(PERSONAL_DIR, rag_manager)
    api_key_manager = APIKeyManager(DATA_DIR)
    preset_manager = PresetManager(DATA_DIR)

    # Initialize the memory vector store. The dense-vector half of hybrid recall.
    # Backend is chosen by MEMORY_BACKEND (Odysseus Lite overlay):
    #   sqlite_fts (default) -> keyword-only; no vector store (memory_vector=None)
    #   hybrid               -> lite sqlite-vec + tiny static embeddings
    #   chromadb             -> upstream MemoryVectorStore (heavy opt-in)
    # Any failure degrades cleanly to keyword-only rather than crashing.
    memory_vector = _init_memory_vector(DATA_DIR, memory_manager, rag_manager)

    memory_provider_registry = MemoryProviderRegistry([
        NativeMemoryProvider(memory_manager, memory_vector),
    ])

    # Initialize processors
    chat_processor = ChatProcessor(memory_manager, personal_docs_manager, memory_vector=memory_vector, skills_manager=skills_manager)
    research_handler = ResearchHandler()
    
    # Initialize chat handler with all dependencies
    chat_handler = ChatHandler(
        session_manager=session_manager,
        memory_manager=memory_manager,
        chat_processor=chat_processor,
        research_handler=research_handler,
        preset_manager=preset_manager,
        upload_handler=upload_handler,
    )
    
    # Initialize model discovery
    model_discovery = ModelDiscovery(DEFAULT_HOST, OPENAI_API_KEY)
    
    # Load and apply saved API keys
    saved_keys = api_key_manager.load()
    if "brave" in saved_keys:
        update_search_config(api_key=saved_keys["brave"])
        logger.info("Loaded Brave API key from saved configuration")
    
    return {
        "memory_manager": memory_manager,
        "memory_vector": memory_vector,
        "memory_provider_registry": memory_provider_registry,
        "skills_manager": skills_manager,
        "session_manager": session_manager,
        "upload_handler": upload_handler,
        "personal_docs_manager": personal_docs_manager,
        "api_key_manager": api_key_manager,
        "preset_manager": preset_manager,
        "chat_processor": chat_processor,
        "research_handler": research_handler,
        "chat_handler": chat_handler,
        "model_discovery": model_discovery,
        "current_presets": preset_manager.presets,
        "PERSONAL_INDEX": personal_docs_manager.index
    }
