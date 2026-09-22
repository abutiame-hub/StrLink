import os
import re
import sys
import json
import shutil
import tempfile
import subprocess
import glob
import time
import urllib.request
import urllib.parse
from pathlib import Path
import tomllib
import uuid
from backup_engine import BackupEngine, VERSION, atomic_json, settings_path
from project_handoff import ProjectHandoff, EXCLUDED_DIRS
from token_guard import TokenGuard


def default_backup_dir():
    """Pick a backup root that works without assuming a fixed D: drive.

    Priority: a Skills_Library next to the running exe (portable/USB mode)
    -> this machine's original D:\\AI_BACKUP if it actually exists -> a
    per-user folder that works on any Windows PC, including a brand new
    one with no D: drive at all.
    """
    exe_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    if os.path.exists(os.path.join(exe_dir, "Skills_Library")):
        portable_dir = exe_dir
    else:
        portable_dir = None
    try:
        saved = json.loads(settings_path().read_text(encoding="utf-8")).get("backup_dir")
        if saved and os.path.isdir(saved):
            return saved
    except (OSError, ValueError):
        pass
    if portable_dir:
        return portable_dir
    if os.path.isdir(r"D:\AI_BACKUP"):
        return r"D:\AI_BACKUP"
    user_profile = os.environ.get("USERPROFILE", str(Path.home()))
    return os.path.join(user_profile, "StrLink_Backup")


def get_log_dir():
    """Shared with main.py so both agree on where strlink.log lives."""
    return os.path.join(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(), "StrLink", "logs")


# Best-effort tripwire for the most obviously dangerous patterns in a
# downloaded skill's source - NOT a real security scanner. It only exists to
# catch the crudest cases (encoded PowerShell payloads, base64-then-exec)
# before a skill is silently indexed into the library; it will miss anything
# even slightly disguised, and can false-positive on legitimate automation.
SUSPICIOUS_PATTERNS = [
    (re.compile(rb'powershell[^\n]{0,60}-[eE][nN][cC]'), "encoded PowerShell command (-enc) - a common obfuscation trick"),
    (re.compile(rb'\biex\s*\(', re.IGNORECASE), "PowerShell Invoke-Expression (IEX) - runs decoded/downloaded code"),
    (re.compile(rb'base64[^\n]{0,80}\b(exec|eval)\b|\b(exec|eval)\b[^\n]{0,80}base64', re.IGNORECASE), "decodes base64 then executes it - a common payload-hiding pattern"),
    (re.compile(rb'\beval\s*\('), "calls eval() on data"),
    (re.compile(rb'os\.system\s*\('), "shells out via os.system()"),
]
SUSPICIOUS_SCAN_EXTS = {".py", ".js", ".ts", ".sh", ".ps1", ".bat", ".cmd"}


