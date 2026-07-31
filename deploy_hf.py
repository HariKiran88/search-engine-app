"""
Deploy this project to a Hugging Face Docker Space.

Usage (PowerShell):
    # Optional (script can auto-detect token from huggingface-cli login)
    $env:HF_TOKEN="hf_xxx"
    $env:HF_REPO_ID="username/space-name"
  python deploy_hf.py
"""

from pathlib import Path
import os
import shutil
import sys
import tempfile

try:
    from huggingface_hub import HfApi
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "huggingface_hub", "-q"])
    from huggingface_hub import HfApi


BASE = Path(__file__).parent.resolve()
HF_DIR = BASE / "hf_space"
DEFAULT_REPO_ID = "Harikirankumar/ml-ai-platform"
ENV_FILE = BASE / ".env"


def _parse_env_file(path: Path) -> dict:
    values = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _load_local_env():
    for key, value in _parse_env_file(ENV_FILE).items():
        os.environ.setdefault(key, value)


def _persist_local_env(token: str, repo_id: str):
    current = _parse_env_file(ENV_FILE)
    current["HF_TOKEN"] = token
    current["HF_REPO_ID"] = repo_id

    lines = [
        "HF_TOKEN=" + current.get("HF_TOKEN", ""),
        "HF_REPO_ID=" + current.get("HF_REPO_ID", DEFAULT_REPO_ID),
    ]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _resolve_repo_id() -> str:
    return os.getenv("HF_REPO_ID", "").strip() or DEFAULT_REPO_ID


def _resolve_token() -> str:
    token = os.getenv("HF_TOKEN", "").strip()
    if token:
        return token

    try:
        from huggingface_hub import get_token
        cached = (get_token() or "").strip()
        if cached:
            return cached
    except Exception:
        try:
            from huggingface_hub import HfFolder
            cached = (HfFolder.get_token() or "").strip()
            if cached:
                return cached
        except Exception:
            pass

    try:
        typed = input("Enter Hugging Face token (starts with hf_): ").strip()
    except EOFError:
        typed = ""

    if not typed:
        raise SystemExit(
            "No Hugging Face token found.\n"
            "Run one of these, then run deploy_hf.py again:\n"
            "  1) huggingface-cli login\n"
            "  2) set HF_TOKEN in .env"
        )
    return typed


def _copy_if_exists(src: Path, dst: Path):
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _build_stage_dir(stage_dir: Path):
    # Space runtime files from hf_space/
    _copy_if_exists(HF_DIR / "README.md", stage_dir / "README.md")
    _copy_if_exists(HF_DIR / "Dockerfile", stage_dir / "Dockerfile")
    _copy_if_exists(HF_DIR / "requirements.txt", stage_dir / "requirements.txt")

    # App files from project root
    _copy_if_exists(BASE / "app.py", stage_dir / "app.py")
    _copy_if_exists(BASE / "desktop.py", stage_dir / "desktop.py")
    _copy_if_exists(BASE / "setup.bat", stage_dir / "setup.bat")
    _copy_if_exists(BASE / "icon.ico", stage_dir / "icon.ico")
    _copy_if_exists(BASE / "create_icon.py", stage_dir / "create_icon.py")

    templates_src = BASE / "templates"
    templates_dst = stage_dir / "templates"
    if templates_src.exists() and templates_src.is_dir():
        shutil.copytree(templates_src, templates_dst, dirs_exist_ok=True)


def main():
    _load_local_env()
    token = _resolve_token()
    repo_id = _resolve_repo_id()
    _persist_local_env(token=token, repo_id=repo_id)

    api = HfApi(token=token)

    space_exists = False
    try:
        api.repo_info(repo_id=repo_id, repo_type="space", token=token)
        space_exists = True
        print(f"[*] Found existing Space: https://huggingface.co/spaces/{repo_id}")
    except Exception:
        space_exists = False

    if not space_exists:
        print(f"[*] Space not found. Trying to create: https://huggingface.co/spaces/{repo_id}")
        try:
            api.create_repo(repo_id=repo_id, repo_type="space", space_sdk="docker", exist_ok=True, token=token)
            print("[*] Space created successfully.")
        except Exception as exc:
            message = str(exc)
            if "402" in message or "Payment Required" in message:
                raise SystemExit(
                    "Cannot create a Docker Space on this Hugging Face plan (402).\n"
                    "For now, create a Docker Space manually in the HF UI (or upgrade to PRO),\n"
                    "then re-run this script to upload files to that existing Space."
                )
            raise

    with tempfile.TemporaryDirectory(prefix="hf-space-stage-") as tmp:
        stage_dir = Path(tmp)
        _build_stage_dir(stage_dir)

        print("[*] Uploading staged files...")
        api.upload_folder(
            folder_path=str(stage_dir),
            repo_id=repo_id,
            repo_type="space",
            commit_message="Deploy Search Engine",
            token=token,
        )

    print(f"[DONE] Deployed to https://huggingface.co/spaces/{repo_id}")
    print("       Build usually takes a few minutes in Spaces.")


if __name__ == "__main__":
    main()
