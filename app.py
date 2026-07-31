from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from ddgs import DDGS
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import signal
import time
from urllib.parse import urlparse
from urllib.request import Request as UrlRequest, urlopen
import trafilatura
from collections import Counter
import base64
import csv
import io
import json
import textwrap
import zipfile
import xml.etree.ElementTree as ET
import nltk
import requests

try:
    from readability import Document as ReadabilityDocument
    _READABILITY_OK = True
except ImportError:
    _READABILITY_OK = False

try:
    from bs4 import BeautifulSoup
    _BS4_OK = True
except ImportError:
    _BS4_OK = False

try:
    from sumy.parsers.plaintext import PlaintextParser
    from sumy.nlp.tokenizers import Tokenizer as SumyTokenizer
    from sumy.summarizers.text_rank import TextRankSummarizer
    from sumy.summarizers.lex_rank import LexRankSummarizer
    from sumy.summarizers.lsa import LsaSummarizer
    _SUMY_OK = True
except ImportError:
    _SUMY_OK = False

try:
    from llama_cpp import Llama as _LlamaCpp
    import llama_cpp as _llama_cpp_mod
    import ctypes as _ctypes
    _LLAMA_LOG_CB = _ctypes.CFUNCTYPE(None, _ctypes.c_int, _ctypes.c_char_p, _ctypes.c_void_p)(
        lambda level, msg, userdata: None
    )
    try:
        _llama_cpp_mod.llama_log_set(_LLAMA_LOG_CB, None)
    except Exception:
        pass
    del _llama_cpp_mod, _ctypes
    _LLAMA_CPP_OK = True
except ImportError:
    _LlamaCpp = None
    _LLAMA_CPP_OK = False

try:
    import warnings as _warnings, logging as _logging
    _warnings.filterwarnings("ignore", message=".*[Ii]mpersonate.*", category=UserWarning)
    _warnings.filterwarnings("ignore", message=".*[Ii]mpersonate.*")
    _logging.getLogger("curl_cffi").setLevel(_logging.ERROR)
    _logging.getLogger("g4f").setLevel(_logging.ERROR)
    from g4f.client import Client as _G4FClient
    import g4f as _g4f
    _G4F_OK = True
except ImportError:
    _G4FClient = None
    _g4f = None
    _G4F_OK = False

# ensure sumy tokenizer resources
for _nltk_pkg in ("punkt", "punkt_tab", "stopwords"):
    try:
        nltk.data.find(f"tokenizers/{_nltk_pkg}" if _nltk_pkg != "stopwords" else f"corpora/{_nltk_pkg}")
    except LookupError:
        try:
            nltk.download(_nltk_pkg, quiet=True)
        except Exception:
            pass

app = FastAPI(title="Search Engine")
templates = Jinja2Templates(directory="templates")


def _cleanup_session_artifacts():
    now_ts = time.time()
    workspace_dir = os.path.dirname(os.path.abspath(__file__))

    try:
        for name in os.listdir(workspace_dir):
            lower_name = str(name or "").lower()
            path = os.path.join(workspace_dir, name)

            if lower_name.startswith(".tmp"):
                try:
                    if os.path.isdir(path):
                        shutil.rmtree(path, ignore_errors=True)
                    elif os.path.isfile(path):
                        os.remove(path)
                except Exception:
                    pass
                continue

            if lower_name.startswith("__chat_run__") and lower_name.endswith(".py"):
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                except Exception:
                    pass
    except Exception:
        pass

    try:
        temp_root = tempfile.gettempdir()
        for entry in os.scandir(temp_root):
            if not entry.is_dir(follow_symlinks=False):
                continue
            if not str(entry.name or "").startswith("runpy_"):
                continue
            try:
                age_sec = now_ts - float(entry.stat().st_mtime)
            except Exception:
                age_sec = 0.0
            if age_sec < 3600:
                continue
            try:
                shutil.rmtree(entry.path, ignore_errors=True)
            except Exception:
                pass
    except Exception:
        pass


@app.on_event("startup")
def _startup_prepare_local_model_file():
    _cleanup_session_artifacts()
    if not _llama_available():
        return
    if not LLAMA_PRELOAD_ON_STARTUP:
        return
    _background_prepare_llama_model_file()


def _delayed_exit(delay_seconds: float = 0.25, code: int = 0):
    def _worker():
        try:
            import time as _time
            _time.sleep(max(0.0, float(delay_seconds)))
        except Exception:
            pass
        try:
            os._exit(code)
        except Exception:
            pass

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


def _delayed_restart(delay_seconds: float = 0.35):
    def _worker():
        try:
            import time as _time
            _time.sleep(max(0.0, float(delay_seconds)))
        except Exception:
            pass

        try:
            python_exe = sys.executable
            root_dir = os.getcwd()
            desktop_path = os.path.join(root_dir, "desktop.py")
            desktop_mode = os.getenv("APP_MODE", "").strip().lower() == "desktop"
            if desktop_mode and os.path.exists(desktop_path):
                target_cmd = [python_exe, desktop_path]
            else:
                # Avoid recursive process trees by not using --reload for app-triggered restarts.
                target_cmd = [python_exe, "-m", "uvicorn", "app:app"]
            create_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            subprocess.Popen(
                target_cmd,
                cwd=root_dir,
                creationflags=create_flags,
                close_fds=False,
            )
        except Exception:
            pass

        try:
            os._exit(0)
        except Exception:
            pass

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()

DOCUMENTS = [
    {"id": 1, "title": "FastAPI Quickstart", "content": "Learn how to build APIs with FastAPI quickly."},
    {"id": 2, "title": "Python Basics", "content": "Variables, loops, functions, and classes in Python."},
    {"id": 3, "title": "Search Relevance", "content": "Keyword matching and simple ranking strategies."},
    {"id": 4, "title": "Web Development", "content": "Frontend and backend basics for modern web apps."},
    {"id": 5, "title": "Machine Learning Intro", "content": "Supervised learning, regression, and classification."},
]

DOMAIN_HINTS = {
    "copilot": ["learn.microsoft.com", "microsoft.com"],
    "microsoft": ["learn.microsoft.com", "microsoft.com"],
    "fastapi": ["fastapi.tiangolo.com", "github.com"],
    "python": ["docs.python.org", "pypi.org"],
    "cra": ["canada.ca"],
    "canada": ["canada.ca"],
}

REGION_PRESETS = {
    "auto": "wt-wt",
    "worldwide": "wt-wt",
    "canada": "ca-en",
    "us": "us-en",
    "uk": "uk-en",
    "india": "in-en",
}

DEFAULT_AI_MODEL = os.getenv("AI_MODEL_ID", "Qwen/Qwen3-1.7B")

# ── RAM-aware model tier selection ────────────────────────────────────────────
def _get_total_ram_gb() -> float:
    """Return total system RAM in GB. Tries psutil first, falls back to ctypes/platform."""
    try:
        import psutil
        return psutil.virtual_memory().total / (1024 ** 3)
    except Exception:
        pass
    try:
        if os.name == "nt":
            import ctypes
            class _MEMSTATUS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                             ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                             ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                             ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                             ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            ms = _MEMSTATUS()
            ms.dwLength = ctypes.sizeof(_MEMSTATUS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
            return ms.ullTotalPhys / (1024 ** 3)
        else:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) / (1024 ** 2)
    except Exception:
        pass
    return 8.0  # safe fallback

_MODEL_TIERS = [
    # (model_ram_gb, repo,                                          filename,                                    n_ctx, label)
    # model_ram_gb = RAM the model itself needs — pick if usable_ram >= this
    ( 3, "Qwen/Qwen3-1.7B-GGUF",                      "Qwen3-1.7B-Q8_0.gguf",                          32768, "Qwen3 1.7B · 32K context"),
    ( 0, "TheBloke/deepseek-coder-1.3b-instruct-GGUF", "deepseek-coder-1.3b-instruct.Q4_K_M.gguf", 16384, "DeepSeek Coder 1.3B · 16K context"),
]


def _resolve_model_tier_by_filename(filename: str):
    value = str(filename or "").strip()
    return next((t for t in _MODEL_TIERS if t[2] == value), None)


def _load_persisted_model_selection(state_path: str):
    try:
        with open(state_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return {}

    if not isinstance(payload, dict):
        return {}

    filename = str(payload.get("filename", "") or "").strip()
    tier_info = _resolve_model_tier_by_filename(filename)
    if not tier_info:
        return {}

    repo = str(payload.get("repo", "") or tier_info[1]).strip() or tier_info[1]
    try:
        n_ctx = int(payload.get("n_ctx", tier_info[3]) or tier_info[3])
    except Exception:
        n_ctx = tier_info[3]

    return {
        "filename": filename,
        "repo": repo,
        "n_ctx": n_ctx,
        "label": tier_info[4],
        "source": "persisted",
    }


def _persist_model_selection(state_path: str, filename: str, repo: str, n_ctx: int):
    try:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "filename": str(filename or "").strip(),
                    "repo": str(repo or "").strip(),
                    "n_ctx": int(n_ctx or 0),
                    "updated_at": time.time(),
                },
                fh,
                ensure_ascii=False,
                indent=2,
            )
    except Exception:
        pass

def _pick_model_tier(ram_gb: float) -> dict:
    # Reserve 95% RAM for OS + other apps; use at most 5% for the model
    usable_ram = ram_gb * 0.05
    auto_cap = max(0.0, float(os.getenv("LLAMA_AUTO_MAX_MODEL_RAM_GB", "3.0") or 0.0))
    if auto_cap > 0:
        usable_ram = min(usable_ram, auto_cap)
    for model_ram, repo, filename, n_ctx, label in _MODEL_TIERS:
        if usable_ram >= model_ram:
            return {"repo": repo, "filename": filename, "n_ctx": n_ctx, "label": label, "ram_gb": round(ram_gb, 1), "usable_gb": round(usable_ram, 1)}
    return {"repo": _MODEL_TIERS[-1][1], "filename": _MODEL_TIERS[-1][2], "n_ctx": _MODEL_TIERS[-1][3], "label": _MODEL_TIERS[-1][4], "ram_gb": round(ram_gb, 1), "usable_gb": round(usable_ram, 1)}

_SYSTEM_RAM_GB = _get_total_ram_gb()
_AUTO_MODEL_TIER = _pick_model_tier(_SYSTEM_RAM_GB)
print(f"[model-select] Detected {_SYSTEM_RAM_GB:.1f}GB RAM -> auto-selected: {_AUTO_MODEL_TIER['label']} ({_AUTO_MODEL_TIER['filename']})")

# Env vars override persisted selection; otherwise use RAM-based pick
_APP_ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
LLAMA_MODELS_DIR = os.getenv("LLAMA_MODELS_DIR", os.path.join(_APP_ROOT_DIR, "models"))
_ACTIVE_MODEL_STATE_PATH = os.path.join(LLAMA_MODELS_DIR, ".active_model.json")
_ENV_LLAMA_MODEL_FILE = str(os.getenv("LLAMA_MODEL_FILE", "") or "").strip()
_ENV_LLAMA_MODEL_REPO = str(os.getenv("LLAMA_MODEL_REPO", "") or "").strip()
_PERSISTED_MODEL_SELECTION = _load_persisted_model_selection(_ACTIVE_MODEL_STATE_PATH)

if _ENV_LLAMA_MODEL_FILE:
    _env_tier = _resolve_model_tier_by_filename(_ENV_LLAMA_MODEL_FILE)
    _INITIAL_MODEL_SELECTION = {
        "filename": _ENV_LLAMA_MODEL_FILE,
        "repo": _ENV_LLAMA_MODEL_REPO or (_env_tier[1] if _env_tier else _AUTO_MODEL_TIER["repo"]),
        "n_ctx": _env_tier[3] if _env_tier else _AUTO_MODEL_TIER["n_ctx"],
        "source": "env",
    }
elif _PERSISTED_MODEL_SELECTION:
    _INITIAL_MODEL_SELECTION = _PERSISTED_MODEL_SELECTION
else:
    _INITIAL_MODEL_SELECTION = {
        "filename": _AUTO_MODEL_TIER["filename"],
        "repo": _AUTO_MODEL_TIER["repo"],
        "n_ctx": _AUTO_MODEL_TIER["n_ctx"],
        "source": "auto",
    }

LLAMA_MODEL_REPO = _INITIAL_MODEL_SELECTION["repo"]
LLAMA_MODEL_FILE = _INITIAL_MODEL_SELECTION["filename"]
_DEFAULT_N_CTX = int(_INITIAL_MODEL_SELECTION["n_ctx"])
_MODEL_SELECTION_OVERRIDDEN = str(_INITIAL_MODEL_SELECTION.get("source", "auto")) != "auto"
_MODEL_SELECTION_SOURCE = str(_INITIAL_MODEL_SELECTION.get("source", "auto") or "auto")
# ──────────────────────────────────────────────────────────────────────────────

LLAMA_N_CTX = int(os.getenv("LLAMA_N_CTX", str(_DEFAULT_N_CTX)))
LLAMA_CODE_MAX_TOKENS = int(os.getenv("LLAMA_CODE_MAX_TOKENS", "768"))
LLAMA_N_THREADS = int(os.getenv("LLAMA_N_THREADS", "4"))
LLAMA_PRELOAD_ON_STARTUP = str(os.getenv("LLAMA_PRELOAD_ON_STARTUP", "1")).strip().lower() in {"1", "true", "yes", "on"}
LLAMA_AUTO_MAX_MODEL_RAM_GB = float(os.getenv("LLAMA_AUTO_MAX_MODEL_RAM_GB", "3.0"))
CODE_CONTEXT_MAX_CHARS = int(os.getenv("CODE_CONTEXT_MAX_CHARS", "12000"))
WORKSPACE_SEARCH_MAX_FILE_BYTES = int(os.getenv("WORKSPACE_SEARCH_MAX_FILE_BYTES", "300000"))
WORKSPACE_SEARCH_MAX_FILES = int(os.getenv("WORKSPACE_SEARCH_MAX_FILES", "1200"))
G4F_MODEL_ID = os.getenv("G4F_MODEL_ID", "gpt-4o-mini")
G4F_PROVIDER_NAME = os.getenv("G4F_PROVIDER", "")
_DEFAULT_BOT_TOOLS_DIR = os.path.join(tempfile.gettempdir(), "search_engine_bot_tools")
BOT_TOOLS_DIR = os.getenv("BOT_TOOLS_DIR", _DEFAULT_BOT_TOOLS_DIR)
BOT_TOOLS_RUNTIME_DIR = os.path.join(BOT_TOOLS_DIR, "runtime")
BOT_TOOLS_IO_DIR = os.path.join(BOT_TOOLS_DIR, "io")
BOT_TOOL_TIMEOUT_SEC = int(os.getenv("BOT_TOOL_TIMEOUT_SEC", "90"))
BOT_TOOL_MAX_OUTPUT_CHARS = int(os.getenv("BOT_TOOL_MAX_OUTPUT_CHARS", "8000"))
G4F_PROVIDER_CHAIN = os.getenv("G4F_PROVIDER_CHAIN", "")
G4F_MODEL_CANDIDATES = os.getenv(
    "G4F_MODEL_CANDIDATES",
    "gpt-4o-mini,openai-fast,gpt-4.1-nano,deepseek-chat",
)
G4F_FREE_MODEL_ALLOWLIST = os.getenv(
    "G4F_FREE_MODEL_ALLOWLIST",
    "deepseek-v3",
)
OCR_API_BASE = os.getenv("OCR_API_BASE", "https://harikirankumar-searchmyfiles.hf.space").strip().rstrip("/")
OCR_API_KEY = os.getenv("OCR_API_KEY", "").strip()
_LLM_LOCK = threading.Lock()
_LLM_GENERATE_LOCK = threading.Lock()
_LLAMA_MODEL = None
_LLAMA_LOAD_ERROR = ""
_LLAMA_STATUS = {
    "state": "idle",
    "message": "Ready",
    "updated_at": 0.0,
}

_WORKSPACE_IGNORED_DIRS = {
    ".git", ".hg", ".svn", ".venv", ".venv-1", "venv", "env",
    "node_modules", "__pycache__", "dist", "build",
}

_PC_IGNORED_DIRS = {
    "$recycle.bin", "system volume information", "windows", "program files", "program files (x86)",
    "programdata", ".git", ".hg", ".svn", ".venv", ".venv-1", "venv", "env",
    "node_modules", "__pycache__", "dist", "build",
    ".cache", "appdata", "application data", "local settings", ".vscode",
}

_WORKSPACE_ALLOWED_EXTENSIONS = {
    ".py", ".pyw", ".js", ".jsx", ".ts", ".tsx", ".json", ".md", ".txt",
    ".html", ".htm", ".css", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env",
    ".sql", ".sh", ".bash", ".zsh", ".bat", ".cmd", ".ps1", ".java", ".cs", ".go", ".rs",
    ".php", ".rb", ".swift", ".kt", ".kts", ".c", ".h", ".cpp", ".hpp", ".ipynb",
}

_CONTEXT_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "for", "from",
    "how", "i", "in", "is", "it", "its", "me", "my", "of", "on", "or", "our",
    "please", "project", "repo", "repository", "tell", "that", "the", "this", "to",
    "us", "what", "which", "who", "why", "with", "you", "your", "about",
}


def _set_llama_status(state: str, message: str = ""):
    _LLAMA_STATUS["state"] = str(state or "idle").strip().lower()
    _LLAMA_STATUS["message"] = str(message or "").strip()
    _LLAMA_STATUS["updated_at"] = time.time()


def _get_llama_status():
    return {
        "state": _LLAMA_STATUS.get("state", "idle"),
        "message": _LLAMA_STATUS.get("message", ""),
        "updated_at": _LLAMA_STATUS.get("updated_at", 0.0),
    }


def _split_csv_values(raw: str):
    parts = re.split(r"[\s,;]+", str(raw or "").strip())
    out = []
    seen = set()
    for item in parts:
        value = str(item or "").strip()
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _resolve_g4f_model_candidates():
    models = []
    configured = str(G4F_MODEL_ID or "").strip()
    if configured:
        models.extend(_split_csv_values(configured))
    models.extend(_split_csv_values(G4F_MODEL_CANDIDATES))

    normalized = []
    seen = set()
    for model in models:
        value = str(model or "").strip()
        if "/" in value:
            value = "openai-fast"
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            normalized.append(value)
    if not normalized:
        normalized.append("openai-fast")
    return normalized


def _resolve_g4f_provider_candidates():
    preferred_names = []
    preferred = str(G4F_PROVIDER_NAME or "").strip()
    if preferred:
        preferred_names.extend(_split_csv_values(preferred))
    preferred_names.extend(_split_csv_values(G4F_PROVIDER_CHAIN))

    providers = []
    seen = set()
    if _g4f is not None and hasattr(_g4f, "Provider"):
        for name in preferred_names:
            provider_obj = getattr(_g4f.Provider, name, None)
            if provider_obj is None:
                continue
            key = str(name).strip().lower()
            if key in seen:
                continue
            seen.add(key)
            providers.append((name, provider_obj))

    providers.append(("auto", None))
    return providers


def _normalize_provider_name(provider_name: str) -> str:
    value = _compress_spaces(str(provider_name or "")).strip()
    return value[:120]


def _resolve_single_g4f_provider(provider_name: str):
    name = _normalize_provider_name(provider_name)
    if not name:
        return None
    if _g4f is None or not hasattr(_g4f, "Provider"):
        return None
    provider_obj = getattr(_g4f.Provider, name, None)
    if provider_obj is None:
        return None
    return (name, provider_obj)


def _list_g4f_provider_catalog(max_items: int = 160):
    if not _G4F_OK or _g4f is None or not hasattr(_g4f, "Provider"):
        return []

    providers = []
    seen = set()
    for name in dir(_g4f.Provider):
        if str(name).startswith("_"):
            continue
        provider_obj = getattr(_g4f.Provider, name, None)
        if provider_obj is None:
            continue
        if not (
            callable(provider_obj)
            or hasattr(provider_obj, "create_completion")
            or hasattr(provider_obj, "working")
        ):
            continue

        value = str(name or "").strip()
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            providers.append(value)

    providers.sort(key=lambda item: item.lower())
    return providers[: max(1, int(max_items))]


def _list_g4f_model_catalog(max_items: int = 220):
    if not _G4F_OK or _g4f is None or not hasattr(_g4f, "models"):
        return []

    out = []
    seen = set()
    g4f_models = _g4f.models

    for attr_name in dir(g4f_models):
        name = str(attr_name or "").strip()
        if not name or name.startswith("_"):
            continue
        if name.lower() in {"modelutils", "modelutilsconverter"}:
            continue

        model_obj = getattr(g4f_models, name, None)
        candidates = []
        if isinstance(model_obj, str):
            candidates.append(model_obj)
        else:
            model_name = getattr(model_obj, "name", None)
            if isinstance(model_name, str) and model_name.strip():
                candidates.append(model_name)

        for candidate in candidates:
            value = str(candidate or "").strip()
            key = value.lower()
            if value and key not in seen:
                seen.add(key)
                out.append(value)

    out.sort(key=lambda item: item.lower())
    return out[: max(1, int(max_items))]


def _resolve_g4f_free_model_options():
    available_models = _list_g4f_model_catalog(max_items=260)
    configured_models = _resolve_g4f_model_candidates()
    allowed_models = _split_csv_values(G4F_FREE_MODEL_ALLOWLIST)

    preferred = []
    preferred.extend(allowed_models)
    preferred.extend(configured_models)

    available_lookup = {str(item or "").strip().lower(): str(item or "").strip() for item in available_models}
    out = []
    seen = set()

    for item in preferred:
        value = str(item or "").strip()
        if not value:
            continue
        resolved = available_lookup.get(value.lower(), value)
        key = resolved.lower()
        if key not in seen:
            seen.add(key)
            out.append(resolved)

    return out


def _score_document(query: str, doc: dict) -> int:
    haystack = f"{doc.get('title', '')} {doc.get('content', '')}".lower()
    terms = [term for term in query.lower().split() if term.strip()]
    return sum(1 for term in terms if term in haystack)


def _normalize_query(query: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(query or "")).strip()
    cleaned = re.sub(r"^(hi|hello|hey)\b[\s,!.:-]*", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(
        r"\b(what do you know about|can you explain|tell me about|i need help with|please help with)\b",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ?.-")
    return cleaned


def _preferred_domains(query: str):
    lowered = query.lower()
    preferred = []
    for token, domains in DOMAIN_HINTS.items():
        if token in lowered:
            preferred.extend(domains)
    deduped = []
    seen = set()
    for domain in preferred:
        key = domain.lower().strip()
        if key and key not in seen:
            seen.add(key)
            deduped.append(key)
    return deduped


def _query_candidates(query: str):
    base = _normalize_query(query)
    candidates = [query, base]
    for domain in _preferred_domains(base or query):
        if base:
            candidates.append(f"site:{domain} {base}")
    out = []
    seen = set()
    for item in candidates:
        value = re.sub(r"\s+", " ", str(item or "")).strip()
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _score_web_result(query: str, title: str, snippet: str, href: str, rank: int, preferred_domains):
    text = f"{title} {snippet} {href}".lower()
    terms = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) >= 3][:8]
    term_hits = sum(1 for term in terms if term in text)
    domain = urlparse(href).netloc.lower()
    domain_boost = 0
    if any(pref in domain for pref in preferred_domains):
        domain_boost += 8
    rank_boost = max(1, 10 - rank)
    return (term_hits * 3) + domain_boost + rank_boost


def _paginate(items, page: int, limit: int):
    total = len(items)
    start = (page - 1) * limit
    end = start + limit
    chunk = items[start:end]
    has_next = end < total
    return chunk, total, has_next


def _resolve_region_code(region_alias: str) -> str:
    alias = str(region_alias or "auto").strip().lower()
    if alias in REGION_PRESETS:
        return REGION_PRESETS[alias]
    if alias in REGION_PRESETS.values():
        return alias
    return REGION_PRESETS["auto"]


