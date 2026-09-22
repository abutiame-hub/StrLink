# StrLink 2.0 — Verified Backup & AI Suite Manager

**StrLink** is a modern Windows desktop utility designed for developers and AI power-users. It ensures verified backups, client-side encryption, token cost governance, and seamless project context transfers between AI coding assistants.

---

## 🚀 Key Features

- 🔒 **AES-256-GCM Encryption:** Transactional, verified snapshots protected by PBKDF2-HMAC-SHA256 (600,000 rounds) and SHA-256 integrity verification.
- 🛡️ **Zero-Loss Safe Restore:** Non-destructive restore engine with automatic rollback on error.
- 🤖 **AI Suite Manager:** Seamlessly back up and manage configurations, skills, and templates for tools like Claude Code, OpenAI Codex, Gemini Antigravity, and Cursor.
- 📊 **Token Guard:** Built-in dashboard to monitor LLM token consumption, avoid rate limits, and track developer AI costs.
- 🔄 **Project Handoff:** Clean, secret-stripped context migration (`WORK_MAP.json`, `HANDOFF.md`) between AI assistants.

---

## 💻 Tech Stack
- **Backend:** Python 3.14
- **UI:** Modern Web UI powered by Microsoft Edge WebView2 (`pywebview`)
- **Cryptography:** `cryptography` (AES-GCM, PBKDF2)
- **Packaging:** PyInstaller & Inno Setup

---

## 📦 Download & Run
Download the latest `StrLink.exe` directly from this repository and run it on Windows.  
*Requires Microsoft Edge WebView2 Runtime.*
