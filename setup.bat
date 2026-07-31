@echo off
setlocal EnableDelayedExpansion
title AI Search Engine - Installer

set "SCRIPT_DIR=%~dp0"
set "BASE_HOME=%USERPROFILE%"
if not defined BASE_HOME set "BASE_HOME=%HOMEDRIVE%%HOMEPATH%"
if not defined BASE_HOME set "BASE_HOME=%cd%"
set "INSTALL_DIR=%BASE_HOME%\AISearchEngine"
set "HF_BASE=https://huggingface.co/spaces/Harikirankumar/ml-ai-platform/resolve/main"
set "MODELS_DIR="
set "IS_UPDATE=0"

echo.
echo  =====================================================
echo    AI Search Engine - Local Installer
echo    Free, Private, No API Keys Required
echo  =====================================================
echo.

:: 1) Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+ from https://www.python.org
    echo         Make sure to enable "Add Python to PATH" during installation.
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set "PYVER=%%v"
echo [OK] Python found: %PYVER%

:: 2) Install location
echo.
if defined CUSTOM_INSTALL_DIR set "INSTALL_DIR=%CUSTOM_INSTALL_DIR%"
if not defined INSTALL_DIR set "INSTALL_DIR=%BASE_HOME%\AISearchEngine"
echo  Install location: !INSTALL_DIR!
echo  Tip: set CUSTOM_INSTALL_DIR to override this path.

echo [*] Resolved install directory: !INSTALL_DIR!

if not exist "!INSTALL_DIR!" mkdir "!INSTALL_DIR!"
if errorlevel 1 (
    echo [ERROR] Failed to create install directory: !INSTALL_DIR!
    pause
    exit /b 1
)
echo [OK] Install directory: !INSTALL_DIR!

if exist "%INSTALL_DIR%\app.py" set "IS_UPDATE=1"
if "%IS_UPDATE%"=="1" (
    echo [*] Existing installation detected.
    echo [*] Running in UPDATE mode - files and packages will be refreshed.
) else (
    echo [*] Running in FRESH install mode.
)

:: 3) Download app files
set "APP_SOURCE=HF"
if exist "%SCRIPT_DIR%app.py" if exist "%SCRIPT_DIR%templates\index.html" set "APP_SOURCE=LOCAL"