def _region_candidates(region_alias: str):
    primary = _resolve_region_code(region_alias)
    candidates = [primary, "wt-wt", "us-en", "ca-en"]
    out = []
    seen = set()
    for code in candidates:
        key = str(code or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _sentence_summary(text: str, max_sentences: int = 3, max_chars: int = 420) -> str:
    cleaned = " ".join(str(text or "").split()).strip()
    if not cleaned:
        return ""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", cleaned) if len(s.strip()) > 35]
    picked = []
    total_chars = 0
    for sentence in sentences:
        if len(picked) >= max_sentences:
            break
        if total_chars + len(sentence) > max_chars and picked:
            break
        picked.append(sentence)
        total_chars += len(sentence)
    if not picked:
        short = cleaned[:max_chars].rsplit(" ", 1)[0].strip()
        return f"{short}..." if short else ""
    return " ".join(picked)


def _sumy_summarize(text: str, n_sentences: int = 4) -> list:
    """Try TextRank → LexRank → LSA in order; return list of sentence strings."""
    if not _SUMY_OK or not text or len(text.split()) < 40:
        return []
    for cls in (TextRankSummarizer, LexRankSummarizer, LsaSummarizer):
        try:
            parser = PlaintextParser.from_string(text, SumyTokenizer("english"))
            sentences = [str(s) for s in cls()(parser.document, n_sentences)]
            if sentences:
                return sentences
        except Exception:
            continue
    return []


def _normalize_line_for_dedupe(value: str) -> str:
    lowered = str(value or "").lower()
    lowered = re.sub(r"\bstarting at\b", "", lowered)
    lowered = re.sub(r"\d+(\.\d+)?", "#", lowered)
    lowered = re.sub(r"[^a-z0-9\s]", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered).strip()
    return lowered


def _clean_extracted_lines(text: str, max_items: int = 160):
    raw_parts = re.split(r"[\r\n]+", str(text or ""))
    if len(raw_parts) <= 1:
        raw_parts = re.split(r"\s+-\s+|\s+•\s+|(?<=[.!?])\s+", str(text or ""))

    blocked = {
        "starting at",
        "learn more",
        "shop now",
        "buy now",
        "read more",
        "table of contents",
        "overview",
    }

    cleaned = []
    seen = set()
    for part in raw_parts:
        line = " ".join(str(part or "").split()).strip(" -•\t")
        if len(line) < 20:
            continue
        low = line.lower().strip(" .")
        if not low:
            continue
        if low in blocked:
            continue
        if low.startswith("faq") or low.startswith("faqs"):
            continue
        if low.startswith("learn about:") or low.startswith("related:"):
            continue
        if "table of contents" in low:
            continue
        if low.count("|") >= 2:
            continue
        if re.search(r"\b(share|tweet|follow us|advertisement|sponsored)\b", low):
            continue

        norm = _normalize_line_for_dedupe(low)
        if len(norm) < 12 or norm in seen:
            continue
        seen.add(norm)
        cleaned.append(line)
        if len(cleaned) >= max_items:
            break
    return cleaned


def _is_list_heavy_text(text: str) -> bool:
    value = str(text or "")
    lines = [ln.strip() for ln in re.split(r"[\r\n]+", value) if ln.strip()]
    if not lines:
        return False
    bullet_like = sum(1 for ln in lines if ln.startswith(("-", "•", "*")) or re.match(r"^\d+[.)]\s", ln))
    ratio = bullet_like / max(1, len(lines))
    return ratio >= 0.25 or len(lines) >= 18


def _extract_feature_highlights(lines):
    buckets = {
        "cpu": r"core\s+ultra|intel",
        "ai": r"copilot\+|on-device\s+ai|ai\s+pc",
        "os": r"windows\s+11|ubuntu|linux|no operating system",
        "display": r"16:10|qhd|oled|mini\s*led|fhd|16\s*[\"”]|14\s*[\"”]",
        "battery": r"up to\s*\d+\s*hrs|battery",
        "weight": r"weight\s*\d|lb|kg|slimmer|lightweight|lightest",
        "durability": r"mil-std|durable|aluminum|magnesium",
        "graphics": r"nvidia|rtx|geforce|intel\s+graphics",
        "form": r"2-in-1|numeric keypad|number pad|keyboard",
    }

    counts = Counter()
    samples = {}
    for line in lines:
        low = line.lower()
        for key, pattern in buckets.items():
            if re.search(pattern, low):
                counts[key] += 1
                samples.setdefault(key, line)

    label = {
        "cpu": "Intel Core Ultra CPU options are the main positioning.",
        "ai": "Multiple models emphasize AI PC and Copilot+ capabilities.",
        "os": "OS options include Windows 11 variants, with some models offering Ubuntu/Linux.",
        "display": "Display options span 14\" and 16\" classes with 16:10, QHD+, Mini LED, and OLED variants.",
        "battery": "Battery claims focus on all-day usage with mentions up to roughly 20-24 hours.",
        "weight": "Portability is highlighted through slimmer builds and relatively low starting weights.",
        "durability": "Business-oriented builds mention MIL-STD durability and metal chassis options.",
        "graphics": "Graphics options range from integrated Intel graphics to NVIDIA RTX/GeForce configurations.",
        "form": "Configuration choices include numeric keypad workflows and selected 2-in-1 form factors.",
    }

    top_keys = [key for key, _ in counts.most_common(6)]
    highlights = [label[key] for key in top_keys if key in label]
    return highlights


def _agentic_explanation(text: str):
    cleaned = " ".join(str(text or "").split()).strip()
    if not cleaned:
        return {
            "summary": "",
            "explanation": "No extractable content was found on this page.",
            "highlights": [],
            "cleaned_points": [],
        }

    lines = _clean_extracted_lines(text)
    lower_all = cleaned.lower()

    if _is_list_heavy_text(text):
        highlights = _extract_feature_highlights(lines)
        if "dell" in lower_all and "core ultra" in lower_all:
            overview = "This page is a Dell lineup overview for Intel Core Ultra laptops and 2-in-1 systems."
        elif "core ultra" in lower_all:
            overview = "This page compares multiple Intel Core Ultra laptop configurations."
        else:
            overview = "This page lists multiple device configurations with repeated spec blocks."

        if highlights:
            explanation = " ".join([overview, "Key takeaways:"] + highlights[:5])
        else:
            fallback_lines = lines[:4]
            joined = " ".join(fallback_lines)
            explanation = _sentence_summary(joined, max_sentences=3, max_chars=650)

        concise = " ".join(explanation.split())[:700].strip()
        return {
            "summary": concise,
            "explanation": concise,
            "highlights": highlights[:6],
            "cleaned_points": lines[:20],
        }

    normal_summary = _sentence_summary(cleaned, max_sentences=4, max_chars=700)
    return {
        "summary": normal_summary,
        "explanation": normal_summary,
        "highlights": [],
        "cleaned_points": lines[:20],
    }


def _summarize_text(text: str, max_sentences: int = 4, max_chars: int = 700) -> str:
    if _is_list_heavy_text(text):
        return _agentic_explanation(text).get("summary", "")
    # try sumy first for real article-style text
    sumy_sentences = _sumy_summarize(text, n_sentences=max_sentences)
    if sumy_sentences:
        return " ".join(sumy_sentences)
    return _sentence_summary(text, max_sentences=max_sentences, max_chars=max_chars)


def _fetch_html(url: str) -> str:
    req = UrlRequest(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/127.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", errors="ignore")


def _extract_with_trafilatura(url: str) -> str:
    try:
        dl = trafilatura.fetch_url(url)
        if dl:
            text = trafilatura.extract(
                dl,
                include_comments=False,
                include_tables=False,
                favor_precision=True,
            ) or ""
            return text.strip()
    except Exception:
        pass
    return ""


def _extract_with_readability(html: str) -> str:
    if not _READABILITY_OK or not _BS4_OK:
        return ""
    try:
        doc = ReadabilityDocument(html)
        article_html = doc.summary(html_partial=True)
        soup = BeautifulSoup(article_html, "lxml")
        text = soup.get_text(separator="\n")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return "\n".join(lines)
    except Exception:
        return ""


def _extract_with_beautifulsoup(html: str) -> str:
    if not _BS4_OK:
        return ""
    try:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        paras = [p.get_text(separator=" ").strip() for p in soup.find_all("p")]
        return "\n".join(p for p in paras if len(p) > 40)
    except Exception:
        return ""


def _extract_page_text(url: str) -> str:
    link = str(url or "").strip()
    if not link:
        return ""

    # 1. trafilatura (precision mode)
    t = _extract_with_trafilatura(link)
    if t and not _is_low_quality_extraction(t):
        return t

    # 2. fetch HTML once for readability + bs4 fallbacks
    html = ""
    try:
        html = _fetch_html(link)
    except Exception:
        pass

    if html:
        r = _extract_with_readability(html)
        if r and not _is_low_quality_extraction(r):
            return r

        b = _extract_with_beautifulsoup(html)
        if b and not _is_low_quality_extraction(b):
            return b

    return ""


def _is_http_url(url: str) -> bool:
    value = str(url or "").strip().lower()
    return value.startswith("http://") or value.startswith("https://")


def _is_low_quality_extraction(text: str) -> bool:
    cleaned = " ".join(str(text or "").split()).strip().lower()
    if len(cleaned) < 260:
        return True

    noisy_phrases = [
        "continue shopping",
        "conditions of use",
        "privacy policy",
        "enable cookies",
        "robot check",
        "captcha",
        "access denied",
        "sign in",
        "log in",
        "subscribe now",
        "accept all cookies",
        "table of contents",
        "learn about:",
        "google news - news about",
    ]
    noisy_hits = sum(1 for phrase in noisy_phrases if phrase in cleaned)
    if noisy_hits >= 2:
        return True

    useful_tokens = re.findall(r"[a-z0-9]+", cleaned)
    if len(useful_tokens) < 45:
        return True

    return False


def _compress_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _ensure_bot_tools_dirs():
    os.makedirs(BOT_TOOLS_RUNTIME_DIR, exist_ok=True)
    os.makedirs(BOT_TOOLS_IO_DIR, exist_ok=True)


def _sanitize_tool_name(name: str) -> str:
    value = re.sub(r"[^a-z0-9_\-]", "_", str(name or "").strip().lower())
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "tool"


def _write_text_if_changed(file_path: str, content: str) -> bool:
    next_text = str(content or "")
    try:
        with open(file_path, "r", encoding="utf-8") as fh:
            current = fh.read()
        if current == next_text:
            return False
    except Exception:
        pass
    with open(file_path, "w", encoding="utf-8") as fh:
        fh.write(next_text)
    return True


def _tool_script_python_runner() -> str:
    return textwrap.dedent(
        """
        import json
        import os
        import subprocess
        import sys

        def _main(input_path: str, output_path: str):
            with open(input_path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            code = str(payload.get("code", "") or "")
            timeout = int(payload.get("timeout", 60) or 60)
            timeout = max(1, min(timeout, 300))

            if not code.strip():
                result = {"ok": False, "error": "No Python code provided.", "stdout": "", "stderr": "", "exit_code": -1}
            else:
                try:
                    run = subprocess.run(
                        [sys.executable, "-u", "-c", code],
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        cwd=payload.get("cwd") or os.getcwd(),
                    )
                    result = {
                        "ok": run.returncode == 0,
                        "stdout": run.stdout,
                        "stderr": run.stderr,
                        "exit_code": run.returncode,
                    }
                except subprocess.TimeoutExpired as exc:
                    result = {
                        "ok": False,
                        "stdout": exc.stdout or "",
                        "stderr": (exc.stderr or "") + "\\nExecution timed out.",
                        "exit_code": -1,
                    }
                except Exception as exc:
                    result = {"ok": False, "stdout": "", "stderr": str(exc), "exit_code": -1}

            with open(output_path, "w", encoding="utf-8") as fh:
                json.dump(result, fh, ensure_ascii=False)

        if __name__ == "__main__":
            _main(sys.argv[1], sys.argv[2])
        """
    ).strip() + "\n"


def _tool_script_package_installer() -> str:
    return textwrap.dedent(
        """
        import json
        import subprocess
        import sys

        def _main(input_path: str, output_path: str):
            with open(input_path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)

            packages = payload.get("packages", [])
            if isinstance(packages, str):
                packages = [packages]
            packages = [str(p).strip() for p in packages if str(p).strip()]
            timeout = int(payload.get("timeout", 180) or 180)
            timeout = max(10, min(timeout, 900))
            upgrade = bool(payload.get("upgrade", False))

            if not packages:
                result = {"ok": False, "error": "No packages provided.", "stdout": "", "stderr": "", "exit_code": -1}
            else:
                cmd = [sys.executable, "-m", "pip", "install"]
                if upgrade:
                    cmd.append("--upgrade")
                cmd.extend(packages)
                try:
                    run = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
                    result = {
                        "ok": run.returncode == 0,
                        "stdout": run.stdout,
                        "stderr": run.stderr,
                        "exit_code": run.returncode,
                        "packages": packages,
                    }
                except subprocess.TimeoutExpired as exc:
                    result = {
                        "ok": False,
                        "stdout": exc.stdout or "",
                        "stderr": (exc.stderr or "") + "\\nPackage installation timed out.",
                        "exit_code": -1,
                        "packages": packages,
                    }
                except Exception as exc:
                    result = {
                        "ok": False,
                        "stdout": "",
                        "stderr": str(exc),
                        "exit_code": -1,
                        "packages": packages,
                    }

            with open(output_path, "w", encoding="utf-8") as fh:
                json.dump(result, fh, ensure_ascii=False)

        if __name__ == "__main__":
            _main(sys.argv[1], sys.argv[2])
        """
    ).strip() + "\n"


def _tool_script_web_search() -> str:
    return textwrap.dedent(
        """
        import json
        import re
        import sys
        from ddgs import DDGS

        def _is_url(text: str) -> bool:
            t = str(text or "").strip().lower()
            return t.startswith("http://") or t.startswith("https://")

        def _main(input_path: str, output_path: str):
            with open(input_path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)

            query = str(payload.get("query", "") or "").strip()
            limit = int(payload.get("limit", 5) or 5)
            limit = max(1, min(limit, 15))
            region = str(payload.get("region", "wt-wt") or "wt-wt").strip() or "wt-wt"

            if not query:
                result = {"ok": False, "error": "No query provided.", "results": []}
            elif _is_url(query):
                result = {
                    "ok": True,
                    "results": [{"title": query, "url": query, "content": "User-provided URL", "source": "url"}],
                }
            else:
                try:
                    rows = list(DDGS().text(query, region=region, safesearch="moderate", max_results=limit))
                    out = []
                    seen = set()
                    for idx, row in enumerate(rows, start=1):
                        href = str(row.get("href", "") or "").strip()
                        if not href or href.lower() in seen:
                            continue
                        seen.add(href.lower())
                        out.append({
                            "id": idx,
                            "title": str(row.get("title", "") or "").strip(),
                            "url": href,
                            "content": str(row.get("body", "") or "").strip(),
                            "source": "duckduckgo",
                        })
                        if len(out) >= limit:
                            break
                    result = {"ok": True, "results": out}
                except Exception as exc:
                    result = {"ok": False, "error": str(exc), "results": []}

            with open(output_path, "w", encoding="utf-8") as fh:
                json.dump(result, fh, ensure_ascii=False)

        if __name__ == "__main__":
            _main(sys.argv[1], sys.argv[2])
        """
    ).strip() + "\n"


def _tool_script_scrape_url() -> str:
    return textwrap.dedent(
        """
        import json
        import re
        import sys
        import requests

        try:
            import trafilatura
            _TRAFILATURA_OK = True
        except Exception:
            _TRAFILATURA_OK = False

        try:
            from bs4 import BeautifulSoup
            _BS4_OK = True
        except Exception:
            _BS4_OK = False

        try:
            from playwright.sync_api import sync_playwright
            _PLAYWRIGHT_OK = True
        except Exception:
            _PLAYWRIGHT_OK = False

        def _is_http_url(text: str) -> bool:
            t = str(text or "").strip().lower()
            return t.startswith("http://") or t.startswith("https://")

        def _clean_text(text: str) -> str:
            value = str(text or "")
            value = re.sub(r"\\s+", " ", value).strip()
            return value

        def _is_low_quality(text: str) -> bool:
            cleaned = _clean_text(text).lower()
            if len(cleaned) < 260:
                return True
            noisy_phrases = [
                "continue shopping", "privacy policy", "enable cookies", "captcha",
                "access denied", "sign in", "log in", "accept all cookies",
            ]
            noisy_hits = sum(1 for phrase in noisy_phrases if phrase in cleaned)
            return noisy_hits >= 2

        def _extract_with_trafilatura(html_or_downloaded: str) -> str:
            if not _TRAFILATURA_OK:
                return ""
            try:
                text = trafilatura.extract(
                    html_or_downloaded,
                    include_comments=False,
                    include_tables=False,
                    favor_precision=True,
                ) or ""
                return text.strip()
            except Exception:
                return ""

        def _extract_with_bs4(html: str):
            if not _BS4_OK:
                return "", ""
            try:
                soup = BeautifulSoup(html, "lxml")
                for tag in soup(["script", "style", "nav", "footer", "aside"]):
                    tag.decompose()
                title = ""
                if soup.title and soup.title.string:
                    title = str(soup.title.string).strip()
                paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
                text = "\\n".join([p for p in paras if len(p) > 40]).strip()
                full_text = soup.get_text("\n", strip=True)
                if not text or len(_clean_text(full_text)) > len(_clean_text(text)):
                    text = full_text
                text = re.sub(r"\\n{3,}", "\\n\\n", text)
                return title, text
            except Exception:
                return "", ""

        def _render_with_playwright(url: str, timeout_ms: int = 60000) -> str:
            if not _PLAYWRIGHT_OK:
                return ""
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    page = browser.new_page()
                    page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                    html = page.content()
                    browser.close()
                return html
            except Exception:
                return ""

        def _main(input_path: str, output_path: str):
            with open(input_path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)

            url = str(payload.get("url", "") or "").strip()
            timeout = int(payload.get("timeout", 20) or 20)
            timeout = max(5, min(timeout, 90))
            max_chars = int(payload.get("max_chars", 6000) or 6000)
            max_chars = max(500, min(max_chars, 40000))

            if not _is_http_url(url):
                result = {"ok": False, "error": "Invalid or missing URL.", "url": url, "text": ""}
            else:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
                }
                title = ""
                text = ""
                render_used = False
                try:
                    resp = requests.get(url, headers=headers, timeout=timeout)
                    resp.raise_for_status()
                    html = resp.text

                    t = _extract_with_trafilatura(html)
                    if t and not _is_low_quality(t):
                        text = t
                    else:
                        title, b = _extract_with_bs4(html)
                        if b and not _is_low_quality(b):
                            text = b

                    if (not text or _is_low_quality(text)) and _PLAYWRIGHT_OK:
                        rendered_html = _render_with_playwright(url)
                        if rendered_html:
                            render_used = True
                            t2 = _extract_with_trafilatura(rendered_html)
                            if t2 and not _is_low_quality(t2):
                                text = t2
                            else:
                                title2, b2 = _extract_with_bs4(rendered_html)
                                if title2 and not title:
                                    title = title2
                                if b2 and not _is_low_quality(b2):
                                    text = b2

                    result = {
                        "ok": bool(text),
                        "url": url,
                        "title": title,
                        "text": str(text or "")[:max_chars],
                        "text_length": len(str(text or "")),
                        "render_used": render_used,
                    }
                    if not result["ok"]:
                        result["error"] = "Could not extract meaningful content."
                except Exception as exc:
                    result = {"ok": False, "error": str(exc), "url": url, "text": ""}

            with open(output_path, "w", encoding="utf-8") as fh:
                json.dump(result, fh, ensure_ascii=False)

        if __name__ == "__main__":
            _main(sys.argv[1], sys.argv[2])
        """
    ).strip() + "\n"


def _tool_script_fetch_webpage() -> str:
    return textwrap.dedent(
        """
        import json
        import re
        import sys
        import requests

        try:
            import trafilatura
            _TRAFILATURA_OK = True
        except Exception:
            _TRAFILATURA_OK = False

        try:
            from bs4 import BeautifulSoup
            _BS4_OK = True
        except Exception:
            _BS4_OK = False

        try:
            from playwright.sync_api import sync_playwright
            _PLAYWRIGHT_OK = True
        except Exception:
            _PLAYWRIGHT_OK = False

        def _is_http_url(text: str) -> bool:
            t = str(text or "").strip().lower()
            return t.startswith("http://") or t.startswith("https://")

        def _clean_text(text: str) -> str:
            return re.sub(r"\\s+", " ", str(text or "")).strip()

        def _extract_with_trafilatura(html: str) -> str:
            if not _TRAFILATURA_OK:
                return ""
            try:
                return (trafilatura.extract(html, include_comments=False, include_tables=False, favor_precision=True) or "").strip()
            except Exception:
                return ""

        def _extract_with_bs4(html: str):
            if not _BS4_OK:
                return "", ""
            try:
                soup = BeautifulSoup(html, "lxml")
                for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                    tag.decompose()
                title = ""
                if soup.title and soup.title.string:
                    title = str(soup.title.string).strip()
                paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
                text = "\\n".join([p for p in paras if len(p) > 40]).strip()
                if not text:
                    text = soup.get_text("\\n", strip=True)
                text = re.sub(r"\\n{3,}", "\\n\\n", text)
                return title, text
            except Exception:
                return "", ""

        def _render_with_playwright(url: str, timeout_ms: int = 60000) -> str:
            if not _PLAYWRIGHT_OK:
                return ""
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    page = browser.new_page()
                    page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                    html = page.content()
                    browser.close()
                return html
            except Exception:
                return ""

        def _score_sentence(sentence: str, terms):
            low = sentence.lower()
            return sum(low.count(t) for t in terms)

        def _text_query_score(text: str, terms):
            low = str(text or "").lower()
            return sum(low.count(t) for t in terms)

        def _extract_relevant_snippets(text: str, query: str, top_k: int = 3):
            base = str(text or "").strip()
            if not base:
                return []
            terms = [t.lower() for t in re.findall(r"[a-zA-Z0-9_]+", str(query or "")) if len(t) > 2]
            priority_terms = [t for t in terms if "_" in t or len(t) >= 10]
            if priority_terms:
                direct_hits = []
                low = base.lower()
                for pt in priority_terms:
                    idx = low.find(pt)
                    if idx >= 0:
                        start = max(0, idx - 140)
                        end = min(len(base), idx + 360)
                        direct_hits.append(base[start:end].strip())
                if direct_hits:
                    out = []
                    seen = set()
                    for chunk in direct_hits:
                        key = _clean_text(chunk).lower()
                        if key in seen:
                            continue
                        seen.add(key)
                        out.append(chunk)
                        if len(out) >= max(1, top_k):
                            return out
            parts = re.split(r"(?<=[.!?])\\s+|\\n+", base)
            candidates = [p.strip() for p in parts if len(p.strip()) >= 30]
            if not candidates:
                return [base[:400]]
            if not terms:
                return candidates[:top_k]
            ranked = sorted(candidates, key=lambda s: _score_sentence(s, terms), reverse=True)
            if priority_terms:
                preferred = [c for c in candidates if any(pt in c.lower() for pt in priority_terms)]
                if preferred:
                    ranked = preferred + [c for c in ranked if c not in preferred]
            out = []
            seen = set()
            for chunk in ranked:
                key = _clean_text(chunk).lower()
                if key in seen:
                    continue
                seen.add(key)
                if _score_sentence(chunk, terms) <= 0 and out:
                    continue
                out.append(chunk[:500])
                if len(out) >= max(1, top_k):
                    break
            return out or candidates[:top_k]

        def _fetch_one(url: str, timeout: int = 25, max_chars: int = 12000):
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
            }
            try:
                resp = requests.get(url, headers=headers, timeout=timeout)
                resp.raise_for_status()
                html = resp.text

                text = _extract_with_trafilatura(html)
                title, bs4_text = _extract_with_bs4(html)
                if bs4_text and len(_clean_text(bs4_text)) > len(_clean_text(text)):
                    text = bs4_text
                if (not text or len(_clean_text(text)) < 220) and bs4_text:
                    text = bs4_text

                render_used = False
                if (not text or len(_clean_text(text)) < 220) and _PLAYWRIGHT_OK:
                    rendered_html = _render_with_playwright(url)
                    if rendered_html:
                        render_used = True
                        text2 = _extract_with_trafilatura(rendered_html)
                        title2, bs4_text2 = _extract_with_bs4(rendered_html)
                        if title2 and not title:
                            title = title2
                        if bs4_text2 and len(_clean_text(bs4_text2)) > len(_clean_text(text2)):
                            text2 = bs4_text2
                        text = text2 if len(_clean_text(text2)) >= len(_clean_text(text)) else text
                        if len(_clean_text(text)) < 220 and bs4_text2:
                            text = bs4_text2

                text = str(text or "")
                return {
                    "ok": bool(_clean_text(text)),
                    "url": url,
                    "title": title,
                    "text": text,
                    "text_length": len(text),
                    "render_used": render_used,
                }
            except Exception as exc:
                return {"ok": False, "url": url, "title": "", "text": "", "text_length": 0, "error": str(exc)}

        def _main(input_path: str, output_path: str):
            with open(input_path, "r", encoding="utf-8-sig") as fh:
                payload = json.load(fh)

            urls = payload.get("urls", [])
            if isinstance(urls, str):
                urls = [urls]
            urls = [str(u or "").strip() for u in urls if _is_http_url(u)]
            query = str(payload.get("query", "") or "").strip()
            timeout = int(payload.get("timeout", 25) or 25)
            timeout = max(5, min(timeout, 90))
            top_k = int(payload.get("top_k", 3) or 3)
            top_k = max(1, min(top_k, 6))
            max_chars = int(payload.get("max_chars", 12000) or 12000)
            max_chars = max(1000, min(max_chars, 50000))

            if not urls:
                result = {"ok": False, "error": "No valid URLs provided.", "query": query, "results": []}
            else:
                out = []
                query_terms = [t.lower() for t in re.findall(r"[a-zA-Z0-9_]+", query) if len(t) > 2]
                for link in urls[:5]:
                    row = _fetch_one(link, timeout=timeout, max_chars=max_chars)
                    full_text = row.get("text", "")
                    if query_terms and row.get("ok"):
                        text_score = _text_query_score(full_text, query_terms)
                        if _BS4_OK:
                            try:
                                resp2 = requests.get(link, headers={
                                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
                                }, timeout=timeout)
                                resp2.raise_for_status()
                                _, bs4_text_retry = _extract_with_bs4(resp2.text)
                                if _text_query_score(bs4_text_retry, query_terms) > text_score:
                                    full_text = bs4_text_retry
                            except Exception:
                                pass
                    snippets = _extract_relevant_snippets(full_text, query=query, top_k=top_k)
                    row["snippets"] = snippets
                    row["text"] = full_text[:max_chars]
                    row["text_length"] = len(row["text"])
                    out.append(row)
                result = {"ok": True, "query": query, "count": len(out), "results": out}

            with open(output_path, "w", encoding="utf-8") as fh:
                json.dump(result, fh, ensure_ascii=False)

        if __name__ == "__main__":
            _main(sys.argv[1], sys.argv[2])
        """
    ).strip() + "\n"


def _bot_tool_registry():
    return {
        "python_runner": {
            "filename": "python_runner.py",
            "builder": _tool_script_python_runner,
            "description": "Run Python code and capture stdout/stderr.",
        },
        "package_installer": {
            "filename": "package_installer.py",
            "builder": _tool_script_package_installer,
            "description": "Install Python packages via pip.",
        },
        "web_search": {
            "filename": "web_search.py",
            "builder": _tool_script_web_search,
            "description": "Search web by keyword or URL.",
        },
        "scrape_url": {
            "filename": "scrape_url.py",
            "builder": _tool_script_scrape_url,
            "description": "Scrape readable text from a URL.",
        },
        "fetch_webpage": {
            "filename": "fetch_webpage.py",
            "builder": _tool_script_fetch_webpage,
            "description": "Fetch one or more webpages and return query-relevant snippets.",
        },
    }


def _truncate_tool_output(text: str, limit: int = BOT_TOOL_MAX_OUTPUT_CHARS) -> str:
    value = str(text or "")
    lim = max(500, int(limit or BOT_TOOL_MAX_OUTPUT_CHARS))
    if len(value) <= lim:
        return value
    return value[:lim].rstrip() + "\n...[output truncated]"


def _run_bot_tool(tool_name: str, payload=None, timeout: int = BOT_TOOL_TIMEOUT_SEC):
    _ensure_bot_tools_dirs()
    payload = payload if isinstance(payload, dict) else {}
    registry = _bot_tool_registry()
    key = _sanitize_tool_name(tool_name)
    spec = registry.get(key)
    if not spec:
        return {"ok": False, "tool": key, "error": f"Unknown tool: {tool_name}"}

    script_code = spec["builder"]()
    script_path = os.path.join(BOT_TOOLS_RUNTIME_DIR, spec["filename"])
    input_path = os.path.join(BOT_TOOLS_IO_DIR, f"{key}_input.json")
    output_path = os.path.join(BOT_TOOLS_IO_DIR, f"{key}_output.json")

    try:
        _write_text_if_changed(script_path, script_code)
        with open(input_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
    except Exception as exc:
        return {"ok": False, "tool": key, "error": f"Failed to prepare tool files: {exc}"}

    try:
        run = subprocess.run(
            [sys.executable, script_path, input_path, output_path],
            capture_output=True,
            text=True,
            timeout=max(5, min(int(timeout or BOT_TOOL_TIMEOUT_SEC), 900)),
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "tool": key,
            "error": "Tool execution timed out.",
            "stdout": _truncate_tool_output(exc.stdout or ""),
            "stderr": _truncate_tool_output(exc.stderr or ""),
        }
    except Exception as exc:
        return {"ok": False, "tool": key, "error": str(exc)}

    parsed = None
    try:
        with open(output_path, "r", encoding="utf-8") as fh:
            parsed = json.load(fh)
    except Exception:
        parsed = None

    if isinstance(parsed, dict):
        parsed["tool"] = key
        if "stdout" in parsed:
            parsed["stdout"] = _truncate_tool_output(parsed.get("stdout", ""))
        if "stderr" in parsed:
            parsed["stderr"] = _truncate_tool_output(parsed.get("stderr", ""))
        return parsed

    return {
        "ok": run.returncode == 0,
        "tool": key,
        "stdout": _truncate_tool_output(run.stdout),
        "stderr": _truncate_tool_output(run.stderr),
        "exit_code": run.returncode,
    }


def _tool_result_context_block(tool_result: dict) -> str:
    if not isinstance(tool_result, dict):
        return ""
    tool = str(tool_result.get("tool", "tool") or "tool")
    parts = [f"Tool: {tool}"]
    if "ok" in tool_result:
        parts.append(f"Success: {bool(tool_result.get('ok'))}")

    for key in ("error", "message"):
        value = str(tool_result.get(key, "") or "").strip()
        if value:
            parts.append(f"{key.capitalize()}: {value[:500]}")

    if tool == "web_search":
        rows = tool_result.get("results", []) if isinstance(tool_result.get("results"), list) else []
        for idx, row in enumerate(rows[:5], start=1):
            title = str(row.get("title", "") or "").strip()
            url = str(row.get("url", "") or "").strip()
            snippet = str(row.get("content", "") or "").strip()
            parts.append(f"Result {idx}: {title}\nURL: {url}\nSnippet: {snippet[:260]}")
    elif tool == "fetch_webpage":
        rows = tool_result.get("results", []) if isinstance(tool_result.get("results"), list) else []
        for idx, row in enumerate(rows[:4], start=1):
            title = str(row.get("title", "") or "").strip()
            url = str(row.get("url", "") or "").strip()
            snippets = row.get("snippets", []) if isinstance(row.get("snippets"), list) else []
            top_snippet = str(snippets[0] if snippets else row.get("text", "") or "").strip()
            parts.append(f"Page {idx}: {title}\nURL: {url}\nRelevant: {top_snippet[:420]}")
    elif tool == "scrape_url":
        title = str(tool_result.get("title", "") or "").strip()
        url = str(tool_result.get("url", "") or "").strip()
        text = str(tool_result.get("text", "") or "").strip()
        if title:
            parts.append(f"Title: {title}")
        if url:
            parts.append(f"URL: {url}")
        if text:
            parts.append("Extracted text:\n" + text[:2000])
    elif tool in {"python_runner", "package_installer"}:
        stdout = str(tool_result.get("stdout", "") or "").strip()
        stderr = str(tool_result.get("stderr", "") or "").strip()
        if stdout:
            parts.append("STDOUT:\n" + stdout[:1500])
        if stderr:
            parts.append("STDERR:\n" + stderr[:1500])

    return "\n\n".join(part for part in parts if part)


def _extract_first_python_block_from_text(text: str) -> str:
    raw = str(text or "")
    if not raw.strip():
        return ""

    fenced_python = re.findall(r"```(?:python|py|python3)\s*\n([\s\S]*?)```", raw, flags=re.IGNORECASE)
    if fenced_python:
        return str(fenced_python[0] or "").strip()

    fenced_any = re.findall(r"```\s*\n([\s\S]*?)```", raw)
    for block in fenced_any:
        candidate = str(block or "").strip()
        if not candidate:
            continue
        if re.search(r"\b(import\s+\w+|from\s+\w+\s+import|def\s+\w+\s*\(|class\s+\w+\s*\(|print\s*\()", candidate):
            return candidate

    return ""


def _query_requests_python_execution(query: str) -> bool:
    text = str(query or "").lower()
    if not text.strip():
        return False
    if "```python" in text or "```py" in text:
        return True
    execution_terms = (
        "run this code",
        "execute this code",
        "run this python",
        "execute this python",
        "run the code",
        "execute the code",
        "debug this code",
        "fix this code and run",
    )
    return any(term in text for term in execution_terms)


def _query_explicitly_requests_web_search(query: str) -> bool:
    text = str(query or "").strip().lower()
    if not text:
        return False
    explicit_terms = (
        "websearch",
        "web search",
        "search web",
        "search the web",
        "look this up online",
        "find online",
        "internet search",
        "browse web",
    )
    return any(term in text for term in explicit_terms)


def _extract_missing_module_name(stderr_text: str) -> str:
    text = str(stderr_text or "")
    if not text:
        return ""
    patterns = [
        r"ModuleNotFoundError:\s*No module named ['\"]([^'\"]+)['\"]",
        r"ImportError:\s*No module named ['\"]([^'\"]+)['\"]",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            value = str(m.group(1) or "").strip()
            if value:
                return value.split(".", 1)[0]
    return ""


def _module_to_package_name(module_name: str) -> str:
    value = str(module_name or "").strip().split(".", 1)[0]
    if not value:
        return ""
    mapped = {
        "bs4": "beautifulsoup4",
        "cv2": "opencv-python",
        "pil": "Pillow",
        "yaml": "PyYAML",
        "sklearn": "scikit-learn",
        "dateutil": "python-dateutil",
    }
    key = value.lower()
    if key in mapped:
        return mapped[key]
    return value


def _is_safe_package_name(package_name: str) -> bool:
    value = str(package_name or "").strip()
    if not value or len(value) > 64:
        return False
    if any(ch in value for ch in ("/", "\\", ":", ";", "&", "|", "`", "\"", "'", " ")):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\-]*", value))


def _auto_python_tool_chain(latest_query: str):
    code = _extract_first_python_block_from_text(latest_query)
    if not code.strip():
        return []

    chain_rows = []
    first_run = _run_bot_tool(
        "python_runner",
        payload={"code": code, "timeout": 90},
        timeout=95,
    )
    if isinstance(first_run, dict):
        first_run["auto_invoked"] = True
    chain_rows.append(first_run)

    if bool(first_run.get("ok")):
        return chain_rows

    missing_module = _extract_missing_module_name(first_run.get("stderr", ""))
    if not missing_module:
        return chain_rows

    package_name = _module_to_package_name(missing_module)
    if not _is_safe_package_name(package_name):
        return chain_rows

    install_result = _run_bot_tool(
        "package_installer",
        payload={"packages": [package_name], "timeout": 300, "upgrade": False},
        timeout=320,
    )
    if isinstance(install_result, dict):
        install_result["auto_invoked"] = True
        install_result["auto_for_module"] = missing_module
    chain_rows.append(install_result)

    if not bool(install_result.get("ok")):
        return chain_rows

    rerun = _run_bot_tool(
        "python_runner",
        payload={"code": code, "timeout": 90},
        timeout=95,
    )
    if isinstance(rerun, dict):
        rerun["auto_invoked"] = True
        rerun["rerun_after_install"] = package_name
    chain_rows.append(rerun)
    return chain_rows


def _tool_rows_to_context(rows, max_items: int = 5) -> str:
    if not isinstance(rows, list):
        return ""
    blocks = []
    for row in rows[:max(1, int(max_items or 5))]:
        block = _tool_result_context_block(row)
        if block:
            blocks.append(block)
    if not blocks:
        return ""
    return (
        "Tool output context. Treat these as observed execution/search results and explain from them first.\n\n"
        + "\n\n".join(blocks)
    )


def _collect_tool_results_for_chat(latest_query: str, tool_requests=None, search_web: bool = False):
    rows = []
    requests_list = tool_requests if isinstance(tool_requests, list) else []
    allow_web_tools = bool(search_web or _query_explicitly_requests_web_search(latest_query))

    for item in requests_list[:4]:
        if not isinstance(item, dict):
            continue
        tool_name = str(item.get("tool", "") or "").strip()
        payload = item.get("input", {}) if isinstance(item.get("input"), dict) else {}
        timeout = int(item.get("timeout", BOT_TOOL_TIMEOUT_SEC) or BOT_TOOL_TIMEOUT_SEC)
        if not tool_name:
            continue
        if tool_name in {"web_search", "fetch_webpage"} and not allow_web_tools:
            continue
        rows.append(_run_bot_tool(tool_name, payload=payload, timeout=timeout))

    if not rows and bool(search_web):
        rows.append(
            _run_bot_tool(
                "web_search",
                payload={"query": latest_query, "limit": 6, "region": "wt-wt"},
                timeout=25,
            )
        )

    web_result_urls = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("tool", "") or "") != "web_search":
            continue
        if not bool(row.get("ok", False)):
            continue
        raw_results = row.get("results", []) if isinstance(row.get("results"), list) else []
        for hit in raw_results:
            if not isinstance(hit, dict):
                continue
            url = str(hit.get("url", "") or "").strip()
            if _is_http_url(url):
                web_result_urls.append(url)
        break

    if web_result_urls:
        deduped = []
        seen = set()
        for url in web_result_urls:
            key = url.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(url)
            if len(deduped) >= 3:
                break
        if deduped:
            fetch_result = _run_bot_tool(
                "fetch_webpage",
                payload={
                    "urls": deduped,
                    "query": latest_query,
                    "top_k": 3,
                    "max_chars": 12000,
                    "timeout": 25,
                },
                timeout=50,
            )
            if isinstance(fetch_result, dict):
                fetch_result["auto_invoked"] = True
                fetch_result["auto_reason"] = "expanded_web_search"
            rows.append(fetch_result)

    if not rows:
        urls = _extract_http_urls(latest_query)
        query_lower = str(latest_query or "").lower()
        wants_scrape = any(token in query_lower for token in ("scrape", "extract", "crawl", "read this page"))
        if urls and wants_scrape:
            rows.append(
                _run_bot_tool(
                    "scrape_url",
                    payload={"url": urls[0], "timeout": 25, "max_chars": 10000},
                    timeout=35,
                )
            )

    if not rows and _query_requests_python_execution(latest_query):
        rows.extend(_auto_python_tool_chain(latest_query))

    context_parts = []
    for row in rows:
        block = _tool_result_context_block(row)
        if block:
            context_parts.append(block)

    context = ""
    if context_parts:
        context = (
            "Tool output context. Treat these as observed execution/search results and explain from them first.\n\n"
            + "\n\n".join(context_parts[:5])
        )

    return {
        "used": bool(rows),
        "results": rows,
        "context": context,
    }


def _looks_like_noise_line(value: str) -> bool:
    low = _compress_spaces(value).lower()
    if len(low) < 25:
        return True
    blocked_patterns = [
        r"^table of contents\b",
        r"^learn about:\b",
        r"^google news\b",
        r"^news about\b",
        r"\bclick here\b",
        r"\bread more\b",
        r"\bnewsletter\b",
        r"\badvertisement\b",
    ]
    return any(re.search(pattern, low) for pattern in blocked_patterns)


def _distinct_items(items, max_items: int = 8):
    out = []
    seen = set()
    for item in items:
        text = _compress_spaces(item)
        if not text or _looks_like_noise_line(text):
            continue
        key = _normalize_line_for_dedupe(text)
        if len(key) < 18 or key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= max_items:
            break
    return out


def _llama_available() -> bool:
    return _LLAMA_CPP_OK


def _download_gguf_model() -> str:
    """Download the GGUF model file if not already present. Returns path to file."""
    os.makedirs(LLAMA_MODELS_DIR, exist_ok=True)
    model_path = os.path.join(LLAMA_MODELS_DIR, LLAMA_MODEL_FILE)
    if os.path.exists(model_path):
        return model_path
    _set_llama_status("downloading", f"Downloading {LLAMA_MODEL_FILE}")
    try:
        from huggingface_hub import hf_hub_download
        os.environ["HF_HUB_DISABLE_XET"] = "1"
        hf_token = (
            os.getenv("HF_TOKEN")
            or os.getenv("HUGGINGFACE_HUB_TOKEN")
            or os.getenv("HF_HUB_TOKEN")
            or None
        )
        print(f"[llama.cpp] Downloading {LLAMA_MODEL_FILE} from {LLAMA_MODEL_REPO} ...")
        downloaded = hf_hub_download(
            repo_id=LLAMA_MODEL_REPO,
            filename=LLAMA_MODEL_FILE,
            local_dir=LLAMA_MODELS_DIR,
            token=hf_token,
            resume_download=True,
        )
        print(f"[llama.cpp] Model saved to {downloaded}")
        return downloaded
    except Exception as exc:
        raise RuntimeError(f"Failed to download GGUF model: {exc}") from exc


def _ensure_llama_model_file() -> str:
    os.makedirs(LLAMA_MODELS_DIR, exist_ok=True)
    model_path = os.path.join(LLAMA_MODELS_DIR, LLAMA_MODEL_FILE)
    if os.path.exists(model_path):
        _set_llama_status("ready", "Model file is ready")
        return model_path
    return _download_gguf_model()


def _background_prepare_llama_model_file():
    if not _llama_available():
        return
    try:
        _set_llama_status("initializing", "Checking local model file")
        _ensure_llama_model_file()
    except Exception as exc:
        _set_llama_status("error", str(exc))
        return
    # Also load the model into memory so the first user request is instant
    try:
        _load_llama_model()
    except Exception as exc:
        _set_llama_status("error", str(exc))


def _switch_model_to(filename: str, repo: str = "", n_ctx: int = 0):
    """Completely replace the active local model at runtime. Deletes the old model file."""
    global _LLAMA_MODEL, _LLAMA_LOAD_ERROR, LLAMA_MODEL_FILE, LLAMA_MODEL_REPO, LLAMA_N_CTX, _MODEL_SELECTION_OVERRIDDEN, _MODEL_SELECTION_SOURCE
    tier_info = _resolve_model_tier_by_filename(filename)
    resolved_repo = repo or (tier_info[1] if tier_info else LLAMA_MODEL_REPO)
    resolved_n_ctx = n_ctx or (tier_info[3] if tier_info else 4096)

    with _LLM_LOCK:
        # Unload current model from memory
        if _LLAMA_MODEL is not None:
            try:
                del _LLAMA_MODEL
            except Exception:
                pass
            _LLAMA_MODEL = None

        _LLAMA_LOAD_ERROR = ""
        LLAMA_MODEL_FILE = filename
        LLAMA_MODEL_REPO = resolved_repo
        LLAMA_N_CTX = resolved_n_ctx
        _MODEL_SELECTION_OVERRIDDEN = True
        _MODEL_SELECTION_SOURCE = "persisted"

    _persist_model_selection(_ACTIVE_MODEL_STATE_PATH, filename, resolved_repo, resolved_n_ctx)

    _set_llama_status("switching", f"Switched to {filename} — will download & load on next use")
    print(f"[model-switch] Active model -> {filename} (repo={resolved_repo}, n_ctx={resolved_n_ctx})")
    thread = threading.Thread(target=_background_prepare_llama_model_file, daemon=True)
    thread.start()


def _load_llama_model():
    """Load (or return cached) the llama-cpp Llama instance."""
    global _LLAMA_MODEL, _LLAMA_LOAD_ERROR
    if _LLAMA_MODEL is not None:
        _set_llama_status("ready", "Model loaded")
        return _LLAMA_MODEL
    if not _LLAMA_CPP_OK:
        _LLAMA_LOAD_ERROR = "llama-cpp-python not installed. Run: pip install llama-cpp-python"
        _set_llama_status("error", _LLAMA_LOAD_ERROR)
        return None
    with _LLM_LOCK:
        if _LLAMA_MODEL is not None:
            _set_llama_status("ready", "Model loaded")
            return _LLAMA_MODEL
        try:
            _set_llama_status("initializing", "Preparing local model")
            model_path = _ensure_llama_model_file()
            _set_llama_status("loading", "Loading model into memory")
            print(f"[llama.cpp] Loading model from {model_path} ...")
            cpu_count = max(1, int(os.cpu_count() or 4))
            thread_cap = max(1, min(int(LLAMA_N_THREADS or 2), 4, cpu_count))
            _LLAMA_MODEL = _LlamaCpp(
                model_path=model_path,
                n_ctx=max(2048, LLAMA_N_CTX),
                n_threads=thread_cap,
                verbose=False,
            )
            _LLAMA_LOAD_ERROR = ""
            _set_llama_status("ready", "Model loaded")
            print("[llama.cpp] Model loaded successfully.")
            return _LLAMA_MODEL
        except Exception as exc:
            _LLAMA_LOAD_ERROR = str(exc)
            _set_llama_status("error", _LLAMA_LOAD_ERROR)
            return None


def _normalize_code_provider(provider: str) -> str:
    value = str(provider or "llama").strip().lower()
    if value in {"gpt4free", "g4f", "gpt4f", "gpt-4f", "gpt 4f"}:
        return "gpt4free"
    return "llama"


def _is_trivial_greeting(query: str) -> bool:
    q = str(query or "").strip().lower()
    if not q:
        return False
    if len(q) > 24:
        return False
    q = re.sub(r"[^a-z\s]", "", q)
    q = re.sub(r"\s+", " ", q).strip()
    trivial = {
        "hi", "hello", "hey", "yo", "sup", "hola", "namaste",
        "hi there", "hello there", "hey there", "good morning", "good evening",
    }
    return q in trivial


def _suggest_llama_max_tokens(latest_query: str, base_limit: int) -> int:
    base = max(128, int(base_limit or 512))
    q = str(latest_query or "").strip().lower()
    if not q:
        return min(base, 384)
    if _query_requests_python_execution(q) or _query_requires_code_output(q):
        return base
    heavy_terms = (
        "implement", "write code", "function", "debug", "fix", "error", "traceback",
        "refactor", "patch", "api", "class", "algorithm", "optimize", "sql", "regex",
    )
    if any(term in q for term in heavy_terms):
        return min(base, 1024)
    if len(q) <= 120:
        return min(base, 320)
    if len(q) <= 260:
        return min(base, 512)
    return min(base, 768)


def _is_code_like_query(query: str) -> bool:
    q = str(query or "").strip().lower()
    if not q:
        return False
    code_markers = {
        "code", "python", "javascript", "typescript", "java", "c++", "c#", "bug", "debug",
        "error", "traceback", "exception", "function", "class", "method", "api", "endpoint",
        "refactor", "fix", "stack", "module", "import", "run", "script", "repo", "file",
    }
    tokens = set(re.findall(r"[a-z0-9_+#.-]+", q))
    return bool(tokens & code_markers)


def _is_excel_analysis_request(query: str) -> bool:
    q = str(query or "").strip().lower()
    if not q:
        return False
    has_excel_word = any(word in q for word in ["excel", "xlsx", "xls", "csv", "tsv", "json", "spreadsheet", "sheet", "table", "tabular"])
    has_analysis_word = any(word in q for word in [
        "analyze", "analysis", "summarize", "summary", "review", "check",
        "calculate", "avg", "average", "mean", "sum", "total", "group", "groupby",
        "aggregate", "filter", "update", "set", "fill", "rows", "records",
    ])
    return has_excel_word and has_analysis_word


def _is_tabular_data_request(query: str) -> bool:
    q = str(query or "").strip().lower()
    if not q:
        return False
    if _is_dataframe_metadata_parse_request(q):
        return True
    tabular_terms = ["excel", "xlsx", "xls", "csv", "tsv", "json", "spreadsheet", "worksheet", "sheet", "table", "tabular", "pandas", "dataframe", "metadata", "page_infos"]
    operation_terms = [
        "analyze", "analysis", "summarize", "summary", "calculate", "avg", "average", "mean",
        "sum", "total", "group", "groupby", "aggregate", "filter", "sort", "update",
        "set", "fill", "replace", "rows", "records", "columns", "hours", "parse", "flatten", "expand", "normalize", "extract",
    ]
    return any(term in q for term in tabular_terms) and any(term in q for term in operation_terms)


def _is_dataframe_metadata_parse_request(query: str) -> bool:
    q = str(query or "").strip().lower()
    if not q:
        return False
    structure_terms = ["metadata", "page_infos", "json_normalize", "nested json", "metadata structure"]
    frame_terms = ["pandas", "dataframe", "load_as_pandas", "fg.head", ".head()", ".head", "feature group"]
    action_terms = ["parse", "flatten", "expand", "normalize", "extract", "columns", "keys"]
    return any(term in q for term in structure_terms) and any(term in q for term in frame_terms) and any(term in q for term in action_terms)


def _query_requests_full_dataset(query: str) -> bool:
    q = str(query or "").strip().lower()
    if not q:
        return False
    full_markers = ["all", "every", "entire", "complete", "whole", "full"]
    subject_markers = ["employee", "employees", "rows", "records", "worksheet", "sheet", "table"]
    return any(m in q for m in full_markers) and any(s in q for s in subject_markers)


def _is_spreadsheet_context_block(path: str, content: str) -> bool:
    lower_path = str(path or "").strip().lower()
    if lower_path.endswith((".xlsx", ".xlsm", ".xls", ".csv", ".tsv", ".json")):
        return True
    text = str(content or "")
    if "Sheet " in text and "|" in text:
        return True
    if any(marker in text.lower() for marker in ["employee name", "hours saved", "tool name", "project name"]) and "|" in text:
        return True
    return False


def _normalize_model_name(model_name: str) -> str:
    return _compress_spaces(str(model_name or "")).strip()[:120]


def _expand_g4f_model_aliases(model_name: str):
    value = _normalize_model_name(model_name)
    if not value:
        return []

    alias_map = {
        "deepseek-v3": ["deepseek-v3", "deepseek-chat"],
        "deepseek chat": ["deepseek-chat", "deepseek-v3"],
        "deepseek-chat": ["deepseek-chat", "deepseek-v3"],
    }
    key = value.lower()
    aliases = alias_map.get(key, [value])

    out = []
    seen = set()
    for alias in aliases:
        normalized_alias = _normalize_model_name(alias)
        alias_key = normalized_alias.lower()
        if normalized_alias and alias_key not in seen:
            seen.add(alias_key)
            out.append(normalized_alias)
    return out


def _trim_code_context(code_context: str, max_chars: int = CODE_CONTEXT_MAX_CHARS) -> str:
    text = str(code_context or "").strip()
    if not text:
        return ""

    limit = max(1500, int(max_chars or CODE_CONTEXT_MAX_CHARS))
    if len(text) <= limit:
        return text

    head = text[: limit // 2]
    tail = text[-max(800, limit - len(head) - 64):]
    return (
        head.rstrip()
        + "\n\n...[context truncated for length]...\n\n"
        + tail.lstrip()
    )


def _context_terms(text: str):
    raw_terms = re.findall(r"[A-Za-z_][A-Za-z0-9_./:-]{1,60}", str(text or "").lower())
    out = []
    seen = set()
    for term in raw_terms:
        if len(term) <= 1:
            continue
        if term in _CONTEXT_STOPWORDS:
            continue
        if term.isdigit():
            continue
        if term not in seen:
            seen.add(term)
            out.append(term)
    return out


def _is_project_overview_query(query: str) -> bool:
    q = str(query or "").strip().lower()
    if not q:
        return False
    patterns = (
        "what is this project",
        "what's this project",
        "what is this repo",
        "what's this repo",
        "about this project",
        "project about",
        "summarize project",
        "summary of project",
        "explain this project",
    )
    if any(p in q for p in patterns):
        return True
    return ("project" in q or "repo" in q) and ("about" in q or "summary" in q or "what" in q or "explain" in q)


def _overview_file_bonus(rel_path: str) -> int:
    path = str(rel_path or "").lower()
    filename = os.path.basename(path)
    bonus = 0
    if filename == "readme.md" or filename == "readme":
        bonus += 160
    if path.startswith("hf_space/") and filename == "readme.md":
        bonus += 40
    if filename in {"requirements.txt", "app.py", "deploy_hf.py", "setup.bat"}:
        bonus += 12
    if path.startswith("templates/"):
        bonus += 6
    if ".github/" in path or "copilot-instructions" in path:
        bonus -= 200
    return bonus


def _parse_file_context_blocks(code_context: str):
    text = str(code_context or "")
    pattern = re.compile(
        r"\[File Context\]\s*(?P<path>[^\n]+)\n```(?P<lang>[^\n]*)\n(?P<content>[\s\S]*?)\n```",
        flags=re.IGNORECASE,
    )

    blocks = []
    for match in pattern.finditer(text):
        path = str(match.group("path") or "").strip()
        lang = str(match.group("lang") or "text").strip().lower() or "text"
        content = str(match.group("content") or "").rstrip()
        if path and content:
            blocks.append({"path": path, "lang": lang, "content": content})

    notes = pattern.sub("", text)
    notes = re.sub(r"\n{3,}", "\n\n", notes).strip()
    return {"files": blocks, "notes": notes}


def _normalize_attached_context_files(items, max_items: int = 10, max_chars_per_file: int = 12000):
    if not isinstance(items, list):
        return []
    out = []
    for row in items[: max(1, int(max_items or 10))]:
        if not isinstance(row, dict):
            continue
        path = str(row.get("path", "") or "").strip()
        lang = str(row.get("lang", "text") or "text").strip().lower() or "text"
        content = str(row.get("content", "") or "").replace("\x00", "").strip()
        if not path or not content:
            continue
        out.append(
            {
                "path": path,
                "lang": lang,
                "content": content[: max(400, int(max_chars_per_file or 12000))],
            }
        )
    return out


def _build_attached_context_blocks_text(items) -> str:
    normalized = _normalize_attached_context_files(items)
    if not normalized:
        return ""
    blocks = []
    for row in normalized:
        blocks.append(
            "[File Context] "
            + str(row.get("path", "") or "")
            + "\n```"
            + str(row.get("lang", "text") or "text")
            + "\n"
            + str(row.get("content", "") or "")
            + "\n```"
        )
    return "\n\n".join(blocks)


def _split_file_context_chunks(path: str, content: str, lang: str = "text", lines_per_chunk: int = 60, overlap: int = 10):
    lines = str(content or "").splitlines()
    if not lines:
        return []

    total = len(lines)
    if total <= lines_per_chunk:
        return [{
            "path": path,
            "lang": lang or "text",
            "content": str(content or "").rstrip(),
            "line_start": 1,
            "line_end": total,
        }]

    chunks = []
    step = max(12, int(lines_per_chunk) - max(0, int(overlap)))
    start = 0
    while start < total:
        end = min(total, start + lines_per_chunk)
        chunk_lines = lines[start:end]
        if chunk_lines:
            chunks.append({
                "path": path,
                "lang": lang or "text",
                "content": "\n".join(chunk_lines).rstrip(),
                "line_start": start + 1,
                "line_end": end,
            })
        if end >= total:
            break
        start += step
    return chunks


def _score_context_chunk(query: str, chunk: dict) -> int:
    terms = _context_terms(query)
    if not terms:
        return 0

    path = str(chunk.get("path", "") or "").lower()
    basename = path.rsplit("/", 1)[-1]
    content = str(chunk.get("content", "") or "").lower()
    score = 0
    for term in terms:
        if term in basename:
            score += 10
        elif term in path:
            score += 6
        if term in content:
            score += 2
    if any(symbol in content for symbol in terms[:8]):
        score += 2
    return score


def _build_retrieved_code_context(query: str, code_context: str = "", runtime_context: str = "", max_chars: int = CODE_CONTEXT_MAX_CHARS):
    parsed = _parse_file_context_blocks(code_context)
    file_blocks = parsed.get("files", [])
    notes = str(parsed.get("notes", "") or "").strip()
    runtime = str(runtime_context or "").strip()

    sections = []
    budget = max(1800, int(max_chars or CODE_CONTEXT_MAX_CHARS))
    has_spreadsheet_blocks = any(
        _is_spreadsheet_context_block(block.get("path", ""), block.get("content", ""))
        for block in file_blocks
    )
    if has_spreadsheet_blocks and _query_requests_full_dataset(query):
        budget = max(budget, 42000)

    if file_blocks:
        attached_paths = [str(item.get("path", "") or "").strip() for item in file_blocks if str(item.get("path", "") or "").strip()]
        if attached_paths:
            sections.append("Attached files: " + ", ".join(attached_paths[:8]))

        ranked_chunks = []
        for block in file_blocks:
            for chunk in _split_file_context_chunks(block.get("path", ""), block.get("content", ""), block.get("lang", "text")):
                chunk["score"] = _score_context_chunk(query, chunk)
                ranked_chunks.append(chunk)

        ranked_chunks.sort(
            key=lambda item: (
                int(item.get("score", 0)),
                1 if item.get("line_start", 1) == 1 else 0,
                -len(str(item.get("path", "") or "")),
            ),
            reverse=True,
        )

        selected_chunks = []
        query_needs_full_dataset = has_spreadsheet_blocks and _query_requests_full_dataset(query)

        if query_needs_full_dataset:
            spreadsheet_chunks = []
            other_chunks = []
            for chunk in ranked_chunks:
                if _is_spreadsheet_context_block(chunk.get("path", ""), chunk.get("content", "")):
                    spreadsheet_chunks.append(chunk)
                else:
                    other_chunks.append(chunk)

            spreadsheet_chunks.sort(
                key=lambda item: (
                    str(item.get("path", "") or ""),
                    int(item.get("line_start", 1) or 1),
                )
            )

            seen_ranges = set()
            for chunk in spreadsheet_chunks:
                key = (chunk.get("path"), chunk.get("line_start"), chunk.get("line_end"))
                if key in seen_ranges:
                    continue
                seen_ranges.add(key)
                selected_chunks.append(chunk)
                if len(selected_chunks) >= 18:
                    break

            if len(selected_chunks) < 18:
                for chunk in other_chunks:
                    key = (chunk.get("path"), chunk.get("line_start"), chunk.get("line_end"))
                    if key in seen_ranges:
                        continue
                    seen_ranges.add(key)
                    selected_chunks.append(chunk)
                    if len(selected_chunks) >= 18:
                        break
        else:
            seen_ranges = set()
            for chunk in ranked_chunks:
                key = (chunk.get("path"), chunk.get("line_start"), chunk.get("line_end"))
                if key in seen_ranges:
                    continue
                if chunk.get("score", 0) <= 0 and selected_chunks:
                    continue
                seen_ranges.add(key)
                selected_chunks.append(chunk)
                if len(selected_chunks) >= 5:
                    break

        if not selected_chunks and ranked_chunks:
            selected_chunks = ranked_chunks[:2]

        for chunk in selected_chunks:
            label = f"File: {chunk.get('path')} (lines {chunk.get('line_start')}-{chunk.get('line_end')})"
            block = label + f"\n```{chunk.get('lang', 'text')}\n{chunk.get('content', '')}\n```"
            sections.append(block)

    if notes:
        sections.append("User notes:\n" + _trim_code_context(notes, max_chars=min(2200, budget // 3)))
    if runtime:
        sections.append("Latest runtime output (authoritative):\n" + _trim_code_context(runtime, max_chars=min(2600, budget // 2)))

    if not sections:
        return ""

    intro = (
        "Workspace context assembled like an IDE assistant: relevant attached file snippets are prioritized over full-file dumps. "
        "Use the snippets below as the primary grounded context."
    )
    compact = intro + "\n\n" + "\n\n".join(sections)
    return _trim_code_context(compact, max_chars=budget)


def _workspace_root_dir() -> str:
    root = os.getenv("WORKSPACE_ROOT", "").strip()
    if root and os.path.isdir(root):
        return root
    return os.path.dirname(os.path.abspath(__file__))


def _workspace_file_ext(path: str) -> str:
    name = os.path.basename(str(path or "")).lower()
    ext = os.path.splitext(name)[1].lower()
    if ext:
        return ext
    if name.startswith(".") and name in _WORKSPACE_ALLOWED_EXTENSIONS:
        return name
    return ""


def _normalize_search_token(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def _normalize_search_scope(scope: str) -> str:
    value = str(scope or "workspace").strip().lower()
    return "pc" if value in {"pc", "all", "computer", "system"} else "workspace"


def _pc_search_roots():
    configured = str(os.getenv("SEARCH_PC_ROOTS", "")).strip()
    if configured:
        roots = []
        for part in re.split(r"[;,]", configured):
            path = str(part or "").strip()
            if path and os.path.isdir(path):
                roots.append(path)
        if roots:
            return roots

    # Use home dir as primary root — covers Desktop/Documents/Projects etc.
    # Skips system dirs and large caches via _PC_IGNORED_DIRS
    home = os.path.expanduser("~")
    roots = []
    if home and os.path.isdir(home):
        roots.append(home)

    if os.name == "nt":
        try:
            import ctypes
            mask = int(ctypes.windll.kernel32.GetLogicalDrives())
            for i in range(26):
                if mask & (1 << i):
                    drive = f"{chr(65 + i)}:\\"
                    drive_norm = os.path.normcase(os.path.abspath(drive))
                    home_norm = os.path.normcase(os.path.abspath(home)) if home else ""
                    # Only add drive if home isn't under it (avoid duplicate walk)
                    if home_norm.startswith(drive_norm):
                        continue
                    if os.path.isdir(drive):
                        roots.append(drive)
        except Exception:
            pass

    return roots if roots else [_workspace_root_dir()]


def _iter_pc_candidate_files(max_files: int = 2_000_000):
    yielded = 0
    hard_cap = max(2_000_000, int(max_files or 2_000_000))
    for root in _pc_search_roots():
        for base, dirs, files in os.walk(root):
            dirs[:] = [name for name in dirs if name.lower() not in _PC_IGNORED_DIRS]
            for filename in files:
                path = os.path.join(base, filename)
                yield path
                yielded += 1
                if yielded >= hard_cap:
                    return


def _normalize_candidate_paths(candidate_paths, root_dir: str):
    if not isinstance(candidate_paths, list):
        return None
    normalized = set()
    for item in candidate_paths:
        value = str(item or "").strip().replace("\\", "/").lstrip("/")
        if not value:
            continue
        abs_path = os.path.normpath(os.path.join(root_dir, value))
        try:
            if not os.path.commonpath([root_dir, abs_path]).startswith(root_dir):
                continue
        except Exception:
            continue
        normalized.add(abs_path)
    return normalized if normalized else None


def _iter_workspace_candidate_files(root_dir: str, max_files: int = WORKSPACE_SEARCH_MAX_FILES, candidate_paths=None):
    candidate_set = _normalize_candidate_paths(candidate_paths, root_dir)
    if candidate_set is not None:
        yielded = 0
        for path in candidate_set:
            if not os.path.isfile(path):
                continue
            yield path
            yielded += 1
            if yielded >= max(50, int(max_files or WORKSPACE_SEARCH_MAX_FILES)):
                break
        return

    yielded = 0
    for base, dirs, files in os.walk(root_dir):
        dirs[:] = [name for name in dirs if name.lower() not in _WORKSPACE_IGNORED_DIRS]
        for filename in files:
            path = os.path.join(base, filename)
            yield path
            yielded += 1
            if yielded >= max(50, int(max_files or WORKSPACE_SEARCH_MAX_FILES)):
                return


def _read_text_file_safe(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            return handle.read()
    except Exception:
        return ""


def _clip_workspace_preview(text: str, max_chars: int = 700, max_lines: int = 14) -> str:
    raw = str(text or "")
    if not raw:
        return ""
    max_lines_safe = max(1, int(max_lines or 14))
    max_chars_safe = max(120, int(max_chars or 700))
    lines = raw.splitlines()
    clipped = "\n".join(lines[:max_lines_safe]).rstrip()
    if len(lines) > max_lines_safe:
        clipped += "\n...[truncated]"
    if len(clipped) > max_chars_safe:
        clipped = clipped[:max_chars_safe].rstrip() + "…"
    return clipped


def _search_workspace_files(query: str, max_results: int = 8, root_dir: str = "", candidate_paths=None, scope: str = "workspace"):
    q = str(query or "").strip()
    if not q:
        return []

    terms = _context_terms(q)
    q_lower = q.lower()
    q_compact = _normalize_search_token(q_lower)
    symbol_like_query = bool(re.match(r"^[a-z_][a-z0-9_]{2,60}$", q_lower))
    overview_query = _is_project_overview_query(q)
    deep_read_query = overview_query or any(token in q.lower() for token in {
        "explain", "workflow", "where", "why", "how", "issue", "error", "bug", "fix", "implement", "architecture",
    })
    if not terms:
        if not overview_query:
            return []
        terms = ["readme", "fastapi", "search", "project"]

    resolved_scope = _normalize_search_scope(scope)
    root = root_dir or _workspace_root_dir()
    ranked = []

    iterator = _iter_pc_candidate_files() if resolved_scope == "pc" else _iter_workspace_candidate_files(root, candidate_paths=candidate_paths)
    _enough_candidates = (max_results or 10) * 40
    for path in iterator:
        rel_path = path.replace("\\", "/") if resolved_scope == "pc" else os.path.relpath(path, root).replace("\\", "/")
        filename = os.path.basename(rel_path).lower()
        rel_lower = rel_path.lower()
        filename_compact = _normalize_search_token(filename)
        rel_compact = _normalize_search_token(rel_lower)

        file_size = 0
        try:
            file_size = int(os.path.getsize(path) or 0)
        except Exception:
            file_size = 0

        max_bytes = max(1024, int(WORKSPACE_SEARCH_MAX_FILE_BYTES))
        can_read_content = file_size <= max_bytes
        content = _read_text_file_safe(path) if can_read_content else ""
        lowered = content.lower() if content else ""

        score = 0
        for term in terms:
            term_compact = _normalize_search_token(term)
            if term in filename:
                score += 14
            elif term_compact and term_compact in filename_compact:
                score += 14
            if term in rel_lower:
                score += 8
            elif term_compact and term_compact in rel_compact:
                score += 8
            if lowered and term in lowered:
                score += min(24, lowered.count(term) * 2)

        if lowered and q_lower and q_lower in lowered:
            score += 20
        if q_lower and q_lower in rel_lower:
            score += 20
        if q_compact and q_compact in rel_compact:
            score += 24
        if q_compact and q_compact in filename_compact:
            score += 24
        if rel_path.lower().endswith(".ipynb"):
            score -= 14
        if rel_path.lower().endswith("setup.bat"):
            score -= 6

        if overview_query:
            score += _overview_file_bonus(rel_path)

        if score <= 0:
            continue

        if resolved_scope == "pc" and len(ranked) >= _enough_candidates:
            break

        if not lowered:
            ranked.append({
                "path": rel_path,
                "lang": "text",
                "line_start": 1,
                "line_end": 1,
                "snippet": _clip_workspace_preview("Path match only (file is large or non-text): " + rel_path, max_chars=700, max_lines=4),
                "score": score,
            })
            continue

        lines = content.splitlines()
        hit_line_idx = None
        best_line_score = -10**9
        for idx, line in enumerate(lines):
            lowered_line = line.lower()
            if not any(term in lowered_line for term in terms):
                continue
            line_score = 0
            for term in terms:
                if term in lowered_line:
                    line_score += min(20, lowered_line.count(term) * 4)
            if q_lower and q_lower in lowered_line:
                line_score += 28
            if "=" in line:
                line_score += 6
            if "getenv" in lowered_line or "os.environ" in lowered_line:
                line_score += 10
            if symbol_like_query and q_lower in lowered_line:
                if (
                    lowered_line.strip().startswith("def ")
                    or lowered_line.strip().startswith("class ")
                    or lowered_line.strip().startswith("async def ")
                    or "function " in lowered_line
                    or "const " in lowered_line
                    or "let " in lowered_line
                ):
                    line_score += 40
            if "placeholder" in lowered_line or "e.g." in lowered_line or "example" in lowered_line:
                line_score -= 10
            if lowered_line.strip().startswith("#"):
                line_score -= 3
            if "$env:" in lowered_line:
                line_score -= 4
            if line_score > best_line_score:
                best_line_score = line_score
                hit_line_idx = idx

        if hit_line_idx is None:
            if score > 0:
                preview = "\n".join(lines[: min(8, len(lines))]).rstrip() if lines else ("Path match: " + rel_path)
                ranked.append({
                    "path": rel_path,
                    "lang": "text",
                    "line_start": 1,
                    "line_end": max(1, min(8, len(lines))),
                    "snippet": _clip_workspace_preview(preview, max_chars=700, max_lines=14),
                    "score": score,
                })
            continue

        window_before = 25 if deep_read_query else 8
        window_after = 30 if deep_read_query else 10
        start = max(0, hit_line_idx - window_before)
        end = min(len(lines), hit_line_idx + window_after)
        snippet = _clip_workspace_preview("\n".join(lines[start:end]).rstrip(), max_chars=700, max_lines=14)
        ext = _workspace_file_ext(rel_path)
        lang = ext.lstrip(".") or "text"
        if lang in {"pyw"}:
            lang = "python"
        elif lang in {"yml"}:
            lang = "yaml"
        elif lang in {"htm"}:
            lang = "html"
        elif lang in {"env"}:
            lang = "bash"

        ranked.append({
            "path": rel_path,
            "lang": lang,
            "line_start": start + 1,
            "line_end": end,
            "snippet": snippet,
            "score": score,
        })

    ranked.sort(key=lambda row: (int(row.get("score", 0)), -int(row.get("line_start", 1))), reverse=True)
    out = []
    seen = set()
    for item in ranked:
        key = (item.get("path"), item.get("line_start"), item.get("line_end"))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= max(1, min(int(max_results or 8), 200)):
            break
    return out


def _build_workspace_context(query: str, max_items: int = 3, candidate_paths=None, scope: str = "workspace"):
    deep_read_query = _is_project_overview_query(query) or any(token in str(query or "").lower() for token in {
        "explain", "workflow", "where", "why", "how", "issue", "error", "bug", "fix", "implement", "architecture",
    })
    target_items = max(1, int(max_items or 3))
    if deep_read_query:
        target_items = max(target_items, 5)
    matches = _search_workspace_files(query, max_results=target_items, candidate_paths=candidate_paths, scope=scope)
    if not matches:
        return {"context": "", "matches": [], "used": False}

    chunks = []
    for item in matches[:target_items]:
        path = str(item.get("path", "") or "")
        lang = str(item.get("lang", "text") or "text")
        line_start = int(item.get("line_start", 1) or 1)
        line_end = int(item.get("line_end", line_start) or line_start)
        snippet = str(item.get("snippet", "") or "").rstrip()
        if not path or not snippet:
            continue
        chunks.append(f"File: {path} (lines {line_start}-{line_end})\n```{lang}\n{snippet}\n```")

    if not chunks:
        return {"context": "", "matches": [], "used": False}

    intro = "Workspace search context (auto-retrieved from current project files with expanded read windows):"
    return {
        "context": intro + "\n\n" + "\n\n".join(chunks),
        "matches": matches,
        "used": True,
    }


def _chat_query_needs_expansion(query: str) -> bool:
    q = str(query or "").strip().lower()
    if not q:
        return True
    if len(q) < 20:
        return True
    vague_markers = {"this", "that", "it", "here", "there", "same", "again", "above", "previous"}
    words = set(re.findall(r"[a-zA-Z_]+", q))
    return bool(words & vague_markers)


def _extract_query_symbols(text: str, max_items: int = 5):
    raw = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,60}\b", str(text or ""))
    out = []
    seen = set()
    for item in raw:
        token = str(item or "").strip()
        key = token.lower()
        if not token or key in seen:
            continue
        if key in _CONTEXT_STOPWORDS:
            continue
        if token.isdigit():
            continue
        seen.add(key)
        out.append(token)
        if len(out) >= max(1, int(max_items or 5)):
            break
    return out


def _build_workspace_context_for_chat(messages, latest_query: str, candidate_paths=None, max_items: int = 5, scope: str = "workspace"):
    primary = str(latest_query or "").strip()
    if not primary:
        return {"context": "", "matches": [], "used": False}

    queries = [primary]
    for symbol in _extract_query_symbols(primary, max_items=5):
        if symbol.lower() != primary.lower():
            queries.append(symbol)

    if _chat_query_needs_expansion(primary):
        prior_user = ""
        for row in reversed(list(messages or [])[:-1]):
            if str(row.get("role", "") or "").strip().lower() == "user":
                prior_user = str(row.get("content", "") or "").strip()
                if prior_user:
                    break
        if prior_user:
            queries.append(prior_user)

    dedup_queries = []
    seen_queries = set()
    for q in queries:
        key = str(q or "").strip().lower()
        if not key or key in seen_queries:
            continue
        seen_queries.add(key)
        dedup_queries.append(str(q).strip())
    dedup_queries = dedup_queries[:4]

    ws_root = _workspace_root_dir()
    ws_root_norm = os.path.normcase(ws_root) if ws_root else ""

    ranked = []
    for idx, query in enumerate(dedup_queries):
        rows = _search_workspace_files(query, max_results=max(8, int(max_items) * 2), candidate_paths=candidate_paths, scope=scope)
        for row in rows:
            scored = dict(row)
            base = int(scored.get("score", 0) or 0) + max(0, 18 - (idx * 5))
            # Strongly boost files from this app's own workspace
            path_norm = os.path.normcase(str(scored.get("path", "") or ""))
            if ws_root_norm and path_norm.startswith(ws_root_norm):
                base += 40
            scored["score"] = base
            ranked.append(scored)

    if not ranked:
        return {"context": "", "matches": [], "used": False}

    ranked.sort(key=lambda item: (int(item.get("score", 0)), -int(item.get("line_start", 1))), reverse=True)
    selected = []
    seen = set()
    for item in ranked:
        key = (item.get("path"), item.get("line_start"), item.get("line_end"))
        if key in seen:
            continue
        seen.add(key)
        selected.append(item)
        if len(selected) >= max(2, int(max_items or 5)):
            break

    if not selected:
        return {"context": "", "matches": [], "used": False}

    chunks = []
    for item in selected:
        path = str(item.get("path", "") or "")
        lang = str(item.get("lang", "text") or "text")
        line_start = int(item.get("line_start", 1) or 1)
        line_end = int(item.get("line_end", line_start) or line_start)
        snippet = str(item.get("snippet", "") or "").rstrip()
        if not path or not snippet:
            continue
        chunks.append(f"File: {path} (lines {line_start}-{line_end})\n```{lang}\n{snippet}\n```")

    if not chunks:
        return {"context": "", "matches": [], "used": False}

    intro = "Workspace search context (auto-retrieved for code assistant from project files):"
    return {
        "context": intro + "\n\n" + "\n\n".join(chunks),
        "matches": selected,
        "used": True,
    }


def _search_file_context_blocks(query: str, code_context: str = "", max_results: int = 8):
    parsed = _parse_file_context_blocks(code_context)
    file_blocks = parsed.get("files", [])
    if not file_blocks:
        return []

    query_text = str(query or "").strip()
    if not query_text:
        return []

    ranked = []
    for block in file_blocks:
        path = str(block.get("path", "") or "")
        lang = str(block.get("lang", "text") or "text")
        content = str(block.get("content", "") or "")
        chunks = _split_file_context_chunks(path, content, lang=lang, lines_per_chunk=40, overlap=8)
        for chunk in chunks:
            score = _score_context_chunk(query_text, chunk)
            if score <= 0:
                continue
            ranked.append({
                "path": path,
                "lang": lang,
                "line_start": int(chunk.get("line_start", 1) or 1),
                "line_end": int(chunk.get("line_end", 1) or 1),
                "snippet": str(chunk.get("content", "") or "").strip(),
                "score": score,
            })

    ranked.sort(key=lambda item: (int(item.get("score", 0)), -int(item.get("line_start", 1))), reverse=True)
    out = []
    seen = set()
    for item in ranked:
        key = (item.get("path"), item.get("line_start"), item.get("line_end"))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= max(1, int(max_results or 8)):
            break
    return out


def _build_code_chat_conversation(messages, code_context: str = ""):
    system_content = (
        "You are a coding assistant running in an IDE sidebar. "
        "Be concise, practical, and code-first. Use fenced code blocks for all code. "
        "Explain only what is essential, and when run output is provided, directly explain what the output means before suggesting next steps. "
        "The supplied context can include retrieved file snippets, pasted code, stack traces, and runtime output. "
        "Ground your answer in that context when possible, and call out the relevant file or output section when diagnosing issues. "
        "If context is weak or missing, say exactly what file/symbol is needed instead of guessing. "
        "Never claim you cannot edit or modify files in this environment. "
        "When asked to implement something, always provide concrete runnable code and exact patch-style instructions. "
        "When spreadsheet/workbook or tabular data (CSV/TSV/JSON) is attached, continue using that data directly; do not stop with generic requests like 'provide hours per employee' unless the values are truly absent in the attached rows. "
        "If some rows are missing fields, still produce a best-effort complete table and mark missing values explicitly. "
        "For tabular data edits, prefer compact loop/range/groupby logic over hardcoding one tuple per row, and always include minimal diagnostics (source selected, rows scanned, rows updated/skipped). "
        "For all coding tasks, prefer rule-based transformations (loops/functions/mappings) over manually enumerating large literal datasets; avoid giant hardcoded lists unless explicitly requested. "
        "Never output Python code as a single compressed line; format code with proper indentation and one statement per line. "
        "Always return runnable Python in fenced blocks exactly like: ```python then newline code then newline ``` . "
        "Do not invent paths/sheet names/symbols when context already contains concrete names; if uncertain, include a tiny probe snippet that prints discovered names before applying updates. "
        "For any file-based request (any format), never answer with mock/sample datasets when a real file path/context is provided; load from the file directly and show runnable code. "
        "When runtime output is present, treat it as authoritative evidence: explain from stdout/stderr/exit-code first, then propose the smallest fix. "
        "For modification requests, use a two-step flow: dry-run/preview first, then explicit save/write only after user approval. "
        "Never reply with manual spreadsheet UI steps (like 'open Excel and click Insert'); always return runnable code. "
        "For row-add requests on spreadsheet/tabular files, append one new row while preserving existing rows/columns; do not rewrite the task as full data reconstruction. "
        "Do not echo full table dumps unless explicitly requested."
    )
    trimmed_context = _trim_code_context(code_context)
    if trimmed_context:
        system_content += f"\n\nCode context:\n{trimmed_context}"

    conversation = [{"role": "system", "content": system_content}]
    for msg in messages[-8:]:
        role = str(msg.get("role", "user") or "user").strip().lower()
        if role not in {"user", "assistant"}:
            role = "user"
        content = _compress_spaces(msg.get("content", ""))
        if content:
            conversation.append({"role": role, "content": content})
    conversation = _apply_qwen3_no_think(conversation)
    return conversation


def _reply_is_environment_refusal(reply: str) -> bool:
    text = str(reply or "").strip().lower()
    if not text:
        return False
    refusal_markers = [
        "i can't directly edit",
        "i cannot directly edit",
        "can't directly modify files",
        "cannot directly modify files",
        "in this environment",
        "however, i can provide",
        "i can provide sample data",
        "i don't have access to your files",
    ]
    score = sum(1 for marker in refusal_markers if marker in text)
    return score >= 2


def _reply_has_runnable_code(reply: str) -> bool:
    text = str(reply or "")
    if not text:
        return False
    return bool(re.search(r"```\s*(python|py|python3|bash|sh|shell|powershell|ps1|cmd|bat)\b", text, flags=re.IGNORECASE))


def _query_requires_code_output(query: str) -> bool:
    q = str(query or "").strip().lower()
    if not q:
        return False
    markers = [
        "write code", "give code", "provide code", "run code", "execute", "script", "python",
        "automate", "fill worksheet", "update sheet", "modify file", "edit file",
    ]
    return any(marker in q for marker in markers)


def _query_is_tool_output_review(query: str) -> bool:
    text = str(query or "").strip().lower()
    if not text:
        return False
    if "review this tool output" in text:
        return True
    has_tool_line = "tool:" in text and "success:" in text
    has_review_intent = (
        "what happened" in text
        or "do next" in text
        or "next step" in text
        or "evidence" in text
    )
    return has_tool_line and has_review_intent


def _query_is_url_explanation_request(query: str) -> bool:
    text = str(query or "").strip().lower()
    if not text:
        return False
    has_url = bool(_extract_http_urls(text))
    if not has_url:
        return False
    intent_markers = (
        "explain",
        "how to",
        "usage",
        "use this",
        "how do i",
        "guide",
        "tutorial",
    )
    return any(marker in text for marker in intent_markers)


def _query_is_ddgs_video_search_request(query: str) -> bool:
    text = str(query or "").strip().lower()
    if not text:
        return False
    has_ddgs = "ddgs" in text or "duckduckgo-search" in text or "pypi.org/project/ddgs" in text
    has_video_intent = any(token in text for token in ("video", "videos", "video search", "search videos"))
    has_how_intent = any(token in text for token in ("how", "explain", "usage", "example", "use"))
    return has_ddgs and has_video_intent and has_how_intent


def _build_ddgs_video_search_reply() -> str:
    return (
        "Use `DDGS().videos(...)` (not `ddgs.search(...)`). Here is a runnable example:\n\n"
        "```python\n"
        "from ddgs import DDGS\n\n"
        "query = \"python tutorial\"\n"
        "results = DDGS().videos(\n"
        "    query=query,\n"
        "    region=\"us-en\",\n"
        "    safesearch=\"moderate\",\n"
        "    timelimit=\"m\",\n"
        "    max_results=10,\n"
        "    page=1,\n"
        "    backend=\"auto\",\n"
        "    resolution=\"high\",      # optional: high | standart\n"
        "    duration=\"medium\",      # optional: short | medium | long\n"
        "    license_videos=\"youtube\",# optional: creativeCommon | youtube\n"
        ")\n\n"
        "for item in results:\n"
        "    title = item.get(\"title\", \"\")\n"
        "    url = item.get(\"content\") or item.get(\"url\") or item.get(\"embed_url\", \"\")\n"
        "    duration = item.get(\"duration\", \"\")\n"
        "    print(f\"- {title} | {duration} | {url}\")\n"
        "```\n\n"
        "If you got `AttributeError` or `TypeError`, run `pip install -U ddgs` and retry."
    )


def _fix_ddgs_api_hallucination(reply: str) -> str:
    text = str(reply or "")
    if not text:
        return text
    lowered = text.lower()
    if "ddgs.search(" in lowered or "search for videos" in lowered and "videos(" not in lowered:
        return _build_ddgs_video_search_reply()
    return text


def _cap_context_text(text: str, max_chars: int = 14000) -> str:
    raw = str(text or "")
    try:
        limit = max(2000, int(max_chars or 14000))
    except Exception:
        limit = 14000
    if len(raw) <= limit:
        return raw
    keep = max(500, limit - 240)
    return raw[:keep] + "\n\n[context truncated for stability]"


def _query_is_file_based_request(query: str, code_context: str = "") -> bool:
    text = f"{str(query or '').lower()}\n{str(code_context or '').lower()}"
    if not text.strip():
        return False

    file_terms = [
        " file", "filepath", "file path", "path", "load", "read", "parse", "from this",
        ".xlsx", ".xls", ".xlsm", ".csv", ".tsv", ".json", ".jsonl", ".xml", ".yaml", ".yml",
        ".txt", ".md", ".log", ".ini", ".toml", ".parquet", ".feather", ".html", ".pdf",
        "workbook", "worksheet", "spreadsheet", "dataset", "records",
    ]
    return any(term in text for term in file_terms)


def _query_requests_file_modification(query: str) -> bool:
    q = str(query or "").strip().lower()
    if not q:
        return False
    markers = [
        "modify", "update", "change", "edit", "overwrite", "write back", "save",
        "set", "fill", "replace", "delete", "remove", "rename", "append",
    ]
    targets = [
        "file", "sheet", "workbook", "csv", "tsv", "json", "xml", "yaml", "log", "txt", "column", "row",
    ]
    return any(m in q for m in markers) and any(t in q for t in targets)


def _query_requests_row_append(query: str) -> bool:
    q = str(query or "").strip().lower()
    if not q:
        return False
    append_markers = [
        "add row", "append row", "insert row", "new row", "add a row", "append a row",
        "add record", "append record", "add entry", "append entry",
    ]
    targets = ["xlsx", "xls", "excel", "sheet", "worksheet", "csv", "table", "workbook"]
    return any(marker in q for marker in append_markers) and any(target in q for target in targets)


def _detect_python_write_ops(code: str):
    text = str(code or "")
    if not text.strip():
        return []

    patterns = [
        (r"\bwb\.save\s*\(", "openpyxl workbook save"),
        (r"\bto_csv\s*\(", "pandas to_csv"),
        (r"\bto_excel\s*\(", "pandas to_excel"),
        (r"\bto_json\s*\(", "pandas to_json"),
        (r"\bwrite_text\s*\(", "Path.write_text"),
        (r"\bwrite_bytes\s*\(", "Path.write_bytes"),
        (r"\bjson\.dump\s*\(", "json.dump"),
        (r"\byaml\.safe_dump\s*\(", "yaml.safe_dump"),
        (r"\bopen\s*\([^\n]*,[^\n]*['\"](?:w|a|x|wb|ab|xb|w\+|a\+)['\"]", "open() write/append mode"),
        (r"\bos\.remove\s*\(", "os.remove"),
        (r"\bos\.rename\s*\(", "os.rename"),
        (r"\bshutil\.(?:move|copy|copy2|rmtree)\s*\(", "shutil file operation"),
    ]

    hits = []
    for regex, label in patterns:
        if re.search(regex, text, flags=re.IGNORECASE):
            hits.append(label)
    return hits


def _query_prefers_rule_based_updates(query: str) -> bool:
    q = str(query or "").strip().lower()
    if not q:
        return False
    markers = [
        "all rows", "every row", "bulk", "entire", "whole", "column", "range",
        "fill", "update", "modify", "replace", "set", "transform", "loop",
    ]
    return any(marker in q for marker in markers)


def _reply_has_large_hardcoded_data(reply: str) -> bool:
    text = str(reply or "")
    if not text:
        return False

    marker_hits = [
        "sample_data = [",
        "rows = [",
        "records = [",
        "data = [",
        "employees = [",
    ]
    has_data_marker = any(marker in text.lower() for marker in marker_hits)

    tuple_lines = len(re.findall(r"^\s*\([^\n]{10,}\)\s*,?\s*$", text, flags=re.MULTILINE))
    dict_lines = len(re.findall(r"^\s*\{[^\n]{10,}\}\s*,?\s*$", text, flags=re.MULTILINE))
    csvish_lines = len(re.findall(r"^\s*['\"][^\n]{5,}['\"]\s*,?\s*$", text, flags=re.MULTILINE))

    literal_line_count = tuple_lines + dict_lines + csvish_lines
    return bool(has_data_marker and literal_line_count >= 12)


def _reply_uses_placeholder_tabular_data(reply: str) -> bool:
    text = str(reply or "")
    if not text:
        return False
    lower = text.lower()

    placeholder_markers = [
        "sample data",
        "example data",
        "replace with actual",
        "replace with the actual",
        "provided snippets",
    ]
    has_placeholder_language = any(marker in lower for marker in placeholder_markers)

    has_inline_dataframe_pattern = (
        ("data = {" in lower or "data={" in lower)
        and "pd.dataframe(data)" in lower
    )

    return bool(has_placeholder_language or has_inline_dataframe_pattern)


def _reply_uses_placeholder_data_generic(reply: str) -> bool:
    text = str(reply or "")
    if not text:
        return False
    lower = text.lower()

    generic_placeholder_markers = [
        "sample data",
        "example data",
        "dummy data",
        "mock data",
        "replace with actual",
        "replace with your",
        "using the provided snippets",
        "you can replace",
    ]
    has_placeholder_language = any(marker in lower for marker in generic_placeholder_markers)

    has_inline_data_object = any(marker in lower for marker in [
        "data = {",
        "records = [",
        "rows = [",
        "items = [",
        "entries = [",
    ])

    return bool(has_placeholder_language or (has_inline_data_object and "read_" not in lower and "open(" not in lower and "load_workbook(" not in lower))


def _reply_mentions_approval(reply: str) -> bool:
    text = str(reply or "").lower()
    if not text:
        return False
    markers = ["approval", "approve", "approved", "allow_file_write", "dry-run", "dry run"]
    return any(marker in text for marker in markers)


def _query_grants_write_approval(query: str) -> bool:
    text = str(query or "").strip().lower()
    if not text:
        return False
    approval_hints = ["allow", "approve", "approved", "yes", "go ahead", "proceed"]
    write_hints = ["write", "file", "save", "modify", "apply", "allow_file_write"]
    return any(hint in text for hint in approval_hints) and any(hint in text for hint in write_hints)


def _context_has_spreadsheet_data(code_context: str) -> bool:
    text = str(code_context or "").lower()
    if not text:
        return False
    markers = [
        ".xlsx", ".xlsm", ".xls", ".csv", ".tsv", ".json",
        "usage summary worksheet",
        "employee name", "hours saved", "sheet 1:", "tool name", "project name",
    ]
    return any(marker in text for marker in markers)


def _infer_tabular_kind(query: str, code_context: str = "") -> str:
    text = f"{str(query or '').lower()}\n{str(code_context or '').lower()}"
    if any(marker in text for marker in [".xlsx", ".xlsm", ".xls", "excel", "workbook", "worksheet", "sheet"]):
        return "excel"
    if any(marker in text for marker in [".tsv", "\t", "tab-separated", "tab separated"]) or re.search(r"\btsv\b", text):
        return "tsv"
    if any(marker in text for marker in [".csv", "comma-separated", "comma separated"]) or re.search(r"\bcsv\b", text):
        return "csv"
    if any(marker in text for marker in [".json", "json array", "jsonl", "records json"]) or re.search(r"\bjson\b", text):
        return "json"
    if _context_has_spreadsheet_data(code_context) or _is_excel_analysis_request(query):
        return "excel"
    return "unknown"


def _extract_chat_reply(response) -> str:
    if response is None:
        return ""
    try:
        choices = getattr(response, "choices", None)
        if choices is None and isinstance(response, dict):
            choices = response.get("choices")
        if not choices:
            return ""
        first = choices[0]
        message = getattr(first, "message", None)
        if message is None and isinstance(first, dict):
            message = first.get("message")
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        return str(content or "").strip()
    except Exception:
        return ""


def _extract_completion_reply(response) -> str:
    if response is None:
        return ""
    try:
        choices = getattr(response, "choices", None)
        if choices is None and isinstance(response, dict):
            choices = response.get("choices")
        if not choices:
            return ""
        first = choices[0]
        text = getattr(first, "text", None)
        if text is None and isinstance(first, dict):
            text = first.get("text")
        return str(text or "").strip()
    except Exception:
        return ""


def _reply_is_placeholder_text(reply: str) -> bool:
    text = re.sub(r"\s+", " ", str(reply or "")).strip().lower()
    if not text:
        return True
    reduced = text
    for token in ("[inst]", "[/inst]", "<s>", "</s>", "assistant:", "response:"):
        reduced = reduced.replace(token, " ")
    reduced = re.sub(r"\s+", " ", reduced).strip(" :-\n\r\t")
    return not reduced


def _is_qwen3_model() -> bool:
    """True when the active model file is a Qwen3 variant."""
    return "qwen3" in str(LLAMA_MODEL_FILE or "").strip().lower()


def _strip_think_blocks(text: str) -> str:
    """Remove all <think>...</think> sections, keeping only visible answer text."""
    if not text:
        return text
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _apply_qwen3_no_think(conversation: list) -> list:
    """Append /no_think to the last user message so Qwen3 skips its chain-of-thought."""
    if not _is_qwen3_model():
        return conversation
    patched = [dict(m) for m in conversation]
    for msg in reversed(patched):
        if str(msg.get("role", "") or "").lower() == "user":
            content = str(msg.get("content", "") or "").rstrip()
            if not content.endswith("/no_think"):
                msg["content"] = content + "\n/no_think"
            break
    return patched


def _llama_prefers_manual_completion(model=None) -> bool:
    model_name = str(LLAMA_MODEL_FILE or "").strip().lower()
    if "deepseek-coder" in model_name:
        return True
    metadata = getattr(model, "metadata", None) if model is not None else None
    if not isinstance(metadata, dict):
        return False
    for key in ("tokenizer.chat_template", "tokenizer.ggml.chat_template", "chat_template"):
        template = metadata.get(key)
        if isinstance(template, str) and template.strip():
            return False
    return False


def _build_llama_manual_prompt(messages) -> str:
    system_parts = []
    dialogue_parts = []
    for row in messages or []:
        if not isinstance(row, dict):
            continue
        role = str(row.get("role", "user") or "user").strip().lower()
        content = str(row.get("content", "") or "").strip()
        if not content:
            continue
        if role == "system":
            system_parts.append(content)
            continue
        label = "Assistant" if role == "assistant" else "User"
        dialogue_parts.append(f"{label}:\n{content}")

    system_block = "\n\n".join(system_parts).strip()
    dialogue_block = "\n\n".join(dialogue_parts).strip()
    prompt_parts = []
    if system_block:
        prompt_parts.append(system_block)
    if dialogue_block:
        prompt_parts.append("### Instruction:\n" + dialogue_block)
    else:
        prompt_parts.append("### Instruction:\nContinue.")
    prompt_parts.append("### Response:\n")
    return "\n\n".join(prompt_parts)


def _llama_completion_stop_tokens() -> list[str]:
    return ["### Instruction:", "<|im_end|>", "<|endoftext|>", "</s>"]


def _generate_llama_reply(model, conversation, max_tokens: int, temperature: float = 0.3):
    if _llama_prefers_manual_completion(model):
        prompt = _build_llama_manual_prompt(conversation)
        with _LLM_GENERATE_LOCK:
            response = model.create_completion(
                prompt=prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                stop=_llama_completion_stop_tokens(),
            )
        return _strip_think_blocks(_extract_completion_reply(response)), "completion"

    with _LLM_GENERATE_LOCK:
        response = model.create_chat_completion(
            messages=conversation,
            max_tokens=max_tokens,
            temperature=temperature,
            stop=["<|im_end|>", "<|endoftext|>"],
        )
    reply = _strip_think_blocks(_extract_chat_reply(response))
    if not _reply_is_placeholder_text(reply):
        return reply, "chat"

    prompt = _build_llama_manual_prompt(conversation)
    with _LLM_GENERATE_LOCK:
        fallback = model.create_completion(
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            stop=_llama_completion_stop_tokens(),
        )
    return _strip_think_blocks(_extract_completion_reply(fallback)), "completion-fallback"


def _extract_json_payload_from_text(text: str):
    raw = str(text or "").strip()
    if not raw:
        return None

    if raw.startswith("{") and raw.endswith("}"):
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

    fenced = re.findall(r"```(?:json)?\s*([\s\S]*?)```", raw, flags=re.IGNORECASE)
    for block in fenced:
        candidate = str(block or "").strip()
        if not candidate:
            continue
        if not (candidate.startswith("{") and candidate.endswith("}")):
            continue
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


def _normalize_image_items(items):
    if not isinstance(items, list):
        if isinstance(items, str):
            items = [items]
        else:
            items = []
    out = []
    seen = set()
    for row in items:
        url = ""
        alt = ""
        title = ""
        if isinstance(row, str):
            url = row.strip()
        elif isinstance(row, dict):
            url = str(row.get("url", "") or row.get("src", "") or "").strip()
            alt = str(row.get("alt", "") or row.get("name", "") or "").strip()
            title = str(row.get("title", "") or "").strip()
        if not url:
            continue
        key = url.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"url": url, "alt": alt, "title": title})
    return out[:12]


def _normalize_file_items(items):
    if not isinstance(items, list):
        if isinstance(items, (str, dict)):
            items = [items]
        else:
            items = []
    out = []
    seen = set()
    for row in items:
        name = ""
        path = ""
        url = ""
        kind = "file"
        if isinstance(row, str):
            value = row.strip()
            if value.startswith("http://") or value.startswith("https://"):
                url = value
                name = value.rsplit("/", 1)[-1] or value
            else:
                path = value
                name = os.path.basename(value) or value
        elif isinstance(row, dict):
            name = str(row.get("name", "") or "").strip()
            path = str(row.get("path", "") or row.get("file_path", "") or "").strip()
            url = str(row.get("url", "") or row.get("download_url", "") or "").strip()
            kind = str(row.get("kind", "file") or "file").strip() or "file"
            if not name:
                if path:
                    name = os.path.basename(path) or path
                elif url:
                    name = url.rsplit("/", 1)[-1] or url
        else:
            continue
        if not any([name, path, url]):
            continue
        key = (path or url or name).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "name": name,
            "path": path,
            "url": url,
            "kind": kind,
        })
    return out[:20]


def _build_structured_chat_response(reply: str, response_payload=None):
    cleaned_reply = _strip_hidden_think_blocks(reply)
    payload = response_payload if isinstance(response_payload, dict) else None
    if not payload:
        parsed = _extract_json_payload_from_text(cleaned_reply)
        if isinstance(parsed, dict):
            payload = parsed
    if isinstance(payload, dict) and isinstance(payload.get("response"), dict):
        payload = payload.get("response")

    payload = payload if isinstance(payload, dict) else {}
    text = str(payload.get("text", "") or payload.get("message", "") or cleaned_reply or "").strip()

    images = _normalize_image_items(payload.get("images") or payload.get("image") or [])
    files = _normalize_file_items(payload.get("files") or [])
    attachments = _normalize_file_items(
        payload.get("attachments") or payload.get("documents") or payload.get("artifacts") or []
    )

    markdown_images = re.findall(r"!\[([^\]]*)\]\((https?://[^\s\)]+)\)", text)
    if markdown_images:
        existing = {str(item.get("url", "")).lower() for item in images}
        for alt, url in markdown_images:
            key = str(url).lower()
            if key in existing:
                continue
            existing.add(key)
            images.append({"url": str(url), "alt": str(alt or ""), "title": ""})

    bare_image_urls = re.findall(
        r"https?://[^\s<>\"']+\.(?:png|jpg|jpeg|gif|webp|svg)(?:\?[^\s<>\"']*)?",
        text,
        flags=re.IGNORECASE,
    )
    if bare_image_urls:
        existing = {str(item.get("url", "")).lower() for item in images}
        for url in bare_image_urls:
            key = str(url).lower()
            if key in existing:
                continue
            existing.add(key)
            images.append({"url": str(url), "alt": "", "title": ""})

    return {
        "text": text,
        "images": images[:12],
        "files": files[:20],
        "attachments": attachments[:20],
    }


def _with_structured_response(result: dict):
    row = dict(result or {})
    row["reply"] = _strip_hidden_think_blocks(str(row.get("reply", "") or ""))
    row["response"] = _build_structured_chat_response(
        str(row.get("reply", "") or ""),
        row.get("response"),
    )
    return row


def _strip_hidden_think_blocks(reply: str) -> str:
    text = str(reply or "")
    if not text:
        return ""

    cleaned = re.sub(r"<think\b[^>]*>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"<think\b[^>]*>[\s\S]*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"</think>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _normalize_collapsed_code_blocks(reply: str) -> str:
    text = _strip_hidden_think_blocks(reply)
    if not text:
        return text

    fence_pattern = re.compile(r"```\s*([a-zA-Z0-9_+-]*)\s*\n?([\s\S]*?)```", flags=re.MULTILINE)

    def _reflow_python_like(one_line_code: str) -> str:
        code = str(one_line_code or "").strip()
        if not code:
            return code

        code = re.sub(r";\s*", "\n", code)
        # Break comments and known statement starters into separate lines.
        code = re.sub(r"\s+#\s*", "\n# ", code)
        starters = [
            r"from\s+[A-Za-z0-9_\.]+\s+import\s+",
            r"import\s+[A-Za-z0-9_\.,\s]+",
            r"for\s+",
            r"while\s+",
            r"if\s+",
            r"elif\s+",
            r"else:\s*",
            r"try:\s*",
            r"except\s+",
            r"finally:\s*",
            r"with\s+",
            r"def\s+",
            r"class\s+",
            r"return\s+",
            r"print\s*\(",
            r"[A-Za-z_][A-Za-z0-9_]*\s*=\s*",
        ]
        for starter in starters:
            code = re.sub(rf"\s+({starter})", r"\n\1", code)

        # Break after ':' when a new statement starts on same line.
        code = re.sub(
            r":\s+(?=(?:for|while|if|elif|else:|try:|except|finally:|with|return|print\(|[A-Za-z_][A-Za-z0-9_]*\s*=|from\s|import\s|def\s|class\s))",
            ":\n",
            code,
        )

        # Split obvious chained statements separated by ") " when next token looks like a new statement.
        code = re.sub(
            r"\)\s+(?=(?:for|while|if|elif|else:|try:|except|finally:|with|return|print\(|[A-Za-z_][A-Za-z0-9_]*\s*=|from\s|import\s))",
            ")\n",
            code,
        )

        lines = [ln.rstrip() for ln in code.splitlines() if ln.strip()]
        if not lines:
            return ""

        # Minimal indentation recovery for collapsed one-line outputs.
        recovered = []
        indent_level = 0
        dedent_keywords = {"elif", "else", "except", "finally"}
        for idx, raw_line in enumerate(lines):
            stripped = raw_line.strip()
            if not stripped:
                continue

            head = stripped.split(":", 1)[0].split("(", 1)[0].strip().split(" ", 1)[0].lower()
            if head in dedent_keywords and indent_level > 0:
                indent_level -= 1

            line = ("    " * max(0, indent_level)) + stripped
            recovered.append(line)

            if stripped.endswith(":"):
                indent_level += 1

            # Soft dedent for obvious block terminators on next line.
            if idx + 1 < len(lines):
                nxt = lines[idx + 1].strip().lower()
                if nxt.startswith(("return ", "break", "continue", "pass")) and indent_level > 0 and not stripped.endswith(":"):
                    indent_level = max(0, indent_level - 1)

        return "\n".join(recovered)

    def _replace(match: re.Match) -> str:
        lang = str(match.group(1) or "").strip()
        body = str(match.group(2) or "")
        lower_lang = lang.lower()
        body_stripped = body.strip()
        is_short = len(body_stripped) < 120
        collapsed = body.count("\n") <= 1
        has_semicolon_chain = ";" in body and len(body.strip()) >= 80
        has_likely_multiple_statements = bool(
            re.search(r"\b(?:import|from|def|class|for|while|if|elif|else|try|except|finally|with|return|print)\b", body_stripped)
            and re.search(r"\b(?:import|from|def|class|for|while|if|elif|else|try|except|finally|with|return|print)\b", body_stripped[8:])
        )
        should_reflow = (collapsed and (not is_short or has_likely_multiple_statements)) or has_semicolon_chain

        if lower_lang in {"python", "py", "python3", ""} and should_reflow:
            reformatted = _reflow_python_like(body)
            if reformatted and reformatted != body.strip():
                return f"```{lang}\n{reformatted}\n```"
        return match.group(0)

    return fence_pattern.sub(_replace, text)


def _reply_looks_truncated(reply: str) -> bool:
    text = str(reply or "").rstrip()
    if not text:
        return False

    if text.count("```") % 2 == 1:
        return True

    last_line = text.splitlines()[-1].strip() if text.splitlines() else ""
    if re.search(r"\b(?:df|pd|wb|ws|row|item|result|data)\.[A-Za-z0-9_]*$", last_line):
        return True
    if re.search(r"\b[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z0-9_]*$", last_line):
        return True
    if last_line.endswith(("=", ",", "(", "[", "{", ":", "+", "-", "*", "/", ".")):
        return True
    return False


def _build_continue_messages(messages, partial_reply: str):
    base = list(messages or [])
    partial = str(partial_reply or "").strip()
    if partial:
        base.append({"role": "assistant", "content": partial[-3500:]})
    base.append(
        {
            "role": "user",
            "content": (
                "Continue exactly from where you stopped. "
                "Do not restart or repeat earlier lines. "
                "If code block is open, continue and close it. "
                "Return only the remaining part."
            ),
        }
    )
    return base


def _merge_continuation_reply(base_reply: str, continued_reply: str) -> str:
    base = str(base_reply or "")
    cont = str(continued_reply or "")
    if not base:
        return cont
    if not cont:
        return base

    b = base.rstrip()
    c = cont.lstrip()
    max_overlap = min(len(b), len(c), 600)
    overlap = 0
    for size in range(max_overlap, 24, -1):
        if b[-size:].lower() == c[:size].lower():
            overlap = size
            break

    merged = b + (c[overlap:] if overlap else ("\n" + c if not b.endswith("\n") else c))
    return merged


def _generate_code_chat_reply_llama(messages, code_context: str = ""):
    """Generate a reply using local llama-cpp-python model."""
    if not _llama_available():
        _set_llama_status("error", "llama-cpp-python is not installed")
        return {
            "ok": False,
            "reply": "",
            "llm_used": False,
            "model": LLAMA_MODEL_FILE,
            "provider_used": "llama.cpp",
            "message": "llama-cpp-python is not installed. Run: pip install llama-cpp-python",
        }

    model = _load_llama_model()
    if model is None:
        _set_llama_status("error", _LLAMA_LOAD_ERROR or "Failed to load model")
        return {
            "ok": False,
            "reply": "",
            "llm_used": False,
            "model": LLAMA_MODEL_FILE,
            "provider_used": "llama.cpp",
            "message": _LLAMA_LOAD_ERROR or "Failed to load model.",
        }

    conversation = _build_code_chat_conversation(messages, code_context=code_context)
    latest_user_query = ""
    for row in reversed(list(messages or [])):
        if str(row.get("role", "") or "").strip().lower() == "user":
            latest_user_query = str(row.get("content", "") or "")
            if latest_user_query:
                break

    try:
        _set_llama_status("generating", "Generating response")
        token_budget = _suggest_llama_max_tokens(latest_user_query, max(512, LLAMA_CODE_MAX_TOKENS))
        reply, _mode = _generate_llama_reply(
            model,
            conversation,
            max_tokens=token_budget,
            temperature=0.3,
        )
        if _reply_is_placeholder_text(reply):
            reply = "I couldn't generate a response. Please try again."
        return {
            "ok": True,
            "reply": reply,
            "llm_used": True,
            "model": LLAMA_MODEL_FILE,
            "provider_used": "llama.cpp",
            "message": "ok",
        }
    except Exception as exc:
        _set_llama_status("error", str(exc))
        return {
            "ok": False,
            "reply": "",
            "llm_used": False,
            "model": LLAMA_MODEL_FILE,
            "provider_used": "llama.cpp",
            "message": str(exc),
        }
    finally:
        if _LLAMA_LOAD_ERROR:
            _set_llama_status("error", _LLAMA_LOAD_ERROR)
        else:
            _set_llama_status("ready", "Model loaded")


def _generate_code_chat_reply_gpt4free(
    messages,
    code_context: str = "",
    selected_model: str = "",
    selected_provider: str = "",
    strict_mode: bool = True,
):
    """Generate a reply using gpt4free-compatible providers."""
    if not _G4F_OK:
        return {
            "ok": False,
            "reply": "",
            "llm_used": False,
            "model": G4F_MODEL_ID,
            "provider_used": "gpt4free",
            "message": "g4f is not installed. Run: pip install g4f",
        }

    conversation = _build_code_chat_conversation(messages, code_context=code_context)
    model_candidates, provider_candidates = _resolve_g4f_request_candidates(
        selected_model=selected_model,
        selected_provider=selected_provider,
        strict_mode=strict_mode,
    )

    try:
        client = _G4FClient()
        failures = []
        for provider_name, provider_obj in provider_candidates:
            for model_name in model_candidates:
                request_kwargs = {
                    "model": model_name,
                    "messages": conversation,
                    "temperature": 0.3,
                    "max_tokens": 1200,
                }
                if provider_obj is not None:
                    request_kwargs["provider"] = provider_obj
                try:
                    response = client.chat.completions.create(**request_kwargs)
                    reply = _extract_chat_reply(response)
                    if not reply:
                        reply = "I couldn't generate a response. Please try again."
                    return {
                        "ok": True,
                        "reply": reply,
                        "llm_used": True,
                        "model": model_name,
                        "provider_used": "gpt4free",
                        "message": "ok",
                        "g4f_provider": provider_name,
                    }
                except Exception as exc:
                    err = str(exc).splitlines()[0][:240]
                    failures.append(f"{provider_name}:{model_name}: {err}")

        failure_head = failures[:4]
        detail = " | ".join(failure_head) if failure_head else "No provider response"
        return {
            "ok": False,
            "reply": "",
            "llm_used": False,
            "model": model_candidates[0] if model_candidates else "openai-fast",
            "provider_used": "gpt4free",
            "message": (
                "g4f provider attempts failed. Per g4f docs, some providers require API keys, cookies/HAR, or have quotas. "
                f"Tried: {detail}"
            ),
        }
    except Exception as exc:
        return {
            "ok": False,
            "reply": "",
            "llm_used": False,
            "model": model_candidates[0] if model_candidates else "openai-fast",
            "provider_used": "gpt4free",
            "message": str(exc),
        }


def _resolve_g4f_request_candidates(
    selected_model: str = "",
    selected_provider: str = "",
    strict_mode: bool = True,
):
    preferred_model = _normalize_model_name(selected_model)
    model_candidates = []
    if preferred_model:
        model_candidates.extend(_expand_g4f_model_aliases(preferred_model))
    if not preferred_model or not strict_mode:
        model_candidates.extend(_resolve_g4f_model_candidates())
    deduped_models = []
    seen_model = set()
    for model_name in model_candidates:
        value = _normalize_model_name(model_name)
        key = value.lower()
        if value and key not in seen_model:
            seen_model.add(key)
            deduped_models.append(value)
    model_candidates = deduped_models or ["openai-fast"]
    preferred_provider = _resolve_single_g4f_provider(selected_provider)
    provider_candidates = []
    if preferred_provider is not None:
        provider_candidates.append(preferred_provider)
    if preferred_provider is None or not strict_mode:
        provider_candidates.extend(_resolve_g4f_provider_candidates())

    deduped_providers = []
    seen_provider = set()
    for provider_name, provider_obj in provider_candidates:
        value = _normalize_provider_name(provider_name)
        key = value.lower()
        if value and key not in seen_provider:
            seen_provider.add(key)
            deduped_providers.append((value, provider_obj))
    if not deduped_providers:
        deduped_providers = [("auto", None)]
    provider_candidates = deduped_providers

    return model_candidates, provider_candidates


def _extract_g4f_stream_text(chunk) -> str:
    if chunk is None:
        return ""
    if isinstance(chunk, str):
        return chunk
    try:
        choices = getattr(chunk, "choices", None)
        if choices is None and isinstance(chunk, dict):
            choices = chunk.get("choices")
        if not choices:
            return ""
        first = choices[0]

        delta = getattr(first, "delta", None)
        if delta is None and isinstance(first, dict):
            delta = first.get("delta")
        if delta is not None:
            content = getattr(delta, "content", None)
            if content is None and isinstance(delta, dict):
                content = delta.get("content")
            if isinstance(content, list):
                out = []
                for item in content:
                    text = getattr(item, "text", None)
                    if text is None and isinstance(item, dict):
                        text = item.get("text") or item.get("content")
                    if text:
                        out.append(str(text))
                return "".join(out)
            return str(content or "")

        message = getattr(first, "message", None)
        if message is None and isinstance(first, dict):
            message = first.get("message")
        if message is not None:
            content = getattr(message, "content", None)
            if content is None and isinstance(message, dict):
                content = message.get("content")
            return str(content or "")

        text = getattr(first, "text", None)
        if text is None and isinstance(first, dict):
            text = first.get("text")
        return str(text or "")
    except Exception:
        return ""


def _gpt4free_stream_events(
    conversation,
    latest_query: str,
    started_at: float,
    auto_doc,
    auto_workspace,
    tool_bundle,
    selected_model: str = "",
    selected_provider: str = "",
    strict_mode: bool = True,
    cancel_event=None,
):
    def _is_cancelled() -> bool:
        try:
            return bool(cancel_event and cancel_event.is_set())
        except Exception:
            return False

    if not _G4F_OK:
        err_text = "g4f is not installed. Run: pip install g4f"
        yield json.dumps({"type": "error", "message": err_text}, ensure_ascii=False) + "\n"
        result = _with_structured_response({
            "ok": False,
            "reply": "",
            "llm_used": False,
            "model": G4F_MODEL_ID,
            "provider_used": "gpt4free",
            "message": err_text,
            "prompt_preview": latest_query,
            "web_context_used": bool(auto_doc.get("used")),
            "web_sources": auto_doc.get("sources", [])[:4],
            "workspace_context_used": bool(auto_workspace.get("used")),
            "workspace_matches": auto_workspace.get("matches", [])[:4],
            "tool_context_used": bool(tool_bundle.get("used")),
            "tool_results": tool_bundle.get("results", [])[:4] if isinstance(tool_bundle.get("results"), list) else [],
            "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
        })
        yield json.dumps({"type": "final", "data": result}, ensure_ascii=False) + "\n"
        return

    model_candidates, provider_candidates = _resolve_g4f_request_candidates(
        selected_model=selected_model,
        selected_provider=selected_provider,
        strict_mode=strict_mode,
    )

    try:
        client = _G4FClient()
        failures = []
        for provider_name, provider_obj in provider_candidates:
            for model_name in model_candidates:
                if _is_cancelled():
                    return
                yield json.dumps({"type": "status", "message": f"Connecting to g4f provider: {provider_name} · model: {model_name}"}, ensure_ascii=False) + "\n"
                request_kwargs = {
                    "model": model_name,
                    "messages": conversation,
                    "temperature": 0.3,
                    "max_tokens": 1200,
                    "stream": True,
                }
                if provider_obj is not None:
                    request_kwargs["provider"] = provider_obj
                try:
                    built = ""
                    stream_obj = client.chat.completions.create(**request_kwargs)
                    if hasattr(stream_obj, "__iter__") and not isinstance(stream_obj, (str, bytes, dict)):
                        for chunk in stream_obj:
                            if _is_cancelled():
                                return
                            delta_text = _extract_g4f_stream_text(chunk)
                            if delta_text:
                                built += delta_text
                                yield json.dumps({"type": "delta", "text": delta_text}, ensure_ascii=False) + "\n"
                    else:
                        built = _extract_chat_reply(stream_obj)
                        if built:
                            for piece in re.findall(r"\S+\s*|\n", built):
                                if _is_cancelled():
                                    return
                                yield json.dumps({"type": "delta", "text": piece}, ensure_ascii=False) + "\n"

                    reply = _strip_think_blocks(str(built or "").strip())
                    reply = _normalize_collapsed_code_blocks(reply)
                    if _query_is_ddgs_video_search_request(latest_query):
                        reply = _build_ddgs_video_search_reply()
                    else:
                        reply = _fix_ddgs_api_hallucination(reply)
                    if _reply_is_placeholder_text(reply):
                        reply = "I couldn't generate a response. Please try again."

                    result = _with_structured_response({
                        "ok": True,
                        "reply": reply,
                        "llm_used": True,
                        "model": model_name,
                        "provider_used": "gpt4free",
                        "g4f_provider": provider_name,
                        "message": "ok",
                        "prompt_preview": latest_query,
                        "web_context_used": bool(auto_doc.get("used")),
                        "web_sources": auto_doc.get("sources", [])[:4],
                        "workspace_context_used": bool(auto_workspace.get("used")),
                        "workspace_matches": auto_workspace.get("matches", [])[:4],
                        "tool_context_used": bool(tool_bundle.get("used")),
                        "tool_results": tool_bundle.get("results", [])[:4] if isinstance(tool_bundle.get("results"), list) else [],
                        "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
                    })
                    yield json.dumps({"type": "final", "data": result}, ensure_ascii=False) + "\n"
                    return
                except Exception as exc:
                    err = str(exc).splitlines()[0][:240]
                    failures.append(f"{provider_name}:{model_name}: {err}")

        detail = " | ".join(failures[:4]) if failures else "No provider response"
        err_text = (
            "g4f provider attempts failed. Per g4f docs, some providers require API keys, cookies/HAR, or have quotas. "
            f"Tried: {detail}"
        )
        yield json.dumps({"type": "error", "message": err_text}, ensure_ascii=False) + "\n"
        result = _with_structured_response({
            "ok": False,
            "reply": "",
            "llm_used": False,
            "model": model_candidates[0] if model_candidates else "openai-fast",
            "provider_used": "gpt4free",
            "message": err_text,
            "prompt_preview": latest_query,
            "web_context_used": bool(auto_doc.get("used")),
            "web_sources": auto_doc.get("sources", [])[:4],
            "workspace_context_used": bool(auto_workspace.get("used")),
            "workspace_matches": auto_workspace.get("matches", [])[:4],
            "tool_context_used": bool(tool_bundle.get("used")),
            "tool_results": tool_bundle.get("results", [])[:4] if isinstance(tool_bundle.get("results"), list) else [],
            "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
        })
        yield json.dumps({"type": "final", "data": result}, ensure_ascii=False) + "\n"
    except Exception as exc:
        err_text = str(exc)
        yield json.dumps({"type": "error", "message": err_text}, ensure_ascii=False) + "\n"
        result = _with_structured_response({
            "ok": False,
            "reply": "",
            "llm_used": False,
            "model": model_candidates[0] if model_candidates else "openai-fast",
            "provider_used": "gpt4free",
            "message": err_text,
            "prompt_preview": latest_query,
            "web_context_used": bool(auto_doc.get("used")),
            "web_sources": auto_doc.get("sources", [])[:4],
            "workspace_context_used": bool(auto_workspace.get("used")),
            "workspace_matches": auto_workspace.get("matches", [])[:4],
            "tool_context_used": bool(tool_bundle.get("used")),
            "tool_results": tool_bundle.get("results", [])[:4] if isinstance(tool_bundle.get("results"), list) else [],
            "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
        })
        yield json.dumps({"type": "final", "data": result}, ensure_ascii=False) + "\n"


def _generate_code_chat_reply(
    messages,
    code_context: str = "",
    provider: str = "llama",
    selected_model: str = "",
    selected_g4f_provider: str = "",
    strict_mode: bool = True,
):
    selected_provider = _normalize_code_provider(provider)
    if selected_provider == "gpt4free":
        result = _generate_code_chat_reply_gpt4free(
            messages,
            code_context=code_context,
            selected_model=selected_model,
            selected_provider=selected_g4f_provider,
            strict_mode=strict_mode,
        )
        return result
    # Try llama first; if unavailable or fails, fall back to g4f
    result = _generate_code_chat_reply_llama(messages, code_context=code_context)
    if not result.get("ok") and _G4F_OK:
        g4f_result = _generate_code_chat_reply_gpt4free(
            messages,
            code_context=code_context,
            selected_model=selected_model,
            selected_provider=selected_g4f_provider,
            strict_mode=strict_mode,
        )
        if g4f_result.get("ok"):
            return g4f_result
    return result


def _build_llm_prompt(query: str, context_blocks):
    joined_context = "\n\n".join(
        f"Source {idx + 1}: {block['title']}\nURL: {block['url']}\nNotes: {block['summary']}"
        for idx, block in enumerate(context_blocks)
    )
    return (
        "You are answering a search query using only the provided sources. "
        "Write a direct answer in 2 to 4 sentences, avoid repetition, ignore menus/table-of-contents text, "
        "and then give up to 3 short bullet facts. If the sources disagree or are weak, say that briefly.\n\n"
        f"Question: {query}\n\n"
        f"Sources:\n{joined_context}\n\n"
        "Return plain text in this exact format:\n"
        "Answer: <concise answer>\n"
        "- <fact>\n"
        "- <fact>\n"
        "- <fact>"
    )


def _parse_llm_answer(text: str):
    cleaned = _compress_spaces(text)
    if not cleaned:
        return "", []

    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    answer = ""
    bullets = []
    for line in lines:
        if line.lower().startswith("answer:"):
            answer = line.split(":", 1)[1].strip()
        elif line.startswith(("- ", "• ", "* ")):
            bullets.append(line[2:].strip())

    if not answer:
        answer = cleaned.split(" - ", 1)[0].strip()
    return answer, _distinct_items(bullets, max_items=3)


def _is_public_query(query: str) -> bool:
    lowered = str(query or "").lower()
    code_terms = {
        "python", "javascript", "typescript", "java", "c++", "bug", "stack trace",
        "function", "class", "method", "sql", "html", "css", "react", "fastapi",
        "debug", "compile", "exception", "refactor", "algorithm", "code",
    }
    return not any(term in lowered for term in code_terms)


def _generate_with_g4f(query: str, context_blocks):
    """Generate an AI overview answer using gpt4free as a fallback."""
    if not _G4F_OK:
        return None
    joined_context = "\n\n".join(
        f"Source {idx + 1}: {block['title']}\nURL: {block['url']}\nNotes: {block['summary']}"
        for idx, block in enumerate(context_blocks)
    )
    messages = [
        {
            "role": "system",
            "content": (
                "You are a web answer assistant. Use only the provided sources. "
                "Answer clearly in 2 to 4 sentences, avoid repetition, and then provide up to 3 short bullet facts."
            ),
        },
        {
            "role": "user",
            "content": f"Question: {query}\n\nSources:\n{joined_context}",
        },
    ]
    try:
        client = _G4FClient()
        response = client.chat.completions.create(
            model=G4F_MODEL_ID,
            messages=messages,
            temperature=0.2,
            max_tokens=350,
        )
        content = _extract_chat_reply(response)
        if not content:
            return None
        answer, bullets = _parse_llm_answer(content)
        if not answer:
            answer = _sentence_summary(content, max_sentences=4, max_chars=700)
        return {
            "answer": answer,
            "bullets": bullets,
            "llm_used": True,
            "model": G4F_MODEL_ID,
            "provider": "gpt4free",
        }
    except Exception:
        return None


def _generate_with_llama(query: str, context_blocks):
    """Generate an AI overview answer using the local llama.cpp model."""
    model = _load_llama_model()
    if model is None:
        return None

    joined_context = "\n\n".join(
        f"Source {idx + 1}: {block['title']}\nURL: {block['url']}\nNotes: {block['summary']}"
        for idx, block in enumerate(context_blocks)
    )
    messages = [
        {
            "role": "system",
            "content": (
                "You are a web answer assistant. Use only the provided sources. "
                "Answer clearly in 2 to 4 sentences, avoid repetition, and then provide up to 3 short bullet facts."
            ),
        },
        {
            "role": "user",
            "content": f"Question: {query}\n\nSources:\n{joined_context}",
        },
    ]
    try:
        content, _mode = _generate_llama_reply(
            model,
            messages,
            max_tokens=300,
            temperature=0.2,
        )
        if _reply_is_placeholder_text(content):
            return None
        answer, bullets = _parse_llm_answer(content)
        if not answer:
            answer = _sentence_summary(content, max_sentences=4, max_chars=700)
        return {
            "answer": answer,
            "bullets": bullets,
            "llm_used": True,
            "model": LLAMA_MODEL_FILE,
        }
    except Exception:
        return None


def _attach_summaries(results, max_items: int = 3):
    enriched = []
    for idx, item in enumerate(results):
        row = dict(item)
        if idx < max_items:
            text = _extract_page_text(row.get("url", ""))
            row["summary"] = _summarize_text(text) if text else ""
            row["summary_source_chars"] = len(text)
        else:
            row["summary"] = ""
            row["summary_source_chars"] = 0
        enriched.append(row)
    return enriched


def _query_terms(query: str):
    terms = [t for t in re.findall(r"[a-z0-9]+", str(query or "").lower()) if len(t) >= 3]
    seen = set()
    out = []
    for term in terms:
        if term not in seen:
            seen.add(term)
            out.append(term)
    return out[:12]


def _score_sentence_for_query(query: str, sentence: str) -> int:
    text = str(sentence or "").strip().lower()
    if len(text) < 40:
        return 0
    terms = _query_terms(query)
    hits = sum(1 for term in terms if term in text)
    score = hits * 4
    if re.search(r"\b(is|are|means|refers to|defined as|includes|offers|helps|uses|provides)\b", text):
        score += 2
    if any(ch.isdigit() for ch in text):
        score += 1
    return score


def _best_sentences_for_query(query: str, text: str, max_sentences: int = 4):
    cleaned = " ".join(str(text or "").split()).strip()
    if not cleaned:
        return []
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", cleaned) if len(s.strip()) >= 40]
    ranked = []
    for idx, sentence in enumerate(sentences):
        ranked.append((_score_sentence_for_query(query, sentence), idx, sentence))
    ranked.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    chosen = sorted(ranked[:max_sentences], key=lambda item: item[1])
    return [sentence for score, _, sentence in chosen if score > 0]


def _dedupe_sentences(sentences, max_items: int = 5):
    return _distinct_items(sentences, max_items=max_items)


def _generate_ai_overview(query: str, results, max_sources: int = 3):
    source_cards = []
    candidate_sentences = []
    bullets = []
    llm_context = []

    # Fetch page text in parallel with a tight per-page timeout so g4f is reached quickly
    import concurrent.futures as _cf_fetch
    items = results[:max_sources]
    urls_to_fetch = [str(item.get("url", "") or "").strip() for item in items]

    def _safe_extract(url):
        try:
            if not _is_http_url(url):
                return ""
            with _cf_fetch.ThreadPoolExecutor(max_workers=1) as _ex:
                return _ex.submit(_extract_page_text, url).result(timeout=5)
        except Exception:
            return ""

    with _cf_fetch.ThreadPoolExecutor(max_workers=len(items) or 1) as _pool:
        extracted_texts = list(_pool.map(_safe_extract, urls_to_fetch, timeout=8))

    for item, extracted in zip(items, extracted_texts):
        url = str(item.get("url", "") or "").strip()
        title = str(item.get("title", "") or "").strip()
        snippet = str(item.get("content", "") or "").strip()
        agent = _agentic_explanation(extracted) if extracted else {"summary": "", "highlights": [], "cleaned_points": []}
        candidate_text = extracted or snippet
        page_summary = agent.get("summary", "") or _summarize_text(candidate_text, max_sentences=3, max_chars=420)
        best_sentences = _best_sentences_for_query(query, candidate_text, max_sentences=3)
        combined_notes = _distinct_items([page_summary] + best_sentences + agent.get("cleaned_points", [])[:3], max_items=4)

        if extracted:
            candidate_sentences.extend(_best_sentences_for_query(query, extracted, max_sentences=3))
        if page_summary:
            candidate_sentences.extend(_best_sentences_for_query(query, page_summary, max_sentences=2) or [page_summary])

        for highlight in agent.get("highlights", [])[:2]:
            bullets.append(highlight)

        source_cards.append(
            {
                "title": title or url,
                "url": url,
                "snippet": page_summary or snippet,
            }
        )
        llm_context.append(
            {
                "title": title or url,
                "url": url,
                "summary": " ".join(combined_notes)[:900].strip(),
            }
        )

    answer_sentences = _dedupe_sentences(candidate_sentences, max_items=4)
    bullet_points = _dedupe_sentences(bullets, max_items=5)

    # Try g4f first (fast cloud), fall back to local Qwen if it fails
    llm_result = None
    if llm_context:
        try:
            with _cf_fetch.ThreadPoolExecutor(max_workers=1) as _ex:
                _fut = _ex.submit(_generate_with_g4f, query, llm_context[:3])
                llm_result = _fut.result(timeout=30)
        except Exception:
            llm_result = None
    if not llm_result and llm_context:
        llm_result = _generate_with_llama(query, llm_context[:3])
    if llm_result:
        return {
            "enabled": True,
            "answer": llm_result["answer"],
            "bullets": llm_result["bullets"][:4],
            "sources": source_cards,
            "source_count": len(source_cards),
            "llm_used": True,
            "model": llm_result.get("model", LLAMA_MODEL_FILE),
            "provider": llm_result.get("provider", "llama.cpp"),
            "fallback_reason": "",
        }

    if not answer_sentences:
        fallback_snippets = [str(item.get("content", "") or "").strip() for item in results[:max_sources] if str(item.get("content", "") or "").strip()]
        answer_text = _sentence_summary(" ".join(fallback_snippets), max_sentences=3, max_chars=650)
    else:
        answer_text = " ".join(answer_sentences)

    if not bullet_points:
        bullet_points = answer_sentences[1:4]

    if not answer_text:
        return {
            "enabled": True,
            "answer": "I couldn't build a reliable answer from the fetched pages, but the top sources are still shown below.",
            "bullets": [],
            "sources": source_cards,
            "source_count": len(source_cards),
        }

    return {
        "enabled": True,
        "answer": answer_text,
        "bullets": bullet_points[:4],
        "sources": source_cards,
        "source_count": len(source_cards),
        "llm_used": False,
        "model": LLAMA_MODEL_FILE if _llama_available() else "",
        "fallback_reason": _LLAMA_LOAD_ERROR or ("llama-cpp-python not installed" if not _llama_available() else "model fallback used"),
    }


def _duckduckgo_search(query: str, limit: int = 5, page: int = 1, region: str = "auto"):
    preferred_domains = _preferred_domains(query)
    merged = []
    required = (page * limit) + 1
    fetch_cap = max(8, min(required, 60))

    for candidate in _query_candidates(query):
        fetched = False
        for region_code in _region_candidates(region):
            try:
                rows = list(DDGS().text(candidate, region=region_code, safesearch="moderate", max_results=fetch_cap))
            except Exception:
                continue
            if not rows:
                continue
            fetched = True
            for i, row in enumerate(rows[:fetch_cap], start=1):
                title = str(row.get("title", "") or "").strip()
                href = str(row.get("href", "") or "").strip()
                body = str((row.get("body", "") or "")).replace("\n", " ").strip()
                if not title and not body:
                    continue
                merged.append({
                    "title": title,
                    "url": href,
                    "content": body,
                    "score": _score_web_result(query, title, body, href, i, preferred_domains),
                    "source": "duckduckgo",
                })
            if fetched:
                break

    deduped = []
    seen_urls = set()
    for item in sorted(merged, key=lambda x: x["score"], reverse=True):
        key = item["url"].lower()
        if key and key not in seen_urls:
            seen_urls.add(key)
            deduped.append(item)
        if len(deduped) >= fetch_cap:
            break

    for idx, item in enumerate(deduped, start=1):
        item["id"] = idx
    return deduped


def _duckduckgo_news_search(query: str, limit: int = 5, page: int = 1, region: str = "auto"):
    required = (page * limit) + 1
    fetch_cap = max(8, min(required, 60))
    rows = []
    for region_code in _region_candidates(region):
        try:
            candidate_rows = list(DDGS().news(query, region=region_code, safesearch="moderate", max_results=fetch_cap))
            if candidate_rows:
                rows = candidate_rows
                break
        except Exception:
            continue
    out = []
    for i, row in enumerate(rows[:fetch_cap], start=1):
        title = str(row.get("title", "") or "").strip()
        url = str(row.get("url", "") or row.get("href", "") or "").strip()
        body = str(row.get("body", "") or "").replace("\n", " ").strip()
        date = str(row.get("date", "") or "").strip()
        source_name = str(row.get("source", "") or "").strip()
        if not title and not body:
            continue
        out.append(
            {
                "id": i,
                "title": title,
                "url": url,
                "content": body,
                "score": max(1, 10 - i),
                "source": "duckduckgo-news",
                "date": date,
                "publisher": source_name,
            }
        )
    return out


def _duckduckgo_images_search(query: str, limit: int = 12, page: int = 1, region: str = "auto"):
    required = (page * limit) + 1
    fetch_cap = max(18, min(required, 80))
    rows = []
    for region_code in _region_candidates(region):
        try:
            candidate_rows = list(DDGS().images(query, region=region_code, safesearch="moderate", max_results=fetch_cap))
            if candidate_rows:
                rows = candidate_rows
                break
        except Exception:
            continue
    out = []
    for i, row in enumerate(rows[:fetch_cap], start=1):
        title = str(row.get("title", "") or "").strip()
        image = str(row.get("image", "") or row.get("thumbnail", "") or "").strip()
        thumb = str(row.get("thumbnail", "") or image).strip()
        page_url = str(row.get("url", "") or row.get("href", "") or "").strip()
        source_name = str(row.get("source", "") or "").strip()
        if not image:
            continue
        out.append(
            {
                "id": i,
                "title": title or f"Image {i}",
                "url": page_url,
                "content": source_name,
                "score": max(1, 10 - i),
                "source": "duckduckgo-images",
                "image": image,
                "thumbnail": thumb,
            }
        )
    return out


def _duckduckgo_videos_search(query: str, limit: int = 8, page: int = 1, region: str = "auto"):
    required = (page * limit) + 1
    fetch_cap = max(10, min(required, 60))
    rows = []
    for region_code in _region_candidates(region):
        try:
            candidate_rows = list(DDGS().videos(query, region=region_code, safesearch="moderate", max_results=fetch_cap))
            if candidate_rows:
                rows = candidate_rows
                break
        except Exception:
            continue
    out = []
    for i, row in enumerate(rows[:fetch_cap], start=1):
        title = str(row.get("title", "") or "").strip()
        page_url = str(row.get("content", "") or row.get("url", "") or row.get("href", "") or "").strip()
        body = str(row.get("description", "") or row.get("body", "") or "").replace("\n", " ").strip()
        duration = str(row.get("duration", "") or "").strip()
        publisher = str(row.get("publisher", "") or "").strip()
        if not title and not body:
            continue
        out.append(
            {
                "id": i,
                "title": title,
                "url": page_url,
                "content": body,
                "score": max(1, 10 - i),
                "source": "duckduckgo-videos",
                "duration": duration,
                "publisher": publisher,
            }
        )
    return out


@app.get("/setup.bat")
def download_setup():
    """Serve the Windows installer script for download."""
    from fastapi.responses import FileResponse
    bat_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "setup.bat")
    if os.path.exists(bat_path):
        return FileResponse(bat_path, media_type="application/octet-stream", filename="setup.bat")
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("setup.bat not found", status_code=404)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": "Search Engine",
            "desktop_mode": os.getenv("APP_MODE", "").strip().lower() == "desktop",
        },
    )


@app.get("/api/search")
def search(
    q: str = Query(default="", description="Search query"),
    limit: int = Query(default=10, ge=1, le=25),
    page: int = Query(default=1, ge=1, le=200),
    mode: str = Query(default="ai", description="ai | web | news | images | videos | files"),
    scope: str = Query(default="pc", description="workspace | pc (used for files mode)"),
    summarize: bool = Query(default=False, description="Add extracted page summaries (web/news)."),
    region: str = Query(default="auto", description="auto | worldwide | canada | us | uk | india"),
):
    query = (q or "").strip()
    if not query:
        return {
            "query": query,
            "page": page,
            "limit": limit,
            "mode": mode,
            "ai_overview": None,
            "summarize": summarize,
            "region": str(region or "auto").lower(),
            "count": 0,
            "total_count": 0,
            "has_next": False,
            "results": [],
            "source": "none",
        }

    chosen_mode = str(mode or "web").strip().lower()
    if chosen_mode not in {"ai", "web", "news", "images", "videos", "files"}:
        chosen_mode = "web"

    web_limit = max(1, min(limit, 12))
    try:
        if chosen_mode == "ai":
            vertical_results = _duckduckgo_search(query, limit=max(web_limit, 6), page=1, region=region)
        elif chosen_mode == "news":
            vertical_results = _duckduckgo_news_search(query, limit=web_limit, page=page, region=region)
        elif chosen_mode == "images":
            vertical_results = _duckduckgo_images_search(query, limit=web_limit, page=page, region=region)
        elif chosen_mode == "videos":
            vertical_results = _duckduckgo_videos_search(query, limit=web_limit, page=page, region=region)
        elif chosen_mode == "files":
            fetch_cap = max(25, min((page * limit) + 20, 200))
            matched_files = _search_workspace_files(query, max_results=fetch_cap, scope=scope)
            vertical_results = []
            for idx, item in enumerate(matched_files, start=1):
                path = str(item.get("path", "") or "").strip()
                line_start = int(item.get("line_start", 1) or 1)
                line_end = int(item.get("line_end", line_start) or line_start)
                snippet = str(item.get("snippet", "") or "").strip()
                score = int(item.get("score", 0) or 0)
                title = path.split("/")[-1] if path else f"File {idx}"
                vertical_results.append(
                    {
                        "id": idx,
                        "title": title,
                        "url": "",
                        "content": snippet,
                        "score": score,
                        "source": "workspace-files",
                        "path": path,
                        "line_start": line_start,
                        "line_end": line_end,
                    }
                )
        else:
            vertical_results = _duckduckgo_search(query, limit=web_limit, page=page, region=region)

        if vertical_results:
            page_results, total, has_next = _paginate(vertical_results, page=page, limit=limit)
            ai_overview = None
            if chosen_mode == "ai":
                ai_overview = _generate_ai_overview(query, vertical_results[:5], max_sources=3)
            if summarize and chosen_mode in {"web", "news", "ai"}:
                page_results = _attach_summaries(page_results, max_items=3)
            return {
                "query": query,
                "page": page,
                "limit": limit,
                "mode": chosen_mode,
                "ai_overview": ai_overview,
                "summarize": summarize,
                "region": str(region or "auto").lower(),
                "count": len(page_results),
                "total_count": total,
                "has_next": has_next,
                "results": page_results,
                "source": "workspace-files" if chosen_mode == "files" else ("duckduckgo-ai" if chosen_mode == "ai" else f"duckduckgo-{chosen_mode}"),
            }
    except Exception:
        pass

    if chosen_mode in {"ai", "news", "images", "videos", "files"}:
        return {
            "query": query,
            "page": page,
            "limit": limit,
            "mode": chosen_mode,
            "ai_overview": None,
            "summarize": summarize,
            "region": str(region or "auto").lower(),
            "count": 0,
            "total_count": 0,
            "has_next": False,
            "results": [],
            "source": "workspace-files" if chosen_mode == "files" else f"duckduckgo-{chosen_mode}",
        }

    scored = []
    for doc in DOCUMENTS:
        score = _score_document(query, doc)
        if score > 0:
            scored.append({
                "id": doc["id"],
                "title": doc["title"],
                "content": doc["content"],
                "url": "",
                "score": score,
                "source": "local",
            })

    scored.sort(key=lambda item: item["score"], reverse=True)
    results, total, has_next = _paginate(scored, page=page, limit=limit)
    return {
        "query": query,
        "page": page,
        "limit": limit,
        "mode": chosen_mode,
        "ai_overview": None,
        "summarize": summarize,
        "region": str(region or "auto").lower(),
        "count": len(results),
        "total_count": total,
        "has_next": has_next,
        "results": results,
        "source": "local",
    }


@app.get("/api/extract-summary")
def extract_summary(
    url: str = Query(default="", description="Result URL to extract and summarize."),
    max_sentences: int = Query(default=4, ge=1, le=8),
    max_chars: int = Query(default=700, ge=200, le=2400),
):
    link = str(url or "").strip()
    if not link:
        return {
            "ok": False,
            "url": "",
            "summary": "",
            "extracted_text": "",
            "extracted_chars": 0,
            "message": "Missing URL.",
        }
    if not _is_http_url(link):
        return {
            "ok": False,
            "url": link,
            "summary": "",
            "extracted_text": "",
            "extracted_chars": 0,
            "message": "Only http/https URLs are supported.",
        }

    extracted = _extract_page_text(link)
    if not extracted:
        return {
            "ok": False,
            "url": link,
            "summary": "",
            "agent_explanation": "",
            "highlights": [],
            "cleaned_points": [],
            "extracted_text": "",
            "extracted_chars": 0,
            "message": "Could not extract readable article text from this page (it may be blocked, login-walled, or bot-protected).",
        }

    # for non-list pages try sumy first, then agentic
    if not _is_list_heavy_text(extracted):
        sumy_sents = _sumy_summarize(extracted, n_sentences=max_sentences)
        sumy_text = " ".join(sumy_sents) if sumy_sents else ""
    else:
        sumy_text = ""
    agent = _agentic_explanation(extracted)
    summary = sumy_text or agent.get("summary", "") or _sentence_summary(extracted, max_sentences=max_sentences, max_chars=max_chars)
    return {
        "ok": True,
        "url": link,
        "summary": summary,
        "agent_explanation": agent.get("explanation", summary),
        "highlights": agent.get("highlights", []),
        "cleaned_points": agent.get("cleaned_points", []),
        "extracted_text": extracted,
        "extracted_chars": len(extracted),
        "message": "ok",
    }


@app.get("/api/ocr/status")
def ocr_status():
    configured = bool(OCR_API_BASE)
    return {
        "ok": True,
        "configured": configured,
        "base_url": OCR_API_BASE if configured else "",
        "message": "ready" if configured else "Set OCR_API_BASE to enable OCR integration.",
    }


def _column_letters_to_index(col_letters: str) -> int:
    value = 0
    for ch in str(col_letters or "").upper():
        if "A" <= ch <= "Z":
            value = value * 26 + (ord(ch) - ord("A") + 1)
    return max(1, value)


def _parse_xlsx_preview(raw: bytes, max_sheets: int = 3, max_rows: int = 40) -> str:
    out_lines = []
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = set(zf.namelist())

        shared_strings = []
        if "xl/sharedStrings.xml" in names:
            try:
                root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                for si in root.findall("x:si", ns):
                    parts = []
                    for t in si.findall(".//x:t", ns):
                        parts.append(str(t.text or ""))
                    shared_strings.append("".join(parts))
            except Exception:
                shared_strings = []

        sheet_files = sorted([n for n in names if n.startswith("xl/worksheets/") and n.endswith(".xml")])
        if not sheet_files:
            return "No worksheet XML files found in Excel workbook."

        ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        for sheet_idx, sheet_name in enumerate(sheet_files[: max(1, int(max_sheets))], start=1):
            out_lines.append(f"Sheet {sheet_idx}: {sheet_name}")
            try:
                root = ET.fromstring(zf.read(sheet_name))
                rows = root.findall(".//x:sheetData/x:row", ns)
                for row in rows[: max(1, int(max_rows))]:
                    values_by_col = {}
                    max_col = 0
                    for c in row.findall("x:c", ns):
                        ref = str(c.attrib.get("r", "") or "")
                        col_letters = "".join(ch for ch in ref if ch.isalpha())
                        col_idx = _column_letters_to_index(col_letters)
                        max_col = max(max_col, col_idx)
                        c_type = str(c.attrib.get("t", "") or "")
                        v_node = c.find("x:v", ns)
                        if v_node is None:
                            txt = ""
                            is_node = c.find("x:is", ns)
                            if is_node is not None:
                                txt = "".join(str(t.text or "") for t in is_node.findall(".//x:t", ns))
                            values_by_col[col_idx] = txt
                            continue
                        raw_v = str(v_node.text or "")
                        if c_type == "s":
                            try:
                                si = int(raw_v)
                                raw_v = shared_strings[si] if 0 <= si < len(shared_strings) else raw_v
                            except Exception:
                                pass
                        values_by_col[col_idx] = raw_v
                    if max_col <= 0:
                        continue
                    row_vals = [str(values_by_col.get(i, "")).strip() for i in range(1, max_col + 1)]
                    out_lines.append(" | ".join(row_vals).rstrip())
            except Exception as exc:
                out_lines.append(f"(failed to parse sheet: {exc})")
            out_lines.append("")

    return "\n".join(out_lines).strip()


def _inspect_file_bytes(filename: str, raw: bytes, max_chars: int = 12000) -> tuple[str, str]:
    name = str(filename or "file").strip() or "file"
    ext = os.path.splitext(name)[1].lower()
    payload = raw or b""

    if ext in {".xlsx", ".xlsm"}:
        try:
            text = _parse_xlsx_preview(payload)
            return ("xlsx", text[:max_chars])
        except Exception as exc:
            return ("binary", f"Excel parse failed: {exc}")

    if ext == ".csv":
        try:
            decoded = payload.decode("utf-8", errors="replace")
            reader = csv.reader(io.StringIO(decoded))
            lines = []
            for idx, row in enumerate(reader):
                if idx >= 80:
                    break
                lines.append(" | ".join(str(cell or "") for cell in row))
            return ("csv", "\n".join(lines)[:max_chars])
        except Exception as exc:
            return ("binary", f"CSV parse failed: {exc}")

    text_exts = {
        ".txt", ".md", ".py", ".pyw", ".js", ".jsx", ".ts", ".tsx", ".json", ".html", ".htm", ".css",
        ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env", ".sql", ".sh", ".bash", ".zsh", ".bat",
        ".cmd", ".ps1", ".java", ".cs", ".go", ".rs", ".php", ".rb", ".swift", ".kt", ".kts", ".c", ".h", ".cpp", ".hpp",
    }
    if ext in text_exts:
        try:
            decoded = payload.decode("utf-8", errors="replace")
            return ("text", decoded[:max_chars])
        except Exception:
            pass

    if ext == ".zip":
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                names = zf.namelist()[:300]
            text = "ZIP entries:\n" + "\n".join(names)
            return ("zip", text[:max_chars])
        except Exception as exc:
            return ("binary", f"ZIP parse failed: {exc}")

    sample = payload[:4096]
    printable = sum(1 for b in sample if b in b"\t\n\r" or 32 <= b <= 126)
    ratio = (printable / max(1, len(sample))) if sample else 0.0
    if ratio >= 0.85:
        decoded = payload.decode("utf-8", errors="replace")
        return ("text", decoded[:max_chars])

    head_hex = " ".join(f"{b:02x}" for b in payload[:64])
    meta = {
        "filename": name,
        "size_bytes": len(payload),
        "extension": ext,
        "note": "Binary/unsupported file parsed as metadata preview",
        "head_hex": head_hex,
    }
    return ("binary", json.dumps(meta, ensure_ascii=False, indent=2)[:max_chars])


@app.post("/api/file-inspect")
def file_inspect(payload: dict = Body(default=None)):
    data = payload or {}
    file_base64 = str(data.get("file_base64", "") or "").strip()
    filename = str(data.get("filename", "file") or "file").strip() or "file"
    max_chars = int(data.get("max_chars", 12000) or 12000)
    max_chars = max(1200, min(max_chars, 50000))

    if not file_base64:
        return {"ok": False, "message": "Missing file_base64.", "text": "", "kind": "none"}

    try:
        raw = base64.b64decode(file_base64, validate=False)
    except Exception:
        return {"ok": False, "message": "Invalid base64 payload.", "text": "", "kind": "none"}

    if len(raw) > 40 * 1024 * 1024:
        return {"ok": False, "message": "File too large for inline inspection (max 40MB).", "text": "", "kind": "none"}

    kind, text = _inspect_file_bytes(filename, raw, max_chars=max_chars)
    return {
        "ok": True,
        "message": "ok",
        "kind": kind,
        "filename": filename,
        "text": str(text or ""),
        "chars": len(str(text or "")),
    }


@app.post("/api/ocr/once")
def ocr_once_proxy(payload: dict = Body(default=None)):
    data = payload or {}
    if not OCR_API_BASE:
        return {
            "ok": False,
            "text": "",
            "message": "OCR API is not configured. Set OCR_API_BASE environment variable.",
        }

    file_base64 = str(data.get("file_base64", "") or "").strip()
    filename = str(data.get("filename", "") or "").strip()
    file_type = str(data.get("file_type", "") or "").strip().lower()
    lang = str(data.get("lang", "eng") or "eng").strip() or "eng"
    psm = int(data.get("psm", 3) or 3)
    page = data.get("page", None)
    start_page = data.get("start_page", None)
    end_page = data.get("end_page", None)
    region = data.get("region", None)

    if not file_base64:
        return {"ok": False, "text": "", "message": "Missing file_base64."}

    body = {
        "file_base64": file_base64,
        "filename": filename,
        "file_type": file_type,
        "lang": lang,
        "psm": psm,
    }
    if page is not None:
        body["page"] = page
    if start_page is not None:
        body["start_page"] = start_page
    if end_page is not None:
        body["end_page"] = end_page
    if region is not None:
        body["region"] = region

    headers = {"Content-Type": "application/json", "X-UI-Client": "portable-ocr-web"}
    if OCR_API_KEY:
        headers["Authorization"] = f"Bearer {OCR_API_KEY}"

    url = f"{OCR_API_BASE}/api/ocr_once"
    try:
        # Warm up sleeping OCR spaces and retry once on transient upstream errors.
        try:
            requests.get(OCR_API_BASE, timeout=12)
        except Exception:
            pass

        resp = requests.post(url, json=body, headers=headers, timeout=120)
        if resp.status_code in (502, 503, 504):
            time.sleep(2)
            try:
                requests.get(OCR_API_BASE, timeout=12)
            except Exception:
                pass
            resp = requests.post(url, json=body, headers=headers, timeout=120)

        try:
            result = resp.json()
        except Exception:
            result = {"error": resp.text[:500]}

        if resp.status_code >= 400:
            upstream_message = str(result.get("error") or result.get("message") or "").strip()
            if not upstream_message:
                if resp.status_code == 503:
                    upstream_message = "Upstream OCR service is temporarily unavailable or still starting. Please retry in a few seconds."
                elif resp.status_code == 404:
                    upstream_message = "Upstream OCR endpoint not found. Verify OCR_API_BASE points to the correct service."
                else:
                    upstream_message = f"Upstream OCR request failed with HTTP {resp.status_code}."
            return {
                "ok": False,
                "text": "",
                "status": resp.status_code,
                "message": upstream_message,
                "raw": result,
            }

        if isinstance(result, dict) and "text" in result:
            return {
                "ok": True,
                "text": str(result.get("text", "")),
                "page": result.get("page"),
                "total_pages": result.get("total_pages"),
                "file_type": result.get("file_type", file_type),
                "message": "ok",
            }

        if isinstance(result, dict) and isinstance(result.get("results"), list):
            joined = "\n\n".join(
                f"[Page {item.get('page', idx + 1)}]\n{str(item.get('text', '') or '').strip()}"
                for idx, item in enumerate(result.get("results", []))
            ).strip()
            return {
                "ok": True,
                "text": joined,
                "start_page": result.get("start_page"),
                "end_page": result.get("end_page"),
                "total_pages": result.get("total_pages"),
                "file_type": result.get("file_type", file_type),
                "message": "ok",
            }

        return {
            "ok": False,
            "text": "",
            "message": "Unexpected OCR response format.",
            "raw": result,
        }
    except Exception as exc:
        return {
            "ok": False,
            "text": "",
            "message": f"OCR API request failed: {exc}",
        }


def _looks_like_web_doc_question(query: str) -> bool:
    text = str(query or "").strip().lower()
    if len(text) < 10:
        return False
    web_terms = (
        "latest", "docs", "documentation", "library", "package", "module", "api", "sdk",
        "install", "usage", "example", "examples", "snippet", "snippets", "tutorial",
        "readme", "github", "pypi", "import", "pip install", "npm install", "ocr",
        "extract", "scrape", "search", "open source", "how do i use", "how to use",
        "get me code", "show code", "sample code", "what is", "how do i", "how to",
        "getting started", "quickstart", "guide", "steps", "copilot studio",
        "microsoft learn", "learn.microsoft.com", "power platform", "azure",
    )
    if any(term in text for term in web_terms):
        return True
    if re.search(r"https?://", text):
        return True
    if re.search(r"\b[a-z0-9_]+(?:\.[a-z0-9_]+)+\b", text):
        return True
    if re.search(r"`[^`]{2,40}`", str(query or "")):
        return True
    return False


def _extract_http_urls(text: str):
    raw = str(text or "")
    urls = re.findall(r"https?://[^\s)\]>\"']+", raw, flags=re.IGNORECASE)
    out = []
    seen = set()
    for item in urls:
        value = str(item or "").strip().rstrip(".,;:!?")
        key = value.lower()
        if not value or key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out[:4]


def _package_like_terms(query: str):
    text = str(query or "")
    candidates = []
    candidates.extend(re.findall(r"`([^`]{2,40})`", text))
    candidates.extend(re.findall(r"\b(?:import|from)\s+([a-zA-Z0-9_\.]+)", text))
    candidates.extend(re.findall(r"\b([a-zA-Z][a-zA-Z0-9_\-]{2,30})\b", text))
    blocked = {
        "python", "javascript", "typescript", "please", "could", "should", "would", "using",
        "about", "their", "there", "where", "which", "what", "when", "with", "without",
        "need", "want", "have", "code", "snippet", "snippets", "example", "examples",
        "library", "package", "module", "install", "latest", "question", "answer", "extract",
        "search", "open", "source", "assistant", "from", "import", "hello",
    }
    out = []
    seen = set()
    for item in candidates:
        value = str(item or "").strip().strip("`.,:;()[]{}<>'\"")
        key = value.lower()
        if not value or key in blocked or len(key) < 3 or len(key) > 40:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out[:4]


def _code_doc_search_candidates(query: str):
    raw = _compress_spaces(query)
    packages = _package_like_terms(raw)
    lowered = raw.lower()
    is_code_focused = bool(
        re.search(
            r"\b(python|javascript|typescript|java|c\+\+|api|sdk|library|package|import|pip|npm|function|class|error|exception|stack trace|code|snippet)\b",
            lowered,
        )
    )

    candidates = [raw]
    if is_code_focused and raw and "python" not in lowered:
        candidates.append(f"{raw} python")
    if not re.search(r"\b(example|snippet|code)\b", raw, flags=re.IGNORECASE):
        candidates.append(f"{raw} example")
    candidates.append(f"{raw} documentation")
    candidates.append(f"{raw} how to")

    if "copilot studio" in lowered:
        candidates.extend([
            "site:learn.microsoft.com copilot studio getting started",
            "site:learn.microsoft.com microsoft copilot studio",
            "copilot studio documentation",
        ])

    for pkg in packages[:2]:
        candidates.extend([
            f"site:pypi.org {pkg}",
            f"site:github.com {pkg}",
            f"site:readthedocs.io {pkg}",
            f"{pkg} python example",
        ])
    out = []
    seen = set()
    for item in candidates:
        value = _compress_spaces(item)
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            out.append(value)
    return out[:8]


def _score_code_doc_result(query: str, item: dict) -> int:
    title = str(item.get("title", "") or "")
    snippet = str(item.get("content", "") or "")
    url = str(item.get("url", "") or "")
    domain = urlparse(url).netloc.lower()
    score = int(item.get("score", 0) or 0)
    if any(host in domain for host in ("pypi.org", "github.com", "readthedocs.io", "docs.python.org", "huggingface.co", "learn.microsoft.com")):
        score += 10
    if "learn.microsoft.com" in domain:
        score += 6
    text = f"{title} {snippet} {url}".lower()
    if any(term in text for term in ("example", "examples", "readme", "quickstart", "install", "usage", "tutorial")):
        score += 4
    if "/blob/" in url or "/issues/" in url:
        score -= 2
    if "github.com" in domain and "/tree/" not in url and "/blob/" not in url:
        score += 2
    return score


def _looks_like_code_line(line: str) -> bool:
    text = str(line or "").rstrip()
    stripped = text.strip()
    if len(stripped) < 3 or len(stripped) > 180:
        return False
    patterns = (
        r"^(pip|python|python3|uv|poetry|npm|yarn|pnpm|curl|wget)\b",
        r"^(from|import|def|class|return|if|elif|else|for|while|try|except|with|print)\b",
        r"^(const|let|var|function|export|async|await)\b",
        r"^(<\/?[A-Za-z][^>]*>|\{.*\}|\[.*\])$",
        r"^[A-Za-z_][A-Za-z0-9_]*\s*=",
        r"^#include\b",
    )
    if any(re.search(pattern, stripped) for pattern in patterns):
        return True
    punctuation = sum(1 for ch in stripped if ch in "(){}[]:=<>/\\")
    return punctuation >= 3 and " " in stripped


def _extract_code_blocks_from_text(text: str, max_blocks: int = 3):
    lines = [ln.rstrip() for ln in str(text or "").splitlines()]
    blocks = []
    current = []
    for line in lines:
        if _looks_like_code_line(line):
            current.append(line)
            continue
        if current:
            block = "\n".join(current).strip()
            if block:
                blocks.append(block)
            current = []
    if current:
        block = "\n".join(current).strip()
        if block:
            blocks.append(block)
    deduped = []
    seen = set()
    for block in blocks:
        key = _normalize_line_for_dedupe(block)[:180]
        if key and key not in seen:
            seen.add(key)
            deduped.append(block)
        if len(deduped) >= max_blocks:
            break
    return deduped


def _build_auto_doc_context(query: str, max_sources: int = 4, force_search: bool = False):
    """Fast auto-doc retrieval: parallel search + page fetch with hard timeout."""
    import concurrent.futures

    forced_urls = _extract_http_urls(query)
    should_search = bool(force_search or _looks_like_web_doc_question(query))
    if not should_search and not forced_urls:
        return {"context": "", "sources": [], "used": False}

    gathered = []

    if forced_urls:
        for url in forced_urls[:max_sources]:
            gathered.append({
                "title": url,
                "url": url,
                "content": "User-provided URL",
                "score": 100,
            })
    else:
        # Run up to 2 search variants in parallel
        candidates = _code_doc_search_candidates(query)[:4]

        def _search_one(candidate):
            try:
                rows = list(DDGS().text(candidate, region="wt-wt", safesearch="moderate", max_results=5))
                return [
                    {
                        "title": str(r.get("title", "") or "").strip(),
                        "url": str(r.get("href", "") or "").strip(),
                        "content": str(r.get("body", "") or "").strip(),
                    }
                    for r in rows if r.get("href")
                ]
            except Exception:
                return []

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(_search_one, c): c for c in candidates}
            try:
                for fut in concurrent.futures.as_completed(futures, timeout=10):
                    try:
                        gathered.extend(fut.result())
                    except Exception:
                        pass
            except Exception:
                # Timeout — collect any that already finished
                for fut in futures:
                    if fut.done():
                        try:
                            gathered.extend(fut.result())
                        except Exception:
                            pass

    # Deduplicate + rank
    ranked = []
    seen_urls = set()
    for item in gathered:
        url = str(item.get("url", "") or "").strip().lower()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        item["code_score"] = _score_code_doc_result(query, item)
        ranked.append(item)
    ranked.sort(key=lambda row: row.get("code_score", 0), reverse=True)

    top_items = ranked[:max_sources]
    if not top_items:
        return {"context": "", "sources": [], "used": False}

    # Fetch pages in parallel, 4s timeout per page
    def _fetch_one(item):
        url = str(item.get("url", "") or "").strip()
        extracted = ""
        if _is_http_url(url):
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as inner:
                    f = inner.submit(_extract_page_text, url)
                    extracted = f.result(timeout=4) or ""
            except Exception:
                extracted = ""
        return item, extracted

    fetch_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_sources) as pool:
        futs = [pool.submit(_fetch_one, item) for item in top_items]
        try:
            for fut in concurrent.futures.as_completed(futs, timeout=8):
                try:
                    fetch_results.append(fut.result())
                except Exception:
                    pass
        except Exception:
            for fut in futs:
                if fut.done():
                    try:
                        fetch_results.append(fut.result())
                    except Exception:
                        pass

    context_parts = []
    source_cards = []
    for item, extracted in fetch_results:
        title = str(item.get("title", "") or item.get("url", "") or "Source").strip()
        url = str(item.get("url", "") or "").strip()
        source_text = extracted or str(item.get("content", "") or "")
        summary = _summarize_text(source_text, max_sentences=5, max_chars=700) if source_text else ""
        code_blocks = _extract_code_blocks_from_text(extracted, max_blocks=2) if extracted else []
        context_parts.append(f"Source: {title}\nURL: {url}\nSummary: {summary or str(item.get('content', '') or '').strip()[:300]}")
        for idx, block in enumerate(code_blocks[:2], start=1):
            context_parts.append(f"Snippet {idx} from {title}:\n```text\n{block[:1200]}\n```")
        source_cards.append({
            "title": title,
            "url": url,
            "summary": summary or str(item.get("content", "") or "").strip()[:300],
            "from_user_url": bool(forced_urls and url.lower() in {u.lower() for u in forced_urls}),
        })

    if not context_parts:
        return {"context": "", "sources": [], "used": False}

    intro = (
        "Web research context for this coding question. Prefer these sources over memory. "
        "If the sources are weak or incomplete, say so briefly."
    )
    return {
        "context": intro + "\n\n" + "\n\n".join(context_parts[:6]),
        "sources": source_cards,
        "used": True,
    }


@app.post("/api/code-chat")
def code_chat(payload: dict = Body(default=None)):
    started_at = time.perf_counter()
    data = payload or {}
    messages = data.get("messages", [])
    provider = _normalize_code_provider(data.get("provider", "llama"))
    selected_model = _normalize_model_name(data.get("model", ""))
    selected_g4f_provider = _normalize_provider_name(data.get("g4f_provider", ""))
    strict_mode = bool(data.get("strict_mode", True))
    auto_continue = bool(data.get("auto_continue", True))
    continue_response = bool(data.get("continue_response", False))
    previous_reply = str(data.get("previous_reply", "") or "")
    code_context = str(data.get("code_context", "") or "")
    attached_context_files = data.get("attached_context_files", [])
    runtime_context = str(data.get("runtime_context", "") or "")
    candidate_paths = data.get("candidate_paths", [])
    search_scope = _normalize_search_scope(data.get("search_scope", "pc"))
    search_web = bool(data.get("search_web", False))
    effective_search_web = bool(search_web or provider == "gpt4free")
    tool_requests = data.get("tool_requests", [])

    attached_context_text = _build_attached_context_blocks_text(attached_context_files)
    if attached_context_text:
        if code_context.strip():
            code_context = code_context.rstrip() + "\n\n" + attached_context_text
        else:
            code_context = attached_context_text

    if not isinstance(messages, list):
        messages = []
    cleaned_messages = []
    for row in messages:
        if not isinstance(row, dict):
            continue
        content = _compress_spaces(row.get("content", ""))
        if not content:
            continue
        cleaned_messages.append(
            {
                "role": str(row.get("role", "user") or "user"),
                "content": content[:4000],
            }
        )

    if not cleaned_messages:
        return _with_structured_response({
            "ok": False,
            "reply": "",
            "llm_used": False,
            "model": LLAMA_MODEL_FILE,
            "provider_used": "llama.cpp",
            "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
            "message": "No message provided.",
        })

    latest_query = cleaned_messages[-1]["content"] if cleaned_messages else ""
    is_url_explain_query = _query_is_url_explanation_request(latest_query)
    if is_url_explain_query:
        auto_continue = False

    if continue_response:
        continuation_seed = previous_reply.strip()
        if not continuation_seed:
            return _with_structured_response({
                "ok": False,
                "reply": "",
                "llm_used": False,
                "model": LLAMA_MODEL_FILE,
                "provider_used": "local",
                "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
                "message": "Missing previous_reply for continuation.",
            })

        continuation_messages = _build_continue_messages(cleaned_messages, continuation_seed)
        continuation_result = _generate_code_chat_reply(
            continuation_messages,
            code_context=code_context,
            provider=provider,
            selected_model=selected_model,
            selected_g4f_provider=selected_g4f_provider,
            strict_mode=strict_mode,
        )
        continuation_text = _normalize_collapsed_code_blocks(continuation_result.get("reply", ""))
        merged = _merge_continuation_reply(continuation_seed, continuation_text)
        return _with_structured_response({
            "ok": bool(continuation_result.get("ok", True)),
            "reply": merged,
            "continuation_only": True,
            "continued": True,
            "llm_used": continuation_result.get("llm_used", True),
            "model": continuation_result.get("model", selected_model or LLAMA_MODEL_FILE),
            "provider_used": continuation_result.get("provider_used", provider),
            "message": continuation_result.get("message", "ok"),
            "prompt_preview": latest_query,
            "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
        })

    has_explicit_context = bool(str(code_context or "").strip())
    has_runtime_context = bool(str(runtime_context or "").strip())

    # Context strategy:
    # - Keep strict file/workspace behavior for code context.
    # - Always allow web context for cloud providers like gpt4free.
    should_use_web_context = bool(effective_search_web)
    if should_use_web_context:
        try:
            auto_doc = _build_auto_doc_context(latest_query, max_sources=3, force_search=bool(effective_search_web))
        except Exception:
            auto_doc = {"context": "", "sources": [], "used": False}
    else:
        auto_doc = {"context": "", "sources": [], "used": False}
    has_attached_folder_paths = isinstance(candidate_paths, list) and bool(candidate_paths)
    if has_explicit_context:
        auto_workspace = {"context": "", "matches": [], "used": False}
    elif has_attached_folder_paths:
        try:
            auto_workspace = _build_workspace_context_for_chat(
                cleaned_messages,
                latest_query,
                candidate_paths=candidate_paths,
                max_items=5,
                scope="workspace",
            )
        except Exception:
            auto_workspace = {"context": "", "matches": [], "used": False}
    else:
        auto_workspace = {"context": "", "matches": [], "used": False}
    merged_context_parts = []
    retrieved_code_context = _build_retrieved_code_context(
        latest_query,
        code_context=code_context,
        runtime_context=runtime_context,
    )
    if retrieved_code_context:
        merged_context_parts.append(retrieved_code_context)
    if auto_doc.get("used") and auto_doc.get("context"):
        merged_context_parts.append(auto_doc["context"])
    if auto_workspace.get("used") and auto_workspace.get("context"):
        merged_context_parts.append(auto_workspace["context"])
    try:
        tool_bundle = _collect_tool_results_for_chat(
            latest_query,
            tool_requests=tool_requests,
            search_web=bool(effective_search_web),
        )
    except Exception as exc:
        tool_bundle = {
            "used": False,
            "results": [{"ok": False, "tool": "tool_bundle", "error": str(exc)}],
            "context": "",
        }
    if tool_bundle.get("used") and tool_bundle.get("context"):
        merged_context_parts.append(tool_bundle["context"])
    merged_context = _cap_context_text("\n\n".join(part for part in merged_context_parts if part), max_chars=14000)

    try:
        result = _generate_code_chat_reply(
            cleaned_messages,
            code_context=merged_context,
            provider=provider,
            selected_model=selected_model,
            selected_g4f_provider=selected_g4f_provider,
            strict_mode=strict_mode,
        )
    except KeyboardInterrupt as exc:
        return _with_structured_response({
            "ok": False,
            "reply": "Generation was interrupted. Please retry.",
            "llm_used": False,
            "model": LLAMA_MODEL_FILE,
            "provider_used": provider,
            "prompt_preview": latest_query,
            "web_context_used": bool(auto_doc.get("used")),
            "web_sources": auto_doc.get("sources", [])[:4],
            "workspace_context_used": bool(auto_workspace.get("used")),
            "workspace_matches": auto_workspace.get("matches", [])[:4],
            "tool_context_used": bool(tool_bundle.get("used")),
            "tool_results": tool_bundle.get("results", [])[:4] if isinstance(tool_bundle.get("results"), list) else [],
            "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
            "message": f"Generation interrupted safely: {exc}",
        })
    except Exception as exc:
        return _with_structured_response({
            "ok": False,
            "reply": "I hit an internal error while generating this answer. Please retry once.",
            "llm_used": False,
            "model": LLAMA_MODEL_FILE,
            "provider_used": provider,
            "prompt_preview": latest_query,
            "web_context_used": bool(auto_doc.get("used")),
            "web_sources": auto_doc.get("sources", [])[:4],
            "workspace_context_used": bool(auto_workspace.get("used")),
            "workspace_matches": auto_workspace.get("matches", [])[:4],
            "tool_context_used": bool(tool_bundle.get("used")),
            "tool_results": tool_bundle.get("results", [])[:4] if isinstance(tool_bundle.get("results"), list) else [],
            "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
            "message": f"Generation failed safely: {exc}",
        })
    is_tool_review_query = _query_is_tool_output_review(latest_query)

    if (not is_tool_review_query) and _query_requests_file_modification(latest_query) and not _reply_mentions_approval(result.get("reply", "")) and not _query_grants_write_approval(latest_query):
        approval_note = (
            "Approval step: run in dry-run/read-only mode first. "
            "Apply file changes only after your confirmation (run-python supports allow_file_write=true for approved writes).\n\n"
        )
        result["reply"] = approval_note + str(result.get("reply", ""))

    if auto_continue and _reply_looks_truncated(result.get("reply", "")):
        continuation_messages = _build_continue_messages(cleaned_messages, result.get("reply", ""))
        continuation_result = _generate_code_chat_reply(
            continuation_messages,
            code_context=merged_context,
            provider=provider,
            selected_model=selected_model,
            selected_g4f_provider=selected_g4f_provider,
            strict_mode=strict_mode,
        )
        continuation_text = str(continuation_result.get("reply", "") or "").strip()
        if continuation_text:
            result["reply"] = _merge_continuation_reply(result.get("reply", ""), continuation_text)
            result["continued"] = True
            result["message"] = "Auto-continued truncated response."
        else:
            result["continued"] = False
            result["can_continue"] = True
            result["continue_hint"] = "Resend with continue_response=true and previous_reply to continue manually."

    auto_exec_rows = []
    if _query_requests_python_execution(latest_query):
        auto_exec_rows = _auto_python_tool_chain(result.get("reply", ""))
        if auto_exec_rows:
            exec_context = _tool_rows_to_context(auto_exec_rows)
            if exec_context:
                explain_messages = list(cleaned_messages) + [
                    {"role": "assistant", "content": str(result.get("reply", "") or "")},
                    {
                        "role": "user",
                        "content": (
                            "Execution results are available below. Explain what happened based on this output, "
                            "mention package installation if any, and provide final actionable answer.\n\n"
                            + exec_context
                        ),
                    },
                ]
                explained = _generate_code_chat_reply(
                    explain_messages,
                    code_context=(merged_context + "\n\n" + exec_context).strip(),
                    provider=provider,
                    selected_model=selected_model,
                    selected_g4f_provider=selected_g4f_provider,
                    strict_mode=strict_mode,
                )
                explained_reply = str(explained.get("reply", "") or "").strip()
                if explained.get("ok") and explained_reply:
                    result["reply"] = _normalize_collapsed_code_blocks(explained_reply)
                    result["llm_used"] = explained.get("llm_used", result.get("llm_used", True))
                    result["model"] = explained.get("model", result.get("model", LLAMA_MODEL_FILE))
                    result["provider_used"] = explained.get("provider_used", result.get("provider_used", provider))
                    result["message"] = "Auto-ran generated Python and explained execution output."

    if _query_is_ddgs_video_search_request(latest_query):
        result["reply"] = _build_ddgs_video_search_reply()
        result["ok"] = True
        result["message"] = "Used DDGS videos() safe template for video-search request."
    else:
        result["reply"] = _fix_ddgs_api_hallucination(result.get("reply", ""))

    result["reply"] = _normalize_collapsed_code_blocks(result.get("reply", ""))
    result["prompt_preview"] = latest_query
    result["web_context_used"] = bool(auto_doc.get("used"))
    result["web_sources"] = auto_doc.get("sources", [])[:4]
    result["workspace_context_used"] = bool(auto_workspace.get("used"))
    result["workspace_matches"] = auto_workspace.get("matches", [])[:4]
    merged_tool_rows = (list(tool_bundle.get("results", []) if isinstance(tool_bundle.get("results"), list) else []) + auto_exec_rows)[:8]
    result["tool_context_used"] = bool(merged_tool_rows)
    result["tool_results"] = merged_tool_rows[:4]
    result["elapsed_ms"] = int((time.perf_counter() - started_at) * 1000)
    return _with_structured_response(result)


def _extract_llama_stream_delta(chunk: dict) -> str:
    def _collect_text_parts(row) -> str:
        if not isinstance(row, dict):
            return ""
        parts = []
        for key in ("reasoning_content", "reasoning", "content", "text"):
            value = row.get(key)
            if isinstance(value, str) and value:
                parts.append(value)
        return "".join(parts)

    if not isinstance(chunk, dict):
        return ""
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0] if isinstance(choices[0], dict) else {}
    delta = choice.get("delta")
    text = _collect_text_parts(delta)
    if text:
        return text
    message = choice.get("message")
    text = _collect_text_parts(message)
    if text:
        return text
    text = _collect_text_parts(choice)
    if text:
        return text
    return ""


def _llama_stream_events(conversation, auto_doc, auto_workspace, latest_query, started_at, tool_bundle=None, cancel_event=None):
    """Standalone sync generator — yields NDJSON lines for the llama chat stream."""
    built = ""
    tools = tool_bundle if isinstance(tool_bundle, dict) else {"used": False, "results": []}
    base_tool_rows = list(tools.get("results", [])) if isinstance(tools.get("results"), list) else []
    def _is_cancelled() -> bool:
        return bool(cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)())
    try:
        if _is_cancelled():
            return
        yield json.dumps({"type": "status", "message": "Task created. Preparing local agent model..."}, ensure_ascii=False) + "\n"
        import threading as _threading, queue as _queue
        _q: _queue.Queue = _queue.Queue()

        def _load_in_bg():
            try:
                _q.put(("ok", _load_llama_model()))
            except Exception as _e:
                _q.put(("err", _e))

        _threading.Thread(target=_load_in_bg, daemon=True).start()
        _tick = 0
        while True:
            if _is_cancelled():
                return
            try:
                _status, _val = _q.get(timeout=5)
                break
            except _queue.Empty:
                _tick += 5
                yield json.dumps({"type": "status", "message": f"Preparing local model... {_tick}s"}, ensure_ascii=False) + "\n"

        if _status == "err":
            raise _val

        model = _val
        if model is None:
            failure = {
                "ok": False, "reply": "", "llm_used": False,
                "model": LLAMA_MODEL_FILE, "provider_used": "llama.cpp",
                "prompt_preview": latest_query,
                "web_context_used": bool(auto_doc.get("used")),
                "web_sources": auto_doc.get("sources", [])[:4],
                "workspace_context_used": bool(auto_workspace.get("used")),
                "workspace_matches": auto_workspace.get("matches", [])[:4],
                "tool_context_used": bool(tools.get("used")),
                "tool_results": tools.get("results", [])[:4],
                "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
                "message": _LLAMA_LOAD_ERROR or "Failed to load model.",
            }
            failure = _with_structured_response(failure)
            yield json.dumps({"type": "final", "data": failure}, ensure_ascii=False) + "\n"
            return

        yield json.dumps({"type": "status", "message": "Model ready. Planning and generating task output..."}, ensure_ascii=False) + "\n"
        _set_llama_status("generating", "Planning and generating task output")
        token_budget = _suggest_llama_max_tokens(latest_query, max(512, LLAMA_CODE_MAX_TOKENS))
        with _LLM_GENERATE_LOCK:
            if _llama_prefers_manual_completion(model):
                prompt = _build_llama_manual_prompt(conversation)
                stream = model.create_completion(
                    prompt=prompt,
                    max_tokens=token_budget,
                    temperature=0.3,
                    stop=_llama_completion_stop_tokens(),
                    stream=True,
                )
            else:
                stream = model.create_chat_completion(
                    messages=conversation,
                    max_tokens=token_budget,
                    temperature=0.3,
                    stop=["<|im_end|>", "<|endoftext|>"],
                    stream=True,
                )
            # --- Qwen3 think-block streaming filter ---
            # Buffer <think>...</think> tokens, emit them only as status updates,
            # never as visible delta text. This prevents the UI from stalling on
            # long chain-of-thought that can take 60-180s.
            _think_buf = ""
            _in_think = False
            _think_status_tick = 0
            for chunk in stream:
                if _is_cancelled():
                    break
                delta_text = _extract_llama_stream_delta(chunk)
                if not delta_text:
                    continue
                built += delta_text
                # Track open/close tags character-by-character in a look-ahead buffer
                if _in_think:
                    _think_buf += delta_text
                    if "</think>" in _think_buf:
                        _in_think = False
                        after = _think_buf[_think_buf.find("</think>") + len("</think>"):]
                        _think_buf = ""
                        if after.strip():
                            yield json.dumps({"type": "delta", "text": after}, ensure_ascii=False) + "\n"
                    else:
                        _think_status_tick += 1
                        if _think_status_tick % 20 == 1:
                            preview = _think_buf.strip().replace("\n", " ")[-60:]
                            yield json.dumps({"type": "status", "message": f"Reasoning... {preview}"}, ensure_ascii=False) + "\n"
                else:
                    if "<think>" in delta_text:
                        _in_think = True
                        before = delta_text[:delta_text.find("<think>")]
                        _think_buf = delta_text[delta_text.find("<think>") + len("<think>"):]
                        if "</think>" in _think_buf:
                            _in_think = False
                            after = _think_buf[_think_buf.find("</think>") + len("</think>"):]
                            _think_buf = ""
                            visible = before + after
                            if visible.strip():
                                yield json.dumps({"type": "delta", "text": visible}, ensure_ascii=False) + "\n"
                        else:
                            if before.strip():
                                yield json.dumps({"type": "delta", "text": before}, ensure_ascii=False) + "\n"
                            yield json.dumps({"type": "status", "message": "Reasoning..."}, ensure_ascii=False) + "\n"
                    else:
                        yield json.dumps({"type": "delta", "text": delta_text}, ensure_ascii=False) + "\n"

        if _is_cancelled():
            return

        # Strip any residual think blocks from the accumulated output
        reply = _strip_think_blocks(built.strip())
        reply = _normalize_collapsed_code_blocks(reply)
        if _query_is_ddgs_video_search_request(latest_query):
            reply = _build_ddgs_video_search_reply()
        else:
            reply = _fix_ddgs_api_hallucination(reply)
        if _reply_is_placeholder_text(reply):
            reply = "I couldn't generate a response. Please try again."

        auto_exec_rows = []
        if _query_requests_python_execution(latest_query):
            yield json.dumps({"type": "status", "message": "Running generated Python code via tools..."}, ensure_ascii=False) + "\n"
            auto_exec_rows = _auto_python_tool_chain(reply)
            exec_context = _tool_rows_to_context(auto_exec_rows)
            if exec_context:
                yield json.dumps({"type": "status", "message": "Explaining execution output..."}, ensure_ascii=False) + "\n"
                followup_conversation = list(conversation) + [
                    {"role": "assistant", "content": reply},
                    {
                        "role": "user",
                        "content": (
                            "Execution results are available below. Explain what happened based on this output, "
                            "mention package installation if any, and provide final actionable answer.\n\n"
                            + exec_context
                        ),
                    },
                ]
                explained_reply, _mode = _generate_llama_reply(
                    model,
                    followup_conversation,
                    max_tokens=min(max(512, LLAMA_CODE_MAX_TOKENS), 768),
                    temperature=0.25,
                )
                if not _reply_is_placeholder_text(explained_reply):
                    reply = _normalize_collapsed_code_blocks(explained_reply.strip())

        merged_tool_rows = (base_tool_rows + auto_exec_rows)[:8]
        result = {
            "ok": True, "reply": reply, "llm_used": True,
            "model": LLAMA_MODEL_FILE, "provider_used": "llama.cpp", "message": "ok",
            "prompt_preview": latest_query,
            "web_context_used": bool(auto_doc.get("used")),
            "web_sources": auto_doc.get("sources", [])[:4],
            "workspace_context_used": bool(auto_workspace.get("used")),
            "workspace_matches": auto_workspace.get("matches", [])[:4],
            "tool_context_used": bool(merged_tool_rows),
            "tool_results": merged_tool_rows[:4],
            "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
        }
        result = _with_structured_response(result)
        yield json.dumps({"type": "final", "data": result}, ensure_ascii=False) + "\n"
    except KeyboardInterrupt as exc:
        err_text = str(exc) or "Generation interrupted"
        yield json.dumps({"type": "error", "message": err_text}, ensure_ascii=False) + "\n"
        fallback = {
            "ok": False, "reply": "Generation was interrupted. Please retry.", "llm_used": False,
            "model": LLAMA_MODEL_FILE, "provider_used": "llama.cpp", "message": err_text,
            "prompt_preview": latest_query,
            "web_context_used": bool(auto_doc.get("used")),
            "web_sources": auto_doc.get("sources", [])[:4],
            "workspace_context_used": bool(auto_workspace.get("used")),
            "workspace_matches": auto_workspace.get("matches", [])[:4],
            "tool_context_used": bool(tools.get("used")),
            "tool_results": tools.get("results", [])[:4],
            "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
        }
        fallback = _with_structured_response(fallback)
        yield json.dumps({"type": "final", "data": fallback}, ensure_ascii=False) + "\n"
    except Exception as exc:
        err_text = str(exc)
        yield json.dumps({"type": "error", "message": err_text}, ensure_ascii=False) + "\n"
        fallback = {
            "ok": False, "reply": "", "llm_used": False,
            "model": LLAMA_MODEL_FILE, "provider_used": "llama.cpp", "message": err_text,
            "prompt_preview": latest_query,
            "web_context_used": bool(auto_doc.get("used")),
            "web_sources": auto_doc.get("sources", [])[:4],
            "workspace_context_used": bool(auto_workspace.get("used")),
            "workspace_matches": auto_workspace.get("matches", [])[:4],
            "tool_context_used": bool(tools.get("used")),
            "tool_results": tools.get("results", [])[:4],
            "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
        }
        fallback = _with_structured_response(fallback)
        yield json.dumps({"type": "final", "data": fallback}, ensure_ascii=False) + "\n"
    finally:
        if _LLAMA_LOAD_ERROR:
            _set_llama_status("error", _LLAMA_LOAD_ERROR)
        else:
            _set_llama_status("ready", "Model loaded")