class Api:
    def __init__(self, backup_dir=None):
        self.backup_dir = os.path.abspath(backup_dir) if backup_dir else default_backup_dir()
        self.user_profile = os.environ.get("USERPROFILE", str(Path.home()))
        self.skills_library_dir = os.path.join(self.backup_dir, "Skills_Library")
        self.categories_json_path = os.path.join(self.backup_dir, "skills_categories.json")
        self.templates_dir = os.path.join(self.backup_dir, "Templates")
        self.personal_data_dir = os.path.join(self.backup_dir, "Personal_Data")
        # Underscore-prefixed: pywebview's inject_pywebview() walks every
        # PUBLIC attribute of this Api object via dir()/getattr() to build
        # the window.pywebview.api.* JS bridge, recursing into any
        # non-callable object it finds. A plain `self.window` would hand it
        # the live WinForms window, and its .native property chains into
        # .NET/WinForms internals (AccessibilityObject.Bounds.Empty...)
        # where each access returns a brand-new object, so pywebview's
        # id()-based cycle guard never triggers and it recurses until
        # Python's stack limit on every single startup. Names starting with
        # "_" are skipped by that walker, so this is the sanctioned way to
        # keep an attribute backend-only.
        self._window = None

        self.categories = self._load_categories()
        self._engine = BackupEngine(self)
        self._handoff = ProjectHandoff(self)
        self._token_guard = TokenGuard(self)

    def set_window(self, window):
        self._window = window

    def _load_categories(self):
        if os.path.exists(self.categories_json_path):
            try:
                with open(self.categories_json_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def get_system_info(self):
        return {
            "app_name": "StrLink",
            "version": VERSION,
            "computer_name": os.environ.get("COMPUTERNAME", "PC"),
            "user_profile": self.user_profile,
            "backup_dir": self.backup_dir,
            "skills_library_dir": self.skills_library_dir,
            "skills_library_count": len(os.listdir(self.skills_library_dir)) if os.path.exists(self.skills_library_dir) else 0
        }

    def get_apps_status(self):
        apps = {}
        
        # 1. Claude
        claude_base = os.path.join(self.user_profile, ".claude")
        claude_skills = os.path.join(claude_base, "skills")
        claude_sessions = os.path.join(claude_base, "sessions")
        claude_json = os.path.join(self.user_profile, ".claude.json")
        
        claude_installed_skills = []
        if os.path.exists(claude_skills):
            claude_installed_skills = [d for d in os.listdir(claude_skills) if os.path.isdir(os.path.join(claude_skills, d))]
        
        claude_mcps = self._read_mcp_servers(claude_json)

        claude_sess_count = 0
        if os.path.exists(claude_sessions):
            for root, _, files in os.walk(claude_sessions):
                claude_sess_count += len(files)

        apps["claude"] = {
            "name": "Claude Code",
            "id": "claude",
            "installed": os.path.exists(claude_base),
            "base_dir": claude_base,
            "skills_dir": claude_skills,
            "skills_count": len(claude_installed_skills),
            "installed_skills": claude_installed_skills,
            "mcp_servers": claude_mcps,
            "sessions_count": claude_sess_count,
            "has_settings": os.path.exists(os.path.join(claude_base, "settings.json")) or os.path.exists(os.path.join(claude_base, "CLAUDE.md")),
            "plugins": []
        }

        # 2. Gemini / Antigravity
        gemini_base = os.path.join(self.user_profile, ".gemini")
        gemini_skills = os.path.join(gemini_base, "config", "skills")
        gemini_brain = os.path.join(gemini_base, "antigravity", "brain")
        gemini_mcp = os.path.join(gemini_base, "config", "mcp_config.json")
        gemini_plugins_dir = os.path.join(gemini_base, "config", "plugins")

        gemini_installed_skills = []
        if os.path.exists(gemini_skills):
            gemini_installed_skills = [d for d in os.listdir(gemini_skills) if os.path.isdir(os.path.join(gemini_skills, d))]

        gemini_mcps = self._read_mcp_servers(gemini_mcp)

        gemini_chats_count = 0
        if os.path.exists(gemini_brain):
            gemini_chats_count = len([d for d in os.listdir(gemini_brain) if os.path.isdir(os.path.join(gemini_brain, d))])

        gemini_plugins = []
        if os.path.exists(gemini_plugins_dir):
            gemini_plugins = [d for d in os.listdir(gemini_plugins_dir) if os.path.isdir(os.path.join(gemini_plugins_dir, d))]

        apps["gemini"] = {
            "name": "Gemini / Antigravity",
            "id": "gemini",
            "installed": os.path.exists(gemini_base),
            "base_dir": gemini_base,
            "skills_dir": gemini_skills,
            "skills_count": len(gemini_installed_skills),
            "installed_skills": gemini_installed_skills,
            "mcp_servers": gemini_mcps,
            "sessions_count": gemini_chats_count,
            "has_settings": os.path.exists(os.path.join(gemini_base, "config", "config.json")),
            "plugins": gemini_plugins
        }

        # 3. ChatGPT / Codex
        codex_base = os.path.join(self.user_profile, ".codex")
        codex_skills = os.path.join(codex_base, "skills")
        codex_sessions = os.path.join(codex_base, "sessions")

        codex_installed_skills = []
        if os.path.exists(codex_skills):
            codex_installed_skills = [d for d in os.listdir(codex_skills) if os.path.isdir(os.path.join(codex_skills, d))]

        codex_sess_count = 0
        if os.path.exists(codex_sessions):
            for root, _, files in os.walk(codex_sessions):
                codex_sess_count += len(files)

        apps["codex"] = {
            "name": "ChatGPT / Codex",
            "id": "codex",
            "installed": os.path.exists(codex_base),
            "base_dir": codex_base,
            "skills_dir": codex_skills,
            "skills_count": len(codex_installed_skills),
            "installed_skills": codex_installed_skills,
            "mcp_servers": self._read_codex_mcp(os.path.join(codex_base, "config.toml")),
            "sessions_count": codex_sess_count,
            "has_settings": os.path.exists(os.path.join(codex_base, "config.toml")),
            "plugins": []
        }

        # 4. Cursor
        cursor_base = os.path.join(self.user_profile, ".cursor")
        cursor_skills = os.path.join(cursor_base, "skills")
        # Cursor follows the same {"mcpServers": {...}} convention as Claude,
        # stored at the user level in ~/.cursor/mcp.json.
        cursor_mcp_path = os.path.join(cursor_base, "mcp.json")
        cursor_mcps = self._read_mcp_servers(cursor_mcp_path)

        apps["cursor"] = {
            "name": "Cursor",
            "id": "cursor",
            "installed": os.path.exists(cursor_base),
            "base_dir": cursor_base,
            "skills_dir": cursor_skills,
            "skills_count": len(os.listdir(cursor_skills)) if os.path.exists(cursor_skills) else 0,
            "installed_skills": [d for d in os.listdir(cursor_skills) if os.path.isdir(os.path.join(cursor_skills, d))] if os.path.exists(cursor_skills) else [],
            "mcp_servers": cursor_mcps,
            "sessions_count": 0,
            "has_settings": os.path.exists(cursor_mcp_path),
            "plugins": []
        }

        # 5. Windsurf (best-effort: Codeium's documented config layout as of
        # this writing - worth double-checking against your installed
        # version if Windsurf ever shows as "Not Found" when you know it's
        # there, since IDE vendors do relocate these files over time).
        windsurf_base = os.path.join(self.user_profile, ".codeium", "windsurf")
        windsurf_mcp_path = os.path.join(windsurf_base, "mcp_config.json")
        apps["windsurf"] = {
            "name": "Windsurf",
            "id": "windsurf",
            "installed": os.path.exists(windsurf_base),
            "base_dir": windsurf_base,
            "skills_dir": "",
            "skills_count": 0,
            "installed_skills": [],
            "mcp_servers": self._read_mcp_servers(windsurf_mcp_path),
            "sessions_count": 0,
            "has_settings": os.path.exists(windsurf_mcp_path),
            "plugins": []
        }

        # 6. Claude Desktop (Anthropic's official chat app - distinct from
        # the Claude Code CLI covered above). Documented MCP config location
        # on Windows. No skills/session folder of its own to read.
        claude_desktop_base = os.path.join(os.environ.get("APPDATA") or os.path.join(self.user_profile, "AppData", "Roaming"), "Claude")
        claude_desktop_mcp_path = os.path.join(claude_desktop_base, "claude_desktop_config.json")
        apps["claude_desktop"] = {
            "name": "Claude Desktop",
            "id": "claude_desktop",
            "installed": os.path.exists(claude_desktop_base),
            "base_dir": claude_desktop_base,
            "skills_dir": "",
            "skills_count": 0,
            "installed_skills": [],
            "mcp_servers": self._read_mcp_servers(claude_desktop_mcp_path),
            "sessions_count": 0,
            "has_settings": os.path.exists(claude_desktop_mcp_path),
            "plugins": []
        }

        # 7. Continue (VS Code / JetBrains AI extension).
        continue_base = os.path.join(self.user_profile, ".continue")
        continue_mcp_path = os.path.join(continue_base, "config.json")
        apps["continue"] = {
            "name": "Continue",
            "id": "continue",
            "installed": os.path.exists(continue_base),
            "base_dir": continue_base,
            "skills_dir": "",
            "skills_count": 0,
            "installed_skills": [],
            "mcp_servers": self._read_mcp_servers(continue_mcp_path),
            "sessions_count": 0,
            "has_settings": os.path.exists(continue_mcp_path),
            "plugins": []
        }

        # 8. LM Studio (best-effort, same caveat as Windsurf above).
        lmstudio_base = os.path.join(self.user_profile, ".lmstudio")
        lmstudio_mcp_path = os.path.join(lmstudio_base, "mcp.json")
        apps["lmstudio"] = {
            "name": "LM Studio",
            "id": "lmstudio",
            "installed": os.path.exists(lmstudio_base),
            "base_dir": lmstudio_base,
            "skills_dir": "",
            "skills_count": 0,
            "installed_skills": [],
            "mcp_servers": self._read_mcp_servers(lmstudio_mcp_path),
            "sessions_count": 0,
            "has_settings": os.path.exists(lmstudio_mcp_path),
            "plugins": []
        }

        return apps

    def _read_codex_mcp(self, path):
        try:
            with open(path, "rb") as stream:
                return sorted(tomllib.load(stream).get("mcp_servers", {}))
        except (OSError, ValueError):
            return []

    def _read_mcp_servers(self, mcp_json_path):
        """Every tool above stores MCP config the same way: a JSON file with
        a top-level {"mcpServers": {...}} object. One shared, defensive
        reader instead of repeating the same try/except per app."""
        if not os.path.exists(mcp_json_path):
            return []
        try:
            with open(mcp_json_path, "r", encoding="utf-8") as f:
                return list(json.load(f).get("mcpServers", {}).keys())
        except Exception:
            return []

    # --------------------------------------------------------------------------
    # Wider "is anything else AI-related installed?" scan - explicit consent
    # required (see get_scan_consent/set_scan_consent). This never reads file
    # *contents* and never walks the drive recursively: it only reads the
    # Windows Uninstall registry (the same list Add/Remove Programs shows)
    # and lists the top-level entries of a handful of well-known install
    # folders. That is enough to recognize installed *programs* by name -
    # actually reading every file on the system drive would be slow, mostly
    # meaningless for this purpose, and needlessly invasive.
    # --------------------------------------------------------------------------
    def _read_preferences(self):
        try:
            return json.loads(settings_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _update_preferences(self, **kwargs):
        current = self._read_preferences()
        current.update(kwargs)
        atomic_json(settings_path(), current)

    def get_scan_consent(self):
        prefs = self._read_preferences()
        return {"decided": "ai_scan_consent" in prefs, "granted": prefs.get("ai_scan_consent") is True}

    def set_scan_consent(self, granted):
        self._update_preferences(ai_scan_consent=bool(granted))
        return {"success": True, "granted": bool(granted)}

    def _enumerate_uninstall_entries(self):
        import winreg
        roots = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        entries = []
        for hive, path in roots:
            try:
                with winreg.OpenKey(hive, path) as key:
                    count = winreg.QueryInfoKey(key)[0]
                    for i in range(count):
                        try:
                            subkey_name = winreg.EnumKey(key, i)
                            with winreg.OpenKey(key, subkey_name) as subkey:
                                try:
                                    display_name = winreg.QueryValueEx(subkey, "DisplayName")[0]
                                except FileNotFoundError:
                                    continue
                                try:
                                    install_location = winreg.QueryValueEx(subkey, "InstallLocation")[0]
                                except FileNotFoundError:
                                    install_location = ""
                                entries.append({"name": str(display_name), "install_location": str(install_location)})
                        except OSError:
                            continue
            except OSError:
                continue
        return entries

    def _scan_common_install_folders(self):
        program_files = os.environ.get("ProgramFiles")
        program_files_x86 = os.environ.get("ProgramFiles(x86)")
        local_appdata = os.environ.get("LOCALAPPDATA")
        roots = [p for p in (program_files, program_files_x86, local_appdata) if p]
        if local_appdata:
            roots.append(os.path.join(local_appdata, "Programs"))
        found = []
        for root in roots:
            try:
                for entry in os.listdir(root):
                    found.append({"name": entry, "install_location": os.path.join(root, entry)})
            except OSError:
                continue
        return found

    def scan_installed_ai_tools(self):
        if not self.get_scan_consent()["granted"]:
            return {"success": False, "error": "Scan permission was not granted yet. Use the allow/deny prompt first."}
        try:
            keywords = [
                "claude", "chatgpt", "openai", "cursor", "windsurf", "github copilot",
                "microsoft copilot", "ollama", "lm studio", "perplexity", "anythingllm",
                "gpt4all", "msty", "deepseek", "gemini", "continue.dev", "jetbrains ai",
            ]
            candidates = self._enumerate_uninstall_entries() + self._scan_common_install_folders()
            matches = {}
            for entry in candidates:
                name = (entry.get("name") or "").strip()
                if not name:
                    continue
                lower = name.lower()
                for keyword in keywords:
                    if keyword in lower:
                        key = lower
                        if key not in matches:
                            matches[key] = {"name": name, "install_location": entry.get("install_location", ""), "matched_keyword": keyword}
                        break
            results = sorted(matches.values(), key=lambda item: item["name"].lower())
            return {"success": True, "results": results, "scanned_drive": os.environ.get("SystemDrive", "C:")}
        except Exception as error:
            return {"success": False, "error": str(error)}

    def get_categories_manifest(self):
        if not self.categories:
            self.categories = self._load_categories()
        return self.categories

    # --------------------------------------------------------------------------
    # Smart Online Hub Search (Zero-typing Category/Type Filtering + Text Query)
    # --------------------------------------------------------------------------
    def search_online(self, item_type="skills", category="ui", query="", sort_by="stars", page=1):
        # item_type: 'skills', 'mcp', 'plugins'
        # category: 'ui', 'dev', 'security', 'marketing', 'business', 'ai', 'all'
        # sort_by: 'stars', 'updated', 'forks', or 'best' (omits sort -> GitHub's
        # own relevance ranking). 'name'/'size' are handled client-side since
        # the search API doesn't support them directly.
        results = []

        category_keywords = {
            "ui": "ui ux design figma",
            "dev": "coding developer python flutter web api",
            "security": "cybersecurity pentest security soc",
            "marketing": "marketing seo ecommerce shopify",
            "business": "business product management saas",
            "ai": "llm agent prompt rag intelligence",
            "all": ""
        }

        type_keywords = {
            "skills": "claude skill OR agent skills",
            "mcp": "mcp server OR model context protocol",
            "plugins": "plugin OR extensions"
        }

        search_query_parts = []
        t_kw = type_keywords.get(item_type, "skills")
        search_query_parts.append(f"({t_kw})")

        c_kw = category_keywords.get(category, "")
        if c_kw:
            search_query_parts.append(f"({c_kw})")

        if query and query.strip():
            search_query_parts.append(f"({query.strip()})")

        final_query = " ".join(search_query_parts)

        sort_param = ""
        if sort_by in ("stars", "updated", "forks"):
            sort_param = f"&sort={sort_by}&order=desc"
        # sort_by == "best" (or anything else, incl. "name"/"size" which are
        # sorted client-side from a relevance-ranked set) -> no &sort= at all.

        try:
            page = max(1, int(page or 1))
            per_page = 30
            url = f"https://api.github.com/search/repositories?q={urllib.parse.quote(final_query)}{sort_param}&per_page={per_page}&page={page}"
            req = urllib.request.Request(url, headers={"User-Agent": "StrLink-Hub/1.0", "Accept": "application/vnd.github.v3+json"})

            with urllib.request.urlopen(req, timeout=9) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                total_count = data.get("total_count", 0)
                for item in data.get("items", []):
                    desc = item.get("description") or "AI skill / tool package repository"
                    name_l = item.get("name", "").lower()
                    desc_l = desc.lower()
                    # This used to only ever detect "mcp" and fell back to
                    # "skill" for everything else - a search filtered to
                    # Plugins (item_type="plugins") still built its GitHub
                    # query around plugin/extension keywords, so it found
                    # real plugin repos, but every card came back labeled
                    # "skill" anyway because there was no plugin branch here
                    # at all. Badge text, icon and download type all read
                    # this field, so a name-only fix wouldn't have reached them.
                    if "mcp" in name_l or "model context protocol" in desc_l or "mcp" in desc_l:
                        pkg_type = "mcp"
                    elif "plugin" in name_l or "extension" in name_l or "plugin" in desc_l or "extension" in desc_l:
                        pkg_type = "plugin"
                    else:
                        pkg_type = "skill"
                    results.append({
                        "name": item.get("name"),
                        "full_name": item.get("full_name"),
                        "description": desc,
                        "stars": item.get("stargazers_count", 0),
                        "forks": item.get("forks_count", 0),
                        "size_kb": item.get("size", 0),
                        "updated_at": item.get("updated_at", ""),
                        "url": item.get("html_url"),
                        "clone_url": item.get("clone_url"),
                        "default_branch": item.get("default_branch") or "main",
                        "type": pkg_type,
                        "owner": item.get("owner", {}).get("login", ""),
                        "category": category
                    })
            # GitHub's Search API caps results at 1000 regardless of total_count.
            has_more = (page * per_page) < min(total_count, 1000)
            return {"success": True, "results": results, "query_used": final_query, "total_count": total_count, "page": page, "has_more": has_more}
        except Exception as e:
            return {"success": False, "error": f"Online discovery failed: {str(e)}", "results": []}

    def download_online_package(self, package_info, target_app=None):
        logs = []
        pkg_name = package_info.get("name", "custom_package")
        clone_url = package_info.get("clone_url")
        pkg_type = package_info.get("type", "skill")
        default_branch = package_info.get("default_branch") or "main"

        try:
            if target_app:
                raise ValueError("Direct online installation is disabled; review the download before restoring it")
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", pkg_name) or pkg_name in (".", ".."):
                raise ValueError("Invalid package name")
            full_name = package_info.get("full_name", "")
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", full_name):
                raise ValueError("Invalid GitHub repository")
            clone_url = f"https://github.com/{full_name}.git"
            logs.append(f"[*] Downloading package: {pkg_name} ({pkg_type}) from online repository...")

            # Resolve the destination (and avoid clobbering an existing,
            # unrelated folder with the same name) before creating anything,
            # so both download methods below land in the exact same place.
            dest_dir = os.path.join(self.backup_dir, "Quarantine", pkg_name + "-" + uuid.uuid4().hex[:8])
            os.makedirs(dest_dir, exist_ok=True)

            git_path = shutil.which("git")
            if git_path and clone_url:
                # A DNS hiccup or a dropped connection is common on a home
                # connection and has nothing to do with the repo itself -
                # retrying a couple of times clears most of these without
                # bothering the user. A permanent failure (repo renamed,
                # private, never existed) won't match any of these markers
                # and is raised immediately instead of wasting three tries.
                transient_markers = ("could not resolve host", "timed out", "timeout was reached",
                                     "connection reset", "connection refused", "recv failure",
                                     "temporary failure", "network is unreachable", "could not connect",
                                     "ssl_connect", "eof detected", "unable to access")
                clone_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
                last_reason = "unknown error"
                for attempt in range(1, 4):
                    logs.append(f"[*] Running git clone --depth 1 (attempt {attempt}/3)...")
                    clone = subprocess.run([git_path, "clone", "--depth", "1", "--", clone_url, dest_dir], capture_output=True, text=True, timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=clone_env)
                    if clone.returncode == 0:
                        last_reason = None
                        break
                    # git's own stderr is the only thing that actually
                    # explains a clone failure (renamed/private repo, a
                    # transient network blip, a GitHub rate limit...) - the
                    # exit code alone tells the user nothing actionable.
                    reason = (clone.stderr or clone.stdout or "").strip().splitlines()
                    last_reason = reason[-1] if reason else f"exit code {clone.returncode}"
                    is_transient = any(marker in last_reason.lower() for marker in transient_markers)
                    if attempt < 3 and is_transient:
                        logs.append(f"[!] Network hiccup, retrying: {last_reason}")
                        shutil.rmtree(dest_dir, ignore_errors=True)
                        os.makedirs(dest_dir, exist_ok=True)
                        time.sleep(1.5 * attempt)
                        continue
                    break
                if last_reason is not None:
                    raise RuntimeError("git clone failed: " + last_reason)
                logs.append(f"[✔] Successfully cloned repository into local Skills Library.")
            else:
                full_name = package_info.get("full_name")
                zip_url = f"https://api.github.com/repos/{full_name}/zipball/{default_branch}"
                logs.append(f"[*] Downloading ZIP package directly from GitHub...")
                temp_zip = dest_dir + ".zip"
                req = urllib.request.Request(zip_url, headers={"User-Agent": "StrLink-Hub/1.0"})
                with urllib.request.urlopen(req, timeout=25) as resp, open(temp_zip, "wb") as out_f:
                    out_f.write(resp.read())
                import zipfile
                from backup_engine import inside
                with zipfile.ZipFile(temp_zip) as archive:
                    if sum(entry.file_size for entry in archive.infolist()) > 512 * 1024 * 1024:
                        raise ValueError("Package is too large")
                    for entry in archive.infolist():
                        inside(dest_dir, entry.filename)
                        if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                            raise ValueError("Linked files are not allowed")
                    archive.extractall(dest_dir)
                if os.path.exists(temp_zip): os.remove(temp_zip)

                # GitHub's zipball always wraps the repo in a single
                # "{owner}-{repo}-{sha}" folder. Without flattening it, every
                # file ends up one level deeper than git clone would put it,
                # so the skill's own files (SKILL.md, scripts/...) end up
                # nested where nothing that reads Skills_Library expects
                # them - flatten it so both download paths produce the same
                # layout.
                extracted = os.listdir(dest_dir)
                if len(extracted) == 1:
                    wrapper = os.path.join(dest_dir, extracted[0])
                    if os.path.isdir(wrapper):
                        for entry in os.listdir(wrapper):
                            shutil.move(os.path.join(wrapper, entry), os.path.join(dest_dir, entry))
                        os.rmdir(wrapper)

                logs.append(f"[✔] Successfully extracted and indexed package.")

            suspicious_findings = self._scan_for_suspicious_patterns(dest_dir)
            if suspicious_findings:
                logs.append(f"[⚠ REVIEW] {len(suspicious_findings)} pattern(s) worth a manual look before you trust this package:")
                for f in suspicious_findings:
                    logs.append(f"    - {f}")
                return {"success": False, "status": "quarantined", "error": "Package blocked for manual review: " + dest_dir + "\n" + "\n".join(suspicious_findings), "logs": logs}

            for current, directories, names in os.walk(dest_dir):
                for name in directories + names:
                    candidate = Path(current) / name
                    if candidate.is_symlink() or candidate.is_junction():
                        raise ValueError("Package contains linked files and remains quarantined")

            skill_dirs = self._find_skill_dirs(dest_dir)
            if not skill_dirs:
                return {"success": False, "status": "quarantined", "error": "No SKILL.md found in the repository or its subfolders. Kept for manual review: " + dest_dir, "logs": logs}

            if len(skill_dirs) > 1:
                logs.append(f"[i] This repository is a pack of {len(skill_dirs)} skills; each will be installed as its own library entry.")

            os.makedirs(self.skills_library_dir, exist_ok=True)
            installed, skipped = [], []
            for skill_dir in skill_dirs:
                install_name = pkg_name if skill_dir == Path(dest_dir) else skill_dir.name
                if not re.fullmatch(r"[A-Za-z0-9_.-]+", install_name) or install_name in (".", ".."):
                    skipped.append((install_name, "invalid name"))
                    continue
                library_target = os.path.join(self.skills_library_dir, install_name)
                if os.path.exists(library_target):
                    skipped.append((install_name, "already in library"))
                    continue
                try:
                    shutil.copytree(skill_dir, library_target)
                except OSError as error:
                    # A multi-skill pack (8+ skills from one repo isn't rare)
                    # used to lose every remaining skill in the batch if just
                    # one had a Windows-invalid filename or similar copy
                    # failure - and silently leave whatever *had* copied
                    # already on disk while still reporting the whole
                    # download as failed. Skip just this one skill, clean up
                    # its partial copy, and keep going with the rest.
                    shutil.rmtree(library_target, ignore_errors=True)
                    skipped.append((install_name, f"copy failed: {error}"))
                    continue
                installed.append(install_name)
                logs.append(f"[✔] Installed skill: {install_name}")
            for name, reason in skipped:
                logs.append(f"[i] Skipped '{name}': {reason}")

            if not installed:
                return {"success": False, "status": "quarantined", "error": "Every skill in this package was already in your library or had an invalid name. Review: " + dest_dir, "logs": logs}

            self._rebuild_categories_json()
            return {"success": True, "logs": logs, "suspicious_findings": suspicious_findings, "installed": installed, "skipped": [n for n, _ in skipped]}
        except Exception as e:
            logs.append(f"[ERROR] Failed downloading package: {str(e)}")
            return {"success": False, "error": str(e), "logs": logs}

    # --------------------------------------------------------------------------
    # Backup, Restore, Transfer
    # --------------------------------------------------------------------------
    def perform_backup(self, options):
        return self._engine.backup(options)

    def perform_restore(self, options):
        return self._engine.restore(options)

    def get_backup_inventory(self):
        return self._engine.catalog()

    def open_skill_folder(self, skill_name):
        try:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", skill_name or "") or skill_name in (".", ".."):
                raise ValueError("Invalid skill name")
            target = Path(self.skills_library_dir) / skill_name
            if not target.is_dir():
                raise ValueError("Skill folder not found: " + str(target))
            os.startfile(target)
            return {"success": True}
        except Exception as error:
            return {"success": False, "error": str(error)}

    def browse_project(self, source_app=None):
        try:
            import webview
            directory = (self._handoff.most_recent_project_dir(source_app) or "") if source_app else ""
            result = self._window.create_file_dialog(webview.FOLDER_DIALOG, directory=directory)
            return {"success": True, "path": result[0]} if result else {"success": False, "cancelled": True}
        except Exception as error:
            return {"success": False, "error": str(error)}

    def preview_project_handoff(self, options):
        return self._handoff.preview(options)

    def export_project_handoff(self, options):
        return self._handoff.export(options)

    # --------------------------------------------------------------------------
    # Token Guard (see token_guard.py for what this does and does not do)
    # --------------------------------------------------------------------------
    def detect_token_guard_project(self, project_path):
        try:
            return {"success": True, "project": self._token_guard.detect(project_path)}
        except Exception as error:
            return {"success": False, "error": str(error)}

    def get_token_guard_status(self, project_path):
        try:
            return {"success": True, **self._token_guard.status(project_path)}
        except Exception as error:
            return {"success": False, "error": str(error)}

    def scan_token_guard_project(self, project_path):
        try:
            return {"success": True, **self._token_guard.scan(project_path)}
        except Exception as error:
            return {"success": False, "error": str(error)}

    def enable_token_guard(self, options):
        try:
            return {"success": True, **self._token_guard.enable(options.get("project_path"), options)}
        except Exception as error:
            return {"success": False, "error": str(error)}

    def disable_token_guard(self, project_path):
        try:
            return {"success": True, **self._token_guard.disable(project_path)}
        except Exception as error:
            return {"success": False, "error": str(error)}

    def open_token_guard_folder(self, project_path):
        try:
            self._token_guard.open_folder(project_path)
            return {"success": True}
        except Exception as error:
            return {"success": False, "error": str(error)}

    def list_snapshots(self):
        return self._engine.list_snapshots()

    def get_snapshot_items(self, snapshot_id, password=""):
        try:
            return {"success": True, "items": self._engine.snapshot_items(snapshot_id, password)}
        except Exception as error:
            return {"success": False, "error": str(error)}

    def preview_restore(self, options):
        return self._engine.preview(options)

    def cancel_operation(self):
        self._engine.cancel.set()
        return {"success": True}

    def list_recovery_points(self):
        return self._engine.recovery_list()

    def rollback_restore(self, identifier, password=""):
        return self._engine.rollback(identifier, password)

    def perform_transfer(self, options):
        return {"success": False, "error": "Direct cross-app overwrite is disabled for safety. Use dated backups and previewed restore; MCP formats are not interchangeable.", "logs": []}

    def open_backup_folder(self, folder_path=None):
        try:
            target = folder_path or self.backup_dir
            if os.path.exists(target):
                os.startfile(target)
                return {"success": True}
            return {"success": False, "error": "Folder not found"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def set_backup_dir(self, new_dir):
        if not new_dir or not os.path.isdir(new_dir):
            return {"success": False, "error": "Selected directory does not exist"}
        if not self._engine.lock.acquire(blocking=False):
            return {"success": False, "error": "Wait for the current operation to finish"}
        try:
            resolved = Path(new_dir).resolve()
            for name, root in self._engine.roots().items():
                if name != "claude_mcp" and resolved.is_relative_to(root.resolve()):
                    raise ValueError("Backup storage must be outside application data folders")
            with tempfile.TemporaryFile(dir=resolved):
                pass
            self._update_preferences(backup_dir=str(resolved))
            self.backup_dir = str(resolved)
            self.skills_library_dir = os.path.join(self.backup_dir, "Skills_Library")
            self.categories_json_path = os.path.join(self.backup_dir, "skills_categories.json")
            self.templates_dir = os.path.join(self.backup_dir, "Templates")
            self.personal_data_dir = os.path.join(self.backup_dir, "Personal_Data")
            self.categories = self._load_categories()
            count = len(os.listdir(self.skills_library_dir)) if os.path.isdir(self.skills_library_dir) else 0
            return {"success": True, "backup_dir": self.backup_dir, "skills_library_dir": self.skills_library_dir, "skills_count": count, "categories": self.categories}
        except Exception as error:
            return {"success": False, "error": str(error)}
        finally:
            self._engine.lock.release()

    def browse_backup_dir(self):
        selected = None
        if hasattr(self, '_window') and self._window:
            try:
                import webview
                res = self._window.create_file_dialog(webview.FOLDER_DIALOG)
                if res and len(res) > 0:
                    selected = res[0]
            except Exception:
                pass

        if not selected:
            try:
                ps_code = (
                    "Add-Type -AssemblyName System.Windows.Forms; "
                    "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
                    "$d.Description = 'Select StrLink Backup Directory'; "
                    "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $d.SelectedPath }"
                )
                p = subprocess.run(["powershell", "-NoProfile", "-Command", ps_code], capture_output=True, text=True, timeout=60)
                out = p.stdout.strip()
                if out and os.path.exists(out):
                    selected = out
            except Exception:
                pass

        if selected:
            return self.set_backup_dir(selected)
        return {"success": False, "cancelled": True}

    def export_portable_package(self, dest_dir=None):
        logs = []
        try:
            if not dest_dir and hasattr(self, '_window') and self._window:
                try:
                    import webview
                    res = self._window.create_file_dialog(webview.FOLDER_DIALOG)
                    if res and len(res) > 0:
                        dest_dir = res[0]
                except Exception:
                    pass

            if not dest_dir:
                try:
                    ps_code = (
                        "Add-Type -AssemblyName System.Windows.Forms; "
                        "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
                        "$d.Description = 'Select Destination Folder for Portable StrLink Package'; "
                        "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $d.SelectedPath }"
                    )
                    p = subprocess.run(["powershell", "-NoProfile", "-Command", ps_code], capture_output=True, text=True, timeout=60)
                    out = p.stdout.strip()
                    if out and os.path.exists(out):
                        dest_dir = out
                except Exception:
                    pass

            if not dest_dir or not os.path.exists(dest_dir):
                return {"success": False, "cancelled": True}

            portable_folder = os.path.join(dest_dir, "StrLink_Portable_" + uuid.uuid4().hex[:8])
            if Path(portable_folder).resolve().is_relative_to(Path(self.skills_library_dir).resolve()):
                raise ValueError("Portable destination cannot be inside the skills library")
            os.makedirs(portable_folder, exist_ok=False)
            logs.append(f"Created portable target folder: {portable_folder}")

            # 1. Copy StrLink.exe
            exe_source = None
            if getattr(sys, 'frozen', False):
                exe_source = sys.executable
            else:
                for candidate in [os.path.join(self.backup_dir, "StrLink.exe"), r"D:\AI_BACKUP\StrLink.exe"]:
                    if os.path.exists(candidate):
                        exe_source = candidate
                        break

            if exe_source and os.path.exists(exe_source):
                dest_exe = os.path.join(portable_folder, "StrLink.exe")
                shutil.copy2(exe_source, dest_exe)
                logs.append("Copied StrLink.exe standalone executable")
            else:
                logs.append("[WARNING] StrLink.exe not found in source directory, continuing with data files...")

            # 2. Copy Skills_Library
            if os.path.exists(self.skills_library_dir):
                dest_skills = os.path.join(portable_folder, "Skills_Library")
                if os.path.exists(dest_skills):
                    shutil.rmtree(dest_skills)
                shutil.copytree(self.skills_library_dir, dest_skills)
                skill_count = len(os.listdir(dest_skills))
                logs.append(f"Copied Skills Library ({skill_count} skills)")

            # 3. Copy Templates
            if os.path.exists(self.templates_dir):
                dest_tpl = os.path.join(portable_folder, "Templates")
                if os.path.exists(dest_tpl):
                    shutil.rmtree(dest_tpl)
                shutil.copytree(self.templates_dir, dest_tpl)
                logs.append("Copied clean app configuration templates")

            # 4. Copy skills_categories.json
            if os.path.exists(self.categories_json_path):
                shutil.copy2(self.categories_json_path, os.path.join(portable_folder, "skills_categories.json"))
                logs.append("Copied skills categories database")

            # 5. Create README.txt
            readme_file = os.path.join(portable_folder, "README.txt")
            with open(readme_file, "w", encoding="utf-8") as f:
                f.write("StrLink - Standalone Portable AI Suite\n"
                        "======================================\n\n"
                        "How to run on any computer:\n"
                        "1. Double-click 'StrLink.exe' to run directly.\n"
                        "2. All your skills, templates, and sync configurations are self-contained here.\n"
                        "3. Works directly from USB flash drives, external drives, or any PC partition.\n"
                        "4. Python is bundled; Microsoft Edge WebView2 Runtime is required.\n"
                        "5. Snapshots, recovery data, credentials and chats are NOT included.\n"
                        "6. Review all skills and templates for private data before sharing.\n")
            logs.append("Generated portable README.txt")

            # Open folder in Explorer
            try:
                os.startfile(portable_folder)
            except Exception:
                pass

            return {
                "success": True,
                "portable_path": portable_folder,
                "logs": logs
            }
        except Exception as e:
            logs.append(f"[ERROR] Export failed: {str(e)}")
            return {"success": False, "error": str(e), "logs": logs}

    def _emit_progress(self, current, total, message):
        """Push a live progress update to the page. js_api calls already run
        on a background thread (pywebview does this itself, specifically so
        they're free to call back into evaluate_js), so this is safe to call
        from inside perform_backup/restore/transfer's copy loops without
        blocking the UI. json.dumps() encodes the string safely for JS
        regardless of quotes/special characters in a skill or app name.
        """
        if not self._window:
            return
        try:
            self._window.evaluate_js(
                f"window.onStrLinkProgress && window.onStrLinkProgress({json.dumps(current)}, {json.dumps(total)}, {json.dumps(message)})"
            )
        except Exception:
            pass

    def _scan_for_suspicious_patterns(self, folder):
        """Best-effort scan of a freshly-downloaded skill's own text files.
        See SUSPICIOUS_PATTERNS for exactly what this can and can't catch."""
        hits = []
        for root, _, files in os.walk(folder):
            for fn in files:
                if os.path.splitext(fn)[1].lower() not in SUSPICIOUS_SCAN_EXTS:
                    continue
                fp = os.path.join(root, fn)
                try:
                    with open(fp, "rb") as f:
                        content = f.read(200_000)
                except Exception:
                    continue
                rel = os.path.relpath(fp, folder)
                for pattern, reason in SUSPICIOUS_PATTERNS:
                    if pattern.search(content):
                        hits.append(f"{rel}: {reason}")
        return hits

    def _find_skill_dirs(self, root, max_depth=3):
        """Search up to max_depth levels for folders that directly contain a
        SKILL.md (case-insensitive - some repos ship "skill.md"). A folder
        that has one is treated as a complete skill and not descended into
        further, so a skill's own subfolders (scripts/, references/...)
        never get mistaken for more skills. This is what lets a multi-skill
        "pack" repository - very common on GitHub, e.g. a design-skills repo
        with skills/figma-sync/SKILL.md, skills/ui-flow/SKILL.md, ... -
        install as one library entry per skill instead of being rejected
        outright just because there is no single SKILL.md at the repository
        root. .git/node_modules/etc. are skipped, same exclusion list the
        project-handoff feature already uses.
        """
        found = []
        root = Path(root)

        def walk(current, depth):
            try:
                entries = list(current.iterdir())
            except OSError:
                return
            for entry in entries:
                if entry.is_file() and entry.name.lower() == "skill.md":
                    found.append(current)
                    return
            if depth >= max_depth:
                return
            for entry in entries:
                if entry.is_dir() and not entry.is_symlink() and entry.name.lower() not in EXCLUDED_DIRS and not entry.name.startswith('.'):
                    walk(entry, depth + 1)

        walk(root, 0)
        return found

    def open_diagnostics_log(self):
        try:
            log_dir = get_log_dir()
            if not os.path.exists(log_dir):
                return {"success": False, "error": "Nothing has been logged yet this session."}
            os.startfile(log_dir)
            return {"success": True, "path": log_dir}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def open_browser_export_help(self, service):
        """Browser-based AI chats (chatgpt.com, claude.ai, gemini.google.com)
        live on the provider's servers, not this PC, so StrLink can't back
        them up directly - the only real path is each service's own manual
        "export my data" feature. This opens the service's site in the
        user's actual default browser (so their real logged-in session
        applies) and prepares/opens a dedicated folder for the exported zip,
        rather than guessing a specific settings deep-link that may have
        moved.
        """
        sites = {
            "chatgpt": "https://chatgpt.com",
            "claude": "https://claude.ai",
            "gemini": "https://gemini.google.com",
        }
        url = sites.get(service)
        if not url:
            return {"success": False, "error": "Unknown service"}
        try:
            import webbrowser
            webbrowser.open(url)
            dest = os.path.join(self.personal_data_dir, "Browser_Exports", service)
            os.makedirs(dest, exist_ok=True)
            try:
                os.startfile(dest)
            except Exception:
                pass
            return {"success": True, "folder": dest}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _rebuild_categories_json(self):
        if not os.path.exists(self.skills_library_dir): return
        skills = [d for d in os.listdir(self.skills_library_dir) if os.path.isdir(os.path.join(self.skills_library_dir, d))]

        categories = {
            "Graphic_Design": { "title": "Graphic, UI/UX, 3D & Media Design", "skills": [] },
            "Cybersecurity": { "title": "Cybersecurity, Pentesting & SOC", "skills": [] },
            "Marketing_Ecommerce": { "title": "Marketing, E-Commerce & SEO", "skills": [] },
            "Business_Management": { "title": "Business, C-Level, Operations & Management", "skills": [] },
            "AI_Prompt": { "title": "AI, Prompt Engineering & Subagents", "skills": [] },
            "Coding_Dev": { "title": "Coding, Web, Mobile & Software Development", "skills": [] }
        }

        for s in skills:
            nl = s.lower()
            if re.search(r"muapi|design|figma|logo|3d|banner|canvas|animation|illustration|color|typography|visual|theme|dark-mode|wireframe|photo|video|slides|art|icon", nl):
                categories["Graphic_Design"]["skills"].append(s)
            elif re.search(r"analyzing|detecting|exploiting|hunting|implementing.*(?:security|auth|encrypt|firewall|trust|defense|audit|canary|edr|siem|yara)|performing.*(?:forensic|malware|penetration|exploit|audit|attack|vuln|security|threat|hunt|red-team|crack)|auditing|attacking|reversing|security|malware|forensic|pentest|soc|splunk|c2|ransomware|yara|zeek|burpsuite|nmap|metasploit|bypass|privesc|hardening|threat|breach|incident|ids|zero-trust", nl):
                categories["Cybersecurity"]["skills"].append(s)
            elif re.search(r"shopify|etsy|tiktok|amazon|ebay|walmart|ecommerce|dropshipping|seo|ads|marketing|campaign|ppc|review-check|sales|cro|conversion|checkout|traffic|email-sequence|product-page", nl):
                categories["Marketing_Ecommerce"]["skills"].append(s)
            elif re.search(r"ceo|cto|cfo|cmo|cpo|cro|ciso|c-level|caio|cco|cdo|chro|coo|board|saas|pricing|product-|scrum|roadmap|agile|compliance|gdpr|iso|soc2|fda|risk|procurement|partner|legal|contract|commercial|finance|founder", nl):
                categories["Business_Management"]["skills"].append(s)
            elif re.search(r"prompt|llm|agent|subagent|gemini|claude|codex|gpt|rag|eval|hub-|autoresearch|memory|reasoning|graphify|intelligence|ai-", nl):
                categories["AI_Prompt"]["skills"].append(s)
            else:
                categories["Coding_Dev"]["skills"].append(s)

        with open(self.categories_json_path, "w", encoding="utf-8") as f:
            json.dump(categories, f, indent=2, ensure_ascii=False)
        self.categories = categories