echo.
if "%APP_SOURCE%"=="LOCAL" (
    echo [*] Using local installer folder files.
    copy /Y "%SCRIPT_DIR%app.py" "%INSTALL_DIR%\app.py" >nul
    if errorlevel 1 ( echo [ERROR] Failed to copy app.py & pause & exit /b 1 )
    if exist "%SCRIPT_DIR%desktop.py" (
        copy /Y "%SCRIPT_DIR%desktop.py" "%INSTALL_DIR%\desktop.py" >nul
        if errorlevel 1 ( echo [ERROR] Failed to copy desktop.py & pause & exit /b 1 )
    )
    if not exist "%INSTALL_DIR%\templates" mkdir "%INSTALL_DIR%\templates"
    copy /Y "%SCRIPT_DIR%templates\index.html" "%INSTALL_DIR%\templates\index.html" >nul
    if errorlevel 1 ( echo [ERROR] Failed to copy templates\index.html & pause & exit /b 1 )
    if exist "%SCRIPT_DIR%requirements.txt" (
        copy /Y "%SCRIPT_DIR%requirements.txt" "%INSTALL_DIR%\requirements.txt" >nul
        if errorlevel 1 ( echo [WARN] Failed to copy requirements.txt. Fallback install list will be used. )
    )
    if exist "%SCRIPT_DIR%icon.ico" (
        copy /Y "%SCRIPT_DIR%icon.ico" "%INSTALL_DIR%\icon.ico" >nul
        if errorlevel 1 ( echo [WARN] Failed to copy icon.ico. Shortcut will use default icon. )
    )
) else (
    echo [*] Downloading app files from HuggingFace...
    powershell -NoProfile -Command "& { $ErrorActionPreference='Stop'; Invoke-WebRequest '%HF_BASE%/app.py' -OutFile '%INSTALL_DIR%\app.py' }"
    if errorlevel 1 ( echo [ERROR] Failed to download app.py & pause & exit /b 1 )
    powershell -NoProfile -Command "& { $ErrorActionPreference='Stop'; Invoke-WebRequest '%HF_BASE%/desktop.py' -OutFile '%INSTALL_DIR%\desktop.py' }"
    if errorlevel 1 ( echo [WARN] Failed to download desktop.py. Browser launch mode will be used. )
    if not exist "%INSTALL_DIR%\templates" mkdir "%INSTALL_DIR%\templates"
    powershell -NoProfile -Command "& { $ErrorActionPreference='Stop'; Invoke-WebRequest '%HF_BASE%/templates/index.html' -OutFile '%INSTALL_DIR%\templates\index.html' }"
    if errorlevel 1 ( echo [ERROR] Failed to download templates\index.html & pause & exit /b 1 )
    powershell -NoProfile -Command "& { $ErrorActionPreference='Stop'; Invoke-WebRequest '%HF_BASE%/requirements.txt' -OutFile '%INSTALL_DIR%\requirements.txt' }"
    if errorlevel 1 ( echo [WARN] Failed to download requirements.txt. Fallback install list will be used. )
    powershell -NoProfile -Command "& { $ErrorActionPreference='SilentlyContinue'; Invoke-WebRequest '%HF_BASE%/icon.ico' -OutFile '%INSTALL_DIR%\icon.ico' }" 2>nul
    if errorlevel 1 ( echo [WARN] Failed to download icon.ico. Shortcut will use default icon. )
)
echo [OK] App files ready.

:: 4) Create/reuse virtual environment
echo.
cd /d "%INSTALL_DIR%"
if exist "%INSTALL_DIR%\.venv\Scripts\python.exe" (
    echo [*] Reusing existing virtual environment.
) else (
    echo [*] Creating Python virtual environment...
    python -m venv .venv
    if errorlevel 1 ( echo [ERROR] Failed to create .venv & pause & exit /b 1 )
    echo [OK] Virtual environment created.
)

:: 5) Install dependencies
echo.
echo [*] Installing dependencies (this may take a few minutes)...
call "%INSTALL_DIR%\.venv\Scripts\activate.bat"
if errorlevel 1 ( echo [ERROR] Failed to activate .venv & pause & exit /b 1 )

python -m pip install --upgrade pip setuptools wheel -q
if errorlevel 1 ( echo [WARN] Failed to upgrade pip/setuptools/wheel. Continuing... )

if exist "%INSTALL_DIR%\requirements.txt" (
    echo [*] Installing from requirements.txt...
    python -m pip install -r "%INSTALL_DIR%\requirements.txt" -q
    if errorlevel 1 (
        echo [WARN] requirements.txt install failed. Trying fallback package list...
        python -m pip install fastapi==0.115.0 uvicorn==0.30.6 jinja2==3.1.4 ddgs==9.14.4 trafilatura==2.1.0 lxml_html_clean==0.4.5 beautifulsoup4 readability-lxml sumy nltk huggingface_hub g4f requests openpyxl pandas -q
        if errorlevel 1 ( echo [ERROR] Fallback dependency install failed & pause & exit /b 1 )
    )
) else (
    echo [*] requirements.txt not found. Installing fallback package list...
    python -m pip install fastapi==0.115.0 uvicorn==0.30.6 jinja2==3.1.4 ddgs==9.14.4 trafilatura==2.1.0 lxml_html_clean==0.4.5 beautifulsoup4 readability-lxml sumy nltk huggingface_hub g4f requests openpyxl pandas -q
    if errorlevel 1 ( echo [ERROR] Dependency install failed & pause & exit /b 1 )
)