def _prepare_llama_stream_context(data: dict):
    """Parse payload and build context — shared by HTTP and WebSocket stream endpoints."""
    started_at = time.perf_counter()
    messages = data.get("messages", [])
    provider = _normalize_code_provider(data.get("provider", "llama"))
    strict_mode = bool(data.get("strict_mode", True))
    code_context = str(data.get("code_context", "") or "")
    attached_context_files = data.get("attached_context_files", [])
    runtime_context = str(data.get("runtime_context", "") or "")
    candidate_paths = data.get("candidate_paths", [])
    search_web = bool(data.get("search_web", False))
    effective_search_web = bool(search_web or provider == "gpt4free")
    tool_requests = data.get("tool_requests", [])

    attached_context_text = _build_attached_context_blocks_text(attached_context_files)
    if attached_context_text:
        code_context = (code_context.rstrip() + "\n\n" + attached_context_text) if code_context.strip() else attached_context_text

    if not isinstance(messages, list):
        messages = []
    cleaned_messages = [
        {"role": str(row.get("role", "user") or "user"), "content": _compress_spaces(row.get("content", ""))[:4000]}
        for row in messages if isinstance(row, dict) and _compress_spaces(row.get("content", ""))
    ]

    latest_query = cleaned_messages[-1]["content"] if cleaned_messages else ""
    has_explicit_context = bool(str(code_context or "").strip())
    has_attached_folder_paths = isinstance(candidate_paths, list) and bool(candidate_paths)

    should_use_web_context = bool(effective_search_web)
    if should_use_web_context:
        try:
            auto_doc = _build_auto_doc_context(latest_query, max_sources=3, force_search=bool(effective_search_web))
        except Exception:
            auto_doc = {"context": "", "sources": [], "used": False}
    else:
        auto_doc = {"context": "", "sources": [], "used": False}

    if has_explicit_context:
        auto_workspace = {"context": "", "matches": [], "used": False}
    elif has_attached_folder_paths:
        try:
            auto_workspace = _build_workspace_context_for_chat(cleaned_messages, latest_query, candidate_paths=candidate_paths, max_items=5, scope="workspace")
        except Exception:
            auto_workspace = {"context": "", "matches": [], "used": False}
    else:
        auto_workspace = {"context": "", "matches": [], "used": False}

    merged_context_parts = []
    retrieved_code_context = _build_retrieved_code_context(latest_query, code_context=code_context, runtime_context=runtime_context)
    if retrieved_code_context:
        merged_context_parts.append(retrieved_code_context)
    if auto_doc.get("used") and auto_doc.get("context"):
        merged_context_parts.append(auto_doc["context"])
    if auto_workspace.get("used") and auto_workspace.get("context"):
        merged_context_parts.append(auto_workspace["context"])
    try:
        tool_bundle = _collect_tool_results_for_chat(
            latest_query,
            tool_requests=tool_requests,
            search_web=bool(effective_search_web),
        )
    except Exception as exc:
        tool_bundle = {
            "used": False,
            "results": [{"ok": False, "tool": "tool_bundle", "error": str(exc)}],
            "context": "",
        }
    if tool_bundle.get("used") and tool_bundle.get("context"):
        merged_context_parts.append(tool_bundle["context"])
    merged_context = _cap_context_text("\n\n".join(p for p in merged_context_parts if p), max_chars=14000)

    conversation = _build_code_chat_conversation(cleaned_messages, code_context=merged_context)
    return conversation, auto_doc, auto_workspace, latest_query, started_at, cleaned_messages, tool_bundle


