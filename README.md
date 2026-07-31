# Search Engine

A minimal Search Engine project built with FastAPI.

## Features
- Simple web UI
- `/api/search` endpoint
- DuckDuckGo-powered web search (web-first)
- Query cleanup + candidate generation for better relevance
- Domain-aware boosting (for example Microsoft/FastAPI/Python docs)
- Local in-memory fallback if web search is unavailable
- Local AI mode (Qwen)
- Local coding sidebar assistant (Qwen2.5-Coder via llama.cpp)
- Optional second coding provider: gpt4free (Qwen coder)
- Attach local code files or folders as chat context
- Reuse Python run output automatically in follow-up chat prompts
- Desktop app launcher (`desktop.py`)

## Run locally
1. Create/activate a Python environment
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Start the app:
   ```bash
   uvicorn app:app --reload
   ```
  Note: this command keeps running until you stop it (Ctrl+C). That is expected for a web server.
4. Open:
   - UI: `http://127.0.0.1:8000`
   - API: `http://127.0.0.1:8000/api/search?q=python`

  ## Deploy to Hugging Face Space
  1. Set environment variables (PowerShell):
    ```powershell
    $env:HF_TOKEN="hf_xxx"
    $env:HF_REPO_ID="username/space-name"
    ```
  2. Run one-shot deploy:
    ```bash
    python deploy_hf.py
    ```
  3. Open:
    - `https://huggingface.co/spaces/<username>/<space-name>`

  Notes:
  - `deploy_hf.py` exits after upload (it should not run forever).
  - Space build logs can continue for a few minutes in Hugging Face after upload.

## Desktop app
Run as a desktop window:

```bash
python desktop.py
```

Note: first run can take longer because the local Qwen coder GGUF model is downloaded from Hugging Face.

### Windows one-click installer
- Download `setup.bat` and run it.
- The installer now pulls required app files (`app.py`, `desktop.py`, `templates/index.html`, `requirements.txt`) and installs dependencies automatically.
- If `requirements.txt` install fails, it falls back to a pinned dependency set so setup can still complete.
- Optional local model download is prompted at the end.

## API
`GET /api/search?q=<query>&limit=<n>`

`POST /api/code-chat`

Example payload:
```json
{
  "messages": [
    {"role": "user", "content": "Write a Python function to reverse a linked list."}
  ],
  "provider": "llama",
  "code_context": "Optional pasted code, attached file snippets, or traceback",
  "runtime_context": "Optional runtime output from an earlier code execution"
}
```

Response:
```json
{
  "query": "python",
  "count": 5,
  "source": "duckduckgo",
  "results": [
    {
      "id": 1,
      "title": "Welcome to Python.org",
      "content": "The official home of Python programming language...",
      "url": "https://www.python.org/",
      "score": 19,
      "source": "duckduckgo"
    }
  ]
}
```

Notes:
- `source` is `duckduckgo` when web results are returned.
- `source` is `local` when the app falls back to local sample documents.

## g4f provider notes
- `g4f` is a multi-provider client, not a guaranteed always-free single backend.
- Per official docs, provider availability can depend on API keys, cookies/HAR login, quotas, or provider-specific limits.
- This app supports `provider: "gpt4free"` and will try configured provider/model candidates.

Environment variables (optional):
- `G4F_PROVIDER`: preferred provider name (example: `PollinationsAI`)
- `G4F_PROVIDER_CHAIN`: comma/space list of providers to try before auto mode
- `G4F_MODEL_ID`: preferred model alias
- `G4F_MODEL_CANDIDATES`: fallback model aliases list