python -m pip install pywebview -q
if errorlevel 1 ( echo [WARN] pywebview install failed. App may open in browser fallback mode. )

echo [OK] Packages installed.

echo [*] Downloading NLTK data...
python -c "import nltk; nltk.download('punkt', quiet=True); nltk.download('punkt_tab', quiet=True); nltk.download('stopwords', quiet=True); print('[OK] NLTK data ready')"

:: 6) Optional local model
echo.
echo  -------------------------------------------------------
echo  Optional: Local Qwen model ^(~900MB^)
echo  - Works offline
if "%IS_UPDATE%"=="1" (
  if exist "%INSTALL_DIR%\models\qwen2.5-coder-1.5b-instruct-q4_k_m.gguf" echo  - Existing model detected; you can skip this
)
echo  -------------------------------------------------------
set /p "DL_MODEL=  Download/update local Qwen model? (y/N): "
if /i "!DL_MODEL!"=="y" (
    echo [*] Installing llama-cpp-python ^(prebuilt CPU wheel^)...
    python -m pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu -q
    if errorlevel 1 (
        echo [WARN] llama-cpp-python install failed. App will still work with g4f cloud AI.
        goto :skip_model
    )
    if not exist "%INSTALL_DIR%\models" mkdir "%INSTALL_DIR%\models"
    set "MODELS_DIR=%INSTALL_DIR%\models"
    echo [*] Downloading Qwen GGUF model from HuggingFace...
    python -c "import os; from huggingface_hub import hf_hub_download; p=hf_hub_download(repo_id='Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF', filename='qwen2.5-coder-1.5b-instruct-q4_k_m.gguf', local_dir=os.environ['MODELS_DIR']); print('[OK] Model saved:', p)"
    if errorlevel 1 (
        echo [WARN] Model download failed. App will use g4f cloud AI.
    ) else (
        echo [OK] Local Qwen model ready.
    )
)
:skip_model

:: 7) Create helper files + launcher

echo [*] Creating launch scripts...
(
    echo import time, socket, webbrowser
    echo for _ in range^(45^):
    echo^    try:
    echo^        s = socket.create_connection^(^('127.0.0.1', 9191^), 1^)
    echo^        s.close^(^)
    echo^        break
    echo^    except Exception:
    echo^        time.sleep^(1^)
    echo webbrowser.open^('http://127.0.0.1:9191'^)
) > "%INSTALL_DIR%\wait_open.py"
if errorlevel 1 (
    echo [ERROR] Failed to create wait_open.py
    pause
    exit /b 1
)
echo [OK] wait_open.py created

(
    echo @echo off
    echo title AI Search Engine
    echo cd /d "%INSTALL_DIR%"
    echo call ".venv\Scripts\activate.bat"
    echo echo Starting AI Search Engine desktop app...
    echo set "PYW=.venv\Scripts\pythonw.exe"
    echo set "PYC=.venv\Scripts\python.exe"
    echo if exist "desktop.py" ^(
    echo^  if exist "%%PYW%%" ^(
    echo^    start "" "%%PYW%%" desktop.py
    echo^  ^) else ^(
    echo^    start "" "%%PYC%%" desktop.py
    echo^  ^)
    echo ^) else ^(
    echo^  echo [WARN] desktop.py not found. Falling back to browser mode.
    echo^  start "" "%%PYC%%" -m uvicorn app:app --host 127.0.0.1 --port 9191
    echo^  "%%PYC%%" wait_open.py
    echo ^)
    echo echo.
    echo exit /b 0
) > "%INSTALL_DIR%\Start Search Engine.bat"
if errorlevel 1 (
    echo [ERROR] Failed to create Start Search Engine.bat
    pause
    exit /b 1
)
echo [OK] Start Search Engine.bat created

set "DESKTOP_DIR="
for /f "usebackq delims=" %%d in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do set "DESKTOP_DIR=%%d"
if not defined DESKTOP_DIR set "DESKTOP_DIR=%USERPROFILE%\Desktop"