@app.post("/api/code-chat-stream")
def code_chat_stream(payload: dict = Body(default=None)):
    from fastapi.responses import StreamingResponse

    started_at = time.perf_counter()
    data = payload or {}
    provider = _normalize_code_provider(data.get("provider", "llama"))
    selected_model = _normalize_model_name(data.get("model", ""))
    selected_g4f_provider = _normalize_provider_name(data.get("g4f_provider", ""))
    strict_mode = bool(data.get("strict_mode", True))

    if provider != "llama":
        def _provider_stream():
            yield json.dumps({"type": "status", "message": "Preparing context..."}, ensure_ascii=False) + "\n"
            try:
                conversation, auto_doc, auto_workspace, latest_query, started_at, cleaned_messages, tool_bundle = _prepare_llama_stream_context(data)
            except Exception as exc:
                err_text = str(exc) or "Failed to prepare context"
                yield json.dumps({"type": "error", "message": err_text}, ensure_ascii=False) + "\n"
                result = _with_structured_response({
                    "ok": False,
                    "reply": "",
                    "llm_used": False,
                    "model": selected_model or G4F_MODEL_ID,
                    "provider_used": "gpt4free",
                    "message": err_text,
                    "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
                })
                yield json.dumps({"type": "final", "data": result}, ensure_ascii=False) + "\n"
                return
            if not cleaned_messages:
                result = _with_structured_response({
                    "ok": False,
                    "reply": "",
                    "llm_used": False,
                    "model": selected_model or G4F_MODEL_ID,
                    "provider_used": "gpt4free",
                    "message": "No message provided.",
                    "elapsed_ms": 0,
                })
                yield json.dumps({"type": "final", "data": result}, ensure_ascii=False) + "\n"
                return
            yield from _gpt4free_stream_events(
                conversation,
                latest_query,
                started_at,
                auto_doc,
                auto_workspace,
                tool_bundle,
                selected_model=selected_model,
                selected_provider=selected_g4f_provider,
                strict_mode=strict_mode,
            )

        return StreamingResponse(_provider_stream(), media_type="application/x-ndjson")

    # --- llama path: delegate to shared helpers ---
    conversation, auto_doc, auto_workspace, latest_query, started_at, cleaned_messages, tool_bundle = _prepare_llama_stream_context(data)

    _H = {"X-Accel-Buffering": "no", "Cache-Control": "no-cache, no-transform", "Connection": "keep-alive"}

    if not cleaned_messages:
        def _empty_ev():
            data = _with_structured_response({"ok": False, "reply": "", "llm_used": False, "model": LLAMA_MODEL_FILE, "provider_used": "llama.cpp", "message": "No message provided.", "elapsed_ms": 0})
            yield json.dumps({"type": "final", "data": data}, ensure_ascii=False) + "\n"
        return StreamingResponse(("data: " + c.rstrip("\n") + "\n\n" for c in _empty_ev()), media_type="text/event-stream", headers=_H)

    def _sse():
        for chunk in _llama_stream_events(conversation, auto_doc, auto_workspace, latest_query, started_at, tool_bundle=tool_bundle):
            yield "data: " + chunk.rstrip("\n") + "\n\n"

    return StreamingResponse(_sse(), media_type="text/event-stream", headers=_H)


