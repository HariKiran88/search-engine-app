---
title: AI Search Engine
emoji: 🔍
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: Free AI search engine — DuckDuckGo + g4f
---

# 🔍 AI Search Engine

A fully free, privacy-friendly AI search engine built with **FastAPI**, **DuckDuckGo**, and **gpt4free**.

## Features
- 🌐 **Web search** via DuckDuckGo (no tracking)
- ✨ **AI Overview** powered by g4f (gpt-4o-mini, free)
- 🖼️ Image, News, Video search tabs
- 💬 AI code/chat assistant (floating panel)
- 📄 Full page extraction & summarization
- 🧾 OCR bridge API via `/api/ocr/once` (connected to SearchMyFiles space)
- 🌍 Multi-region support

## OCR Integration

This app includes an OCR proxy endpoint:
- `POST /api/ocr/once`
- `GET /api/ocr/status`

Defaults:
- `OCR_API_BASE=https://harikirankumar-searchmyfiles.hf.space`

Optional secret:
- `OCR_API_KEY` (set in Space Variables/Secrets if your OCR space requires auth)

## Running Locally

See the [installer instructions](https://huggingface.co/spaces/Harikirankumar/ml-ai-platform) or download `setup.bat` from the Files tab and run it on Windows.

## Tech Stack
- FastAPI + Uvicorn
- DuckDuckGo Search (ddgs)
- gpt4free (g4f)
- trafilatura (page extraction)
- Optional: llama-cpp-python + Qwen GGUF (local LLM)