:: Determine icon location for the shortcut
set "SHORTCUT_ICON=%INSTALL_DIR%\icon.ico"
if not exist "%SHORTCUT_ICON%" set "SHORTCUT_ICON=%SystemRoot%\System32\shell32.dll,23"

:: Create a proper .lnk shortcut (with icon) via PowerShell WScript.Shell
set "LNK_TARGET=%DESKTOP_DIR%\AI Search Engine.lnk"
powershell -NoProfile -Command ^
  "$ws = New-Object -ComObject WScript.Shell;" ^
  "$lnk = $ws.CreateShortcut('%LNK_TARGET%');" ^
  "$lnk.TargetPath = '%INSTALL_DIR%\Start Search Engine.bat';" ^
  "$lnk.WorkingDirectory = '%INSTALL_DIR%';" ^
  "$lnk.IconLocation = '%SHORTCUT_ICON%';" ^
  "$lnk.Description = 'AI Search Engine - local private AI';" ^
  "$lnk.WindowStyle = 1;" ^
  "$lnk.Save()"
if errorlevel 1 (
    echo [WARN] Could not create .lnk shortcut. Falling back to .bat copy.
    copy /Y "%INSTALL_DIR%\Start Search Engine.bat" "%DESKTOP_DIR%\AI Search Engine.bat" >nul
    if errorlevel 1 (
        echo [WARN] Could not create shortcut in %DESKTOP_DIR%
    ) else (
        echo [OK] Desktop shortcut created: %DESKTOP_DIR%\AI Search Engine.bat
    )
) else (
    echo [OK] Desktop shortcut created: %LNK_TARGET%
)

if defined OneDrive (
    if exist "%OneDrive%\Desktop" (
        if /i not "%OneDrive%\Desktop"=="%DESKTOP_DIR%" (
            set "LNK_OD=%OneDrive%\Desktop\AI Search Engine.lnk"
            powershell -NoProfile -Command ^
              "$ws = New-Object -ComObject WScript.Shell;" ^
              "$lnk = $ws.CreateShortcut('!LNK_OD!');" ^
              "$lnk.TargetPath = '%INSTALL_DIR%\Start Search Engine.bat';" ^
              "$lnk.WorkingDirectory = '%INSTALL_DIR%';" ^
              "$lnk.IconLocation = '%SHORTCUT_ICON%';" ^
              "$lnk.Description = 'AI Search Engine - local private AI';" ^
              "$lnk.WindowStyle = 1;" ^
              "$lnk.Save()"
            if errorlevel 1 (
                copy /Y "%INSTALL_DIR%\Start Search Engine.bat" "%OneDrive%\Desktop\AI Search Engine.bat" >nul
                if errorlevel 1 (
                    echo [WARN] Could not create shortcut in %OneDrive%\Desktop
                ) else (
                    echo [OK] OneDrive Desktop shortcut created: %OneDrive%\Desktop\AI Search Engine.bat
                )
            ) else (
                echo [OK] OneDrive Desktop shortcut created: !LNK_OD!
            )
        )
    )
)

:: 8) Done
echo.
echo  =====================================================
echo    Installation Complete!
echo  =====================================================
echo.
echo  To start the app:
echo    - Double-click 'AI Search Engine' on your Desktop
echo    - Or run: %INSTALL_DIR%\Start Search Engine.bat
echo.
echo  App URL: http://127.0.0.1:9191
echo.

set /p "START_NOW=  Start now? (Y/n): "
if /i not "!START_NOW!"=="n" start "" "%INSTALL_DIR%\Start Search Engine.bat"

echo.
echo  Installer finished.
echo  Install folder: %INSTALL_DIR%
echo  Launcher file: %INSTALL_DIR%\Start Search Engine.bat
echo.
pause

endlocal
exit /b 0