from fastapi import WebSocket, WebSocketDisconnect as _WebSocketDisconnect

@app.websocket("/ws/code-chat-stream")
async def ws_code_chat_stream(websocket: WebSocket):
    import asyncio as _asyncio
    await websocket.accept()
    try:
        raw = await _asyncio.wait_for(websocket.receive_text(), timeout=30)
        data = json.loads(raw)
    except Exception as exc:
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": f"Handshake failed: {exc}"}))
        except Exception:
            pass
        return

    provider = _normalize_code_provider(data.get("provider", "llama"))
    if provider != "llama":
        selected_model = _normalize_model_name(data.get("model", ""))
        selected_g4f_provider = _normalize_provider_name(data.get("g4f_provider", ""))
        strict_mode = bool(data.get("strict_mode", True))
        try:
            await websocket.send_text(json.dumps({"type": "status", "message": "Preparing context..."}))
            conversation, auto_doc, auto_workspace, latest_query, started_at, cleaned_messages, tool_bundle = _prepare_llama_stream_context(data)
            if not cleaned_messages:
                result = _with_structured_response({
                    "ok": False,
                    "reply": "",
                    "llm_used": False,
                    "model": selected_model or G4F_MODEL_ID,
                    "provider_used": "gpt4free",
                    "message": "No message provided.",
                    "elapsed_ms": 0,
                })
                await websocket.send_text(json.dumps({"type": "final", "data": result}))
                return
            for line in _gpt4free_stream_events(
                conversation,
                latest_query,
                started_at,
                auto_doc,
                auto_workspace,
                tool_bundle,
                selected_model=selected_model,
                selected_provider=selected_g4f_provider,
                strict_mode=strict_mode,
            ):
                await websocket.send_text(line.rstrip("\n"))
        except Exception:
            pass
        return

    conversation, auto_doc, auto_workspace, latest_query, started_at, cleaned_messages, tool_bundle = _prepare_llama_stream_context(data)
    loop = _asyncio.get_event_loop()
    q: _asyncio.Queue = _asyncio.Queue()
    stop_event = threading.Event()

    def _push_queue(item: str | None, timeout: int = 5) -> bool:
        if stop_event.is_set():
            return False
        try:
            _asyncio.run_coroutine_threadsafe(q.put(item), loop).result(timeout=timeout)
            return True
        except Exception:
            stop_event.set()
            return False

    def _run():
        try:
            if not cleaned_messages:
                msg = json.dumps({"type": "final", "data": {"ok": False, "reply": "", "llm_used": False, "model": LLAMA_MODEL_FILE, "provider_used": "llama.cpp", "message": "No message provided.", "elapsed_ms": 0}})
                _push_queue(msg, timeout=5)
            else:
                for line in _llama_stream_events(
                    conversation,
                    auto_doc,
                    auto_workspace,
                    latest_query,
                    started_at,
                    tool_bundle=tool_bundle,
                    cancel_event=stop_event,
                ):
                    if stop_event.is_set():
                        break
                    stripped = line.rstrip("\n")
                    if stripped:
                        if not _push_queue(stripped, timeout=10):
                            break
        except KeyboardInterrupt as exc:
            try:
                _push_queue(json.dumps({"type": "error", "message": str(exc) or "Generation interrupted"}), timeout=5)
            except Exception:
                pass
        except Exception as exc:
            try:
                _push_queue(json.dumps({"type": "error", "message": str(exc)}), timeout=5)
            except Exception:
                pass
        finally:
            stop_event.set()
            try:
                _push_queue(None, timeout=5)
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()

    try:
        while True:
            item = await _asyncio.wait_for(q.get(), timeout=360)
            if item is None:
                break
            await websocket.send_text(item)
    except (_asyncio.TimeoutError, _WebSocketDisconnect):
        stop_event.set()
        pass
    except Exception:
        stop_event.set()
        pass
    finally:
        stop_event.set()
        try:
            await websocket.close()
        except Exception:
            pass





@app.post("/api/code-context-search")
def code_context_search(payload: dict = Body(default=None)):
    data = payload or {}
    query = _compress_spaces(str(data.get("query", "") or ""))
    code_context = str(data.get("code_context", "") or "")
    max_results = int(data.get("max_results", 8) or 8)
    max_results = max(1, min(max_results, 20))

    if not query:
        return {
            "ok": False,
            "message": "Missing query.",
            "matches": [],
        }

    matches = _search_file_context_blocks(query, code_context=code_context, max_results=max_results)
    return {
        "ok": True,
        "query": query,
        "count": len(matches),
        "matches": matches,
    }


@app.post("/api/workspace-search")
def workspace_search(payload: dict = Body(default=None)):
    data = payload or {}
    query = _compress_spaces(str(data.get("query", "") or ""))
    max_results = int(data.get("max_results", 8) or 8)
    max_results = max(1, min(max_results, 25))
    candidate_paths = data.get("candidate_paths", [])
    scope = _normalize_search_scope(data.get("scope", "pc"))

    if not query:
        return {
            "ok": False,
            "message": "Missing query.",
            "matches": [],
        }

    matches = _search_workspace_files(query, max_results=max_results, candidate_paths=candidate_paths, scope=scope)
    return {
        "ok": True,
        "query": query,
        "scope": scope,
        "count": len(matches),
        "matches": matches,
    }


@app.get("/api/bot-tools")
def bot_tools_catalog():
    registry = _bot_tool_registry()
    items = []
    for name, spec in registry.items():
        items.append(
            {
                "tool": name,
                "description": str(spec.get("description", "") or "").strip(),
                "script_path": os.path.join(BOT_TOOLS_RUNTIME_DIR, str(spec.get("filename", "") or "")),
            }
        )
    return {
        "ok": True,
        "tools_dir": BOT_TOOLS_DIR,
        "runtime_dir": BOT_TOOLS_RUNTIME_DIR,
        "io_dir": BOT_TOOLS_IO_DIR,
        "tools": items,
    }


@app.post("/api/bot-tools/python-run")
def bot_tool_python_run(payload: dict = Body(default=None)):
    data = payload or {}
    code = str(data.get("code", "") or "")
    timeout = int(data.get("timeout", 60) or 60)
    cwd = str(data.get("cwd", "") or "").strip()
    return _run_bot_tool(
        "python_runner",
        payload={"code": code, "timeout": timeout, "cwd": cwd},
        timeout=timeout,
    )


@app.post("/api/bot-tools/install-package")
def bot_tool_install_package(payload: dict = Body(default=None)):
    data = payload or {}
    packages = data.get("packages", [])
    timeout = int(data.get("timeout", 180) or 180)
    upgrade = bool(data.get("upgrade", False))
    return _run_bot_tool(
        "package_installer",
        payload={"packages": packages, "timeout": timeout, "upgrade": upgrade},
        timeout=timeout,
    )


@app.post("/api/bot-tools/web-search")
def bot_tool_web_search(payload: dict = Body(default=None)):
    data = payload or {}
    query = str(data.get("query", "") or "").strip()
    limit = int(data.get("limit", 5) or 5)
    region = str(data.get("region", "wt-wt") or "wt-wt").strip() or "wt-wt"
    timeout = int(data.get("timeout", 25) or 25)
    return _run_bot_tool(
        "web_search",
        payload={"query": query, "limit": limit, "region": region},
        timeout=timeout,
    )


@app.post("/api/bot-tools/fetch-webpage")
def bot_tool_fetch_webpage(payload: dict = Body(default=None)):
    data = payload or {}
    urls = data.get("urls", [])
    query = str(data.get("query", "") or "").strip()
    timeout = int(data.get("timeout", 25) or 25)
    top_k = int(data.get("top_k", 3) or 3)
    max_chars = int(data.get("max_chars", 12000) or 12000)
    return _run_bot_tool(
        "fetch_webpage",
        payload={"urls": urls, "query": query, "timeout": timeout, "top_k": top_k, "max_chars": max_chars},
        timeout=max(35, timeout),
    )


@app.post("/api/bot-tools/scrape")
def bot_tool_scrape(payload: dict = Body(default=None)):
    data = payload or {}
    url = str(data.get("url", "") or "").strip()
    timeout = int(data.get("timeout", 25) or 25)
    max_chars = int(data.get("max_chars", 6000) or 6000)
    return _run_bot_tool(
        "scrape_url",
        payload={"url": url, "timeout": timeout, "max_chars": max_chars},
        timeout=timeout,
    )


@app.post("/api/bot-tools/execute")
def bot_tool_execute(payload: dict = Body(default=None)):
    data = payload or {}
    tool_name = str(data.get("tool", "") or "").strip()
    tool_input = data.get("input", {}) if isinstance(data.get("input"), dict) else {}
    timeout = int(data.get("timeout", BOT_TOOL_TIMEOUT_SEC) or BOT_TOOL_TIMEOUT_SEC)
    if not tool_name:
        return {"ok": False, "error": "Missing tool name."}
    return _run_bot_tool(tool_name, payload=tool_input, timeout=timeout)


@app.post("/api/switch-model")
def switch_model(payload: dict = Body(default=None)):
    """Switch local model tier at runtime without restarting the server."""
    data = payload or {}
    filename = str(data.get("filename", "") or "").strip()
    if not filename:
        return {"ok": False, "message": "Missing filename"}
    # Validate it's a known tier
    known = {t[2] for t in _MODEL_TIERS}
    if filename not in known:
        return {"ok": False, "message": f"Unknown model: {filename}. Known: {sorted(known)}"}
    repo = str(data.get("repo", "") or "").strip()
    n_ctx = int(data.get("n_ctx", 0) or 0)
    _switch_model_to(filename, repo=repo, n_ctx=n_ctx)
    tier_info = next((t for t in _MODEL_TIERS if t[2] == filename), None)
    label = tier_info[4] if tier_info else filename
    return {
        "ok": True,
        "message": f"Switched to {label}",
        "filename": filename,
        "label": label,
        "state": _get_llama_status().get("state", "switching"),
    }


@app.get("/api/system-info")
def system_info():
    """Returns RAM, auto-selected model tier, and current model in use."""
    cached_models = []
    try:
        if os.path.isdir(LLAMA_MODELS_DIR):
            cached_models = sorted(
                [name for name in os.listdir(LLAMA_MODELS_DIR) if str(name or "").lower().endswith(".gguf")]
            )
    except Exception:
        cached_models = []
    return {
        "ok": True,
        "ram_gb": round(_SYSTEM_RAM_GB, 1),
        "usable_ram_gb": round(_AUTO_MODEL_TIER.get("usable_gb", _SYSTEM_RAM_GB * 0.05), 1),
        "auto_model": {
            "filename": _AUTO_MODEL_TIER["filename"],
            "repo": _AUTO_MODEL_TIER["repo"],
            "label": _AUTO_MODEL_TIER["label"],
            "n_ctx": _AUTO_MODEL_TIER["n_ctx"],
        },
        "active_model": LLAMA_MODEL_FILE,
        "model_overridden": bool(_MODEL_SELECTION_OVERRIDDEN),
        "model_selection_source": _MODEL_SELECTION_SOURCE,
        "cached_models": cached_models,
        "llama_available": _llama_available(),
        "llama_state": _get_llama_status().get("state", "idle"),
        "llama_message": _get_llama_status().get("message", ""),
        "all_tiers": [
            {"min_ram_gb": t[0], "filename": t[2], "label": t[4]}
            for t in _MODEL_TIERS
        ],
    }


@app.get("/api/code-provider-status")
def code_provider_status(provider: str = Query(default="llama")):
    selected_provider = _normalize_code_provider(provider)
    if selected_provider == "llama":
        status = _get_llama_status()
        return {
            "ok": True,
            "provider": "llama",
            "state": status.get("state", "idle"),
            "message": status.get("message", ""),
            "updated_at": status.get("updated_at", 0.0),
        }
    return {
        "ok": True,
        "provider": "gpt4free",
        "state": "ready",
        "message": "g4f provider selected",
        "updated_at": time.time(),
    }


@app.get("/api/g4f-catalog")
def g4f_catalog():
    configured_providers = [
        name for name, _ in _resolve_g4f_provider_candidates()
        if str(name or "").strip().lower() != "auto"
    ]
    configured_models = _resolve_g4f_model_candidates()
    available_providers = _list_g4f_provider_catalog()
    available_models = _resolve_g4f_free_model_options()

    return {
        "ok": True,
        "g4f_installed": bool(_G4F_OK),
        "configured_provider_chain": configured_providers,
        "configured_models": configured_models,
        "available_providers": available_providers,
        "available_models": available_models,
        "provider_count": len(available_providers),
        "model_count": len(available_models),
        "message": (
            "g4f detected"
            if _G4F_OK
            else "g4f not installed on this runtime; install with pip install g4f"
        ),
        "updated_at": time.time(),
    }


@app.post("/api/run-python")
def run_python(payload: dict = Body(default=None)):
    data = payload or {}
    code = str(data.get("code", "") or "")
    allow_file_write = bool(data.get("allow_file_write", False))
    timeout = int(data.get("timeout", 60) or 60)
    timeout = max(1, min(timeout, 120))
    attached_files = data.get("attached_files", [])
    if not isinstance(attached_files, list):
        attached_files = []
    attached_files = attached_files[:10]

    if not code.strip():
        return {
            "ok": False,
            "stdout": "",
            "stderr": "No Python code provided.",
            "exit_code": -1,
        }

    if len(code) > 30000:
        return {
            "ok": False,
            "stdout": "",
            "stderr": "Code is too large to run.",
            "exit_code": -1,
        }

    write_ops = _detect_python_write_ops(code)
    if write_ops and not allow_file_write:
        return {
            "ok": False,
            "stdout": "",
            "stderr": (
                "Write operations detected and blocked pending approval. "
                "Resend with allow_file_write=true to apply file modifications."
            ),
            "exit_code": -1,
            "requires_approval": True,
            "detected_write_ops": write_ops,
        }

    try:
        with tempfile.TemporaryDirectory(prefix="runpy_") as work_dir:
            staged_files = []
            total_bytes = 0

            for item in attached_files:
                if not isinstance(item, dict):
                    continue
                name = os.path.basename(str(item.get("name", "") or "").strip())
                if not name:
                    continue
                file_b64 = str(item.get("base64", "") or "").strip()
                if not file_b64:
                    continue
                try:
                    raw = base64.b64decode(file_b64, validate=False)
                except Exception:
                    continue
                if len(raw) > 12 * 1024 * 1024:
                    continue
                total_bytes += len(raw)
                if total_bytes > 40 * 1024 * 1024:
                    break

                safe_name = re.sub(r"[^A-Za-z0-9 ._-]", "_", name).strip(" ._") or "file"
                target = os.path.join(work_dir, safe_name)
                if os.path.exists(target):
                    stem, ext = os.path.splitext(safe_name)
                    idx = 2
                    while os.path.exists(os.path.join(work_dir, f"{stem}_{idx}{ext}")):
                        idx += 1
                    target = os.path.join(work_dir, f"{stem}_{idx}{ext}")
                    safe_name = os.path.basename(target)

                with open(target, "wb") as fh:
                    fh.write(raw)
                staged_files.append({"name": safe_name, "size": len(raw)})

            staged_names = [row["name"] for row in staged_files]
            pre_run_snapshot = {}
            for entry in os.listdir(work_dir):
                entry_path = os.path.join(work_dir, entry)
                if not os.path.isfile(entry_path):
                    continue
                try:
                    stat = os.stat(entry_path)
                except OSError:
                    continue
                pre_run_snapshot[entry] = {
                    "size": int(stat.st_size),
                    "mtime_ns": int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
                }
            preface = (
                "# Attached files are available in current working directory.\n"
                f"# Files: {', '.join(staged_names) if staged_names else '(none)'}\n\n"
            )
            run_code = preface + code

            env = os.environ.copy()
            env["ATTACHED_FILES_JSON"] = json.dumps(staged_names, ensure_ascii=False)
            env["PYTHONUNBUFFERED"] = "1"
            completed = subprocess.run(
                [sys.executable, "-u", "-c", run_code],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=work_dir,
                env=env,
            )
            post_run_snapshot = {}
            for entry in os.listdir(work_dir):
                entry_path = os.path.join(work_dir, entry)
                if not os.path.isfile(entry_path):
                    continue
                try:
                    stat = os.stat(entry_path)
                except OSError:
                    continue
                post_run_snapshot[entry] = {
                    "size": int(stat.st_size),
                    "mtime_ns": int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
                }

            artifacts = []
            for name, meta in post_run_snapshot.items():
                before = pre_run_snapshot.get(name)
                if before is None:
                    artifacts.append({"name": name, "change": "created", "size": meta["size"]})
                    continue
                if before.get("size") != meta.get("size") or before.get("mtime_ns") != meta.get("mtime_ns"):
                    artifacts.append({"name": name, "change": "modified", "size": meta["size"]})

            return {
                "ok": completed.returncode == 0,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "exit_code": completed.returncode,
                "attached_files": staged_names,
                "artifacts": artifacts[:30],
            }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "stdout": exc.stdout or "",
            "stderr": (exc.stderr or "") + "\nExecution timed out.",
            "exit_code": -1,
        }
    except Exception as exc:
        return {
            "ok": False,
            "stdout": "",
            "stderr": str(exc),
            "exit_code": -1,
        }
    finally:
        pass


@app.post("/api/run-shell")
def run_shell(payload: dict = Body(default=None)):
    data = payload or {}
    command = str(data.get("command", "") or "").strip()
    lang = str(data.get("lang", "") or "").strip().lower()
    timeout = int(data.get("timeout", 20) or 20)
    timeout = max(1, min(timeout, 90))

    if not command:
        return {
            "ok": False,
            "stdout": "",
            "stderr": "No shell command provided.",
            "exit_code": -1,
        }

    if len(command) > 5000:
        return {
            "ok": False,
            "stdout": "",
            "stderr": "Command is too large to run.",
            "exit_code": -1,
        }

    cmd = command
    pip_match = re.match(r"^\s*(pip3?|python\s+-m\s+pip)\b", command, flags=re.IGNORECASE)
    if pip_match:
        suffix = command[pip_match.end():].strip()
        cmd = f'"{sys.executable}" -m pip {suffix}'.strip()

    workspace_dir = os.path.dirname(os.path.abspath(__file__))
    env = os.environ.copy()
    run_args = None
    use_shell = False

    if lang in {"powershell", "ps1"} and shutil.which("powershell"):
        run_args = ["powershell", "-NoProfile", "-Command", cmd]
    elif lang in {"cmd", "bat"}:
        run_args = ["cmd", "/c", cmd]
    elif lang in {"bash", "sh", "shell", "zsh"} and os.name != "nt" and shutil.which("bash"):
        run_args = ["bash", "-lc", cmd]
    else:
        run_args = cmd
        use_shell = True

    try:
        completed = subprocess.run(
            run_args,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=workspace_dir,
            env=env,
            shell=use_shell,
        )
        return {
            "ok": completed.returncode == 0,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "exit_code": completed.returncode,
            "command": cmd,
            "lang": lang or "shell",
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "stdout": exc.stdout or "",
            "stderr": (exc.stderr or "") + "\nCommand timed out.",
            "exit_code": -1,
            "command": cmd,
            "lang": lang or "shell",
        }
    except Exception as exc:
        return {
            "ok": False,
            "stdout": "",
            "stderr": str(exc),
            "exit_code": -1,
            "command": cmd,
            "lang": lang or "shell",
        }


@app.post("/api/app/close")
def close_app(payload: dict = Body(default=None)):
    _delayed_exit(delay_seconds=0.2, code=0)
    return {
        "ok": True,
        "message": "Application is closing.",
    }


@app.post("/api/app/restart")
def restart_app(payload: dict = Body(default=None)):
    desktop_mode = os.getenv("APP_MODE", "").strip().lower() == "desktop"
    if not desktop_mode:
        return {
            "ok": False,
            "message": "Restart is available only in desktop mode.",
        }
    _delayed_restart(delay_seconds=0.3)
    return {
        "ok": True,
        "message": "Application is restarting.",
    }


@app.post("/api/open-file")
def open_file(payload: dict = Body(default=None)):
    data = payload or {}
    raw_path = str(data.get("path", "") or "").strip()
    line = int(data.get("line", 0) or 0)
    if not raw_path:
        return {"ok": False, "message": "Missing file path."}

    workspace_root = os.path.dirname(os.path.abspath(__file__))
    normalized_rel = raw_path.replace("\\", "/").lstrip("/")
    candidate = os.path.normpath(os.path.join(workspace_root, normalized_rel))
    root_norm = os.path.normpath(workspace_root)
    if not os.path.normcase(candidate).startswith(os.path.normcase(root_norm + os.sep)) and os.path.normcase(candidate) != os.path.normcase(root_norm):
        return {"ok": False, "message": "Path is outside workspace."}
    if not os.path.isfile(candidate):
        return {"ok": False, "message": f"File not found: {raw_path}"}

    try:
        if os.name == "nt":
            line_arg = max(1, line)
            code_cmd = ["code", "-g", f"{candidate}:{line_arg}"]
            try:
                subprocess.Popen(code_cmd, cwd=workspace_root, creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0), close_fds=False)
            except Exception:
                os.startfile(candidate)
        else:
            subprocess.Popen(["xdg-open", candidate], cwd=workspace_root)
        return {"ok": True, "message": "Opened file.", "path": raw_path, "line": max(1, line) if line > 0 else 0}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}
