import fnmatch
import json
import os
from pathlib import Path

from backup_engine import atomic_json, tree_size
from project_handoff import EXCLUDED_DIRS


# What this module actually does (and does not do), stated plainly because
# the feature is easy to oversell: StrLink is a separate process from
# Claude Code/Codex/Gemini CLI/Cursor and has no hook into what those tools
# send their models. It cannot filter or rank context in flight. What it CAN
# honestly do is (a) write an advisory instructions block into the project's
# own CLAUDE.md/AGENTS.md/GEMINI.md - which those tools already read as
# ordinary prompt content - and (b) keep locally-generated summary/state/
# ignore files next to it for the tool (or the user) to reuse instead of
# re-exploring the project from scratch. Everything below is mechanical,
# local, file-based detection; nothing here is sent to or inferred by an LLM.

MARKER_START = "<!-- STRLINK TOKEN GUARD START -->"
MARKER_END = "<!-- STRLINK TOKEN GUARD END -->"

ADVISORY_BLOCK = f"""{MARKER_START}
# StrLink Token Guard

Use the minimum context necessary to complete the task correctly.

Read when useful:
- .strlink/token-policy.md
- .strlink/PROJECT_SUMMARY.md
- .strlink/SESSION_STATE.md

Search locally before scanning the whole repository.
Open only files relevant to the current task.
Prefer diffs over reopening unchanged files.
Avoid re-reading files already summarized in this session unless they changed.
Update .strlink/SESSION_STATE.md after substantial work.
{MARKER_END}"""

TOKEN_POLICY_TEXT = """# StrLink Token Efficiency Policy

Use the minimum context necessary to complete the task correctly. Correctness
comes first - use more context whenever the task genuinely needs it.

1. Search before reading broadly.
2. Don't scan the entire repository unless the task requires it.
3. Read only files relevant to the active task.
4. Read targeted sections of large files instead of the whole file.
5. Reuse PROJECT_SUMMARY.md and SESSION_STATE.md instead of re-deriving them.
6. Prefer a diff over re-reading an unchanged file.
7. Don't repeat unchanged code back in full.
8. Keep long log/terminal output to the relevant errors and context.
9. Don't repeat the user's request or previous explanations back to them.
10. Ignore generated and dependency directories unless the task needs them.
11. Update SESSION_STATE.md after substantial work so the next session can
    pick up without re-exploring the project.
"""

SESSION_STATE_TEMPLATE = """# Current State

Goal:
(what you're working on)

Relevant files:
-

Completed:
-

Remaining:
-

Important decisions:
-

Update this file after substantial work so your AI tool can pick up context
quickly instead of re-exploring the project.
"""

DEFAULT_IGNORE = [
    ".git/", "node_modules/", "dist/", "build/", "coverage/", ".cache/", ".next/",
    "vendor/", "tmp/", "temp/", "__pycache__/", ".venv/", "venv/",
    "*.log", "*.map", "*.min.js", "*.min.css",
    "*.zip", "*.7z", "*.rar", "*.exe", "*.dll",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.mp4", "*.mov",
]

ADAPTER_FILES = {"claude": "CLAUDE.md", "codex": "AGENTS.md", "gemini": "GEMINI.md"}

_NPM_FRAMEWORKS = (
    ("next", "Next.js"), ("nuxt", "Nuxt"), ("react", "React"), ("vue", "Vue"),
    ("svelte", "Svelte"), ("@angular/core", "Angular"), ("express", "Express"),
    ("electron", "Electron"), ("@nestjs/core", "NestJS"),
)
_PY_FRAMEWORKS = (("django", "Django"), ("flask", "Flask"), ("fastapi", "FastAPI"))


class TokenGuard:
    def __init__(self, api):
        self.api = api

    def _project(self, project_path):
        # Reuses ProjectHandoff's own path checks (rejects drive roots, the
        # home directory, the backup destination, and any AI-tool data
        # root) instead of re-implementing that safety logic a second time.
        return self.api._handoff._project(project_path)

    def detect(self, project_path):
        root = self._project(project_path)
        languages, frameworks = [], []
        package_manager = None
        entry_points = []

        package_json = root / "package.json"
        if package_json.exists():
            languages.append("TypeScript" if (root / "tsconfig.json").exists() else "JavaScript")
            try:
                pkg = json.loads(package_json.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pkg = {}
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            frameworks += [label for key, label in _NPM_FRAMEWORKS if key in deps]
            if isinstance(pkg.get("main"), str):
                entry_points.append(pkg["main"])
            for key in ("dev", "start", "build"):
                if key in pkg.get("scripts", {}):
                    entry_points.append(f"npm run {key}")
            if (root / "pnpm-lock.yaml").exists(): package_manager = "pnpm"
            elif (root / "yarn.lock").exists(): package_manager = "yarn"
            elif (root / "bun.lockb").exists(): package_manager = "bun"
            elif (root / "package-lock.json").exists(): package_manager = "npm"

        pyproject = root / "pyproject.toml"
        requirements = root / "requirements.txt"
        if pyproject.exists() or requirements.exists():
            languages.append("Python")
            text = ""
            if pyproject.exists():
                text += pyproject.read_text(encoding="utf-8", errors="ignore").lower()
                package_manager = package_manager or ("poetry" if "[tool.poetry]" in text else "pip")
            if requirements.exists():
                text += requirements.read_text(encoding="utf-8", errors="ignore").lower()
                package_manager = package_manager or "pip"
            frameworks += [label for key, label in _PY_FRAMEWORKS if key in text]
            for candidate in ("main.py", "app.py", "manage.py"):
                if (root / candidate).exists():
                    entry_points.append(candidate)

        if (root / "Cargo.toml").exists():
            languages.append("Rust"); package_manager = package_manager or "cargo"
        if (root / "go.mod").exists():
            languages.append("Go"); package_manager = package_manager or "go modules"
        if (root / "pom.xml").exists():
            languages.append("Java"); package_manager = package_manager or "maven"
        if (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
            languages.append("Java/Kotlin"); package_manager = package_manager or "gradle"
        if (root / "Gemfile").exists():
            languages.append("Ruby"); package_manager = package_manager or "bundler"
        if (root / "composer.json").exists():
            languages.append("PHP"); package_manager = package_manager or "composer"
        if (root / "pubspec.yaml").exists():
            languages.append("Dart/Flutter"); package_manager = package_manager or "pub"
        if any(root.glob("*.csproj")) or any(root.glob("*.sln")):
            languages.append("C#/.NET"); package_manager = package_manager or "nuget"

        source_dirs = [d for d in ("src", "app", "lib", "cmd") if (root / d).is_dir()]
        test_dirs = [d for d in ("test", "tests", "__tests__", "spec") if (root / d).is_dir()]

        return {
            "name": root.name,
            "root": str(root),
            "languages": languages or ["Unknown"],
            "frameworks": sorted(set(frameworks)),
            "packageManager": package_manager,
            "git": (root / ".git").exists(),
            "entryPoints": entry_points,
            "sourceDirs": source_dirs,
            "testDirs": test_dirs,
        }

    def _load_ignore(self, root):
        path = root / ".strlink" / "ignore.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass
        return list(DEFAULT_IGNORE)

    def scan(self, project_path):
        root = self._project(project_path)
        ignore = self._load_ignore(root)
        dir_names = {p[:-1].lower() for p in ignore if p.endswith("/")}
        file_patterns = [p for p in ignore if not p.endswith("/")]

        indexed, ignored_dirs = [], []
        indexed_bytes = ignored_bytes = 0

        for current, directories, names in os.walk(root, followlinks=False):
            rel_dir = Path(current).relative_to(root)
            for name in list(directories):
                path = Path(current) / name
                if path.is_symlink() or path.is_junction():
                    directories.remove(name)
                    continue
                if name in EXCLUDED_DIRS or name.lower() in dir_names:
                    directories.remove(name)
                    size = tree_size(path)
                    ignored_bytes += size
                    ignored_dirs.append({"path": (rel_dir / name).as_posix() + "/", "size": size})
            for name in names:
                path = Path(current) / name
                if path.is_symlink() or path.is_junction():
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if any(fnmatch.fnmatch(name.lower(), pattern.lower()) for pattern in file_patterns):
                    ignored_bytes += size
                    continue
                indexed_bytes += size
                indexed.append({"path": (rel_dir / name).as_posix(), "size": size, "ext": path.suffix.lower()})

        top_files = sorted(indexed, key=lambda f: -f["size"])[:15]
        file_map = {
            "generated_by": "StrLink Token Guard - local scan, no LLM involved",
            "indexed_count": len(indexed),
            "indexed_bytes": indexed_bytes,
            "ignored_bytes": ignored_bytes,
            "ignored_dirs": sorted(ignored_dirs, key=lambda d: -d["size"])[:30],
            "files": indexed,
        }
        strlink_dir = root / ".strlink"
        if strlink_dir.is_dir():
            atomic_json(strlink_dir / "FILE_MAP.json", file_map)

        return {
            "indexed_count": len(indexed),
            "indexed_bytes": indexed_bytes,
            "ignored_bytes": ignored_bytes,
            "ignored_dirs": file_map["ignored_dirs"],
            "top_files": top_files,
        }

    def _render_summary(self, info):
        entries = "\n".join(f"- {e}" for e in info["entryPoints"]) or "- (none detected)"
        return f"""# Project Summary

Auto-generated by StrLink Token Guard from local file detection only - no
LLM was involved. The facts below are mechanical; fill in the rest yourself
or let your AI tool keep it updated as you work.

## Detected facts
- Languages: {", ".join(info["languages"])}
- Frameworks: {", ".join(info["frameworks"]) or "(none detected)"}
- Package manager: {info["packageManager"] or "(none detected)"}
- Git repository: {"yes" if info["git"] else "no"}
- Source directories: {", ".join(info["sourceDirs"]) or "(none detected)"}
- Test directories: {", ".join(info["testDirs"]) or "(none detected)"}
- Entry points:
{entries}

## What this project does
(fill in)

## Key architecture notes
(fill in)

## Build / test commands
(fill in)

## Important decisions
(fill in)
"""

    def status(self, project_path):
        root = self._project(project_path)
        settings_path = root / ".strlink" / "settings.json"
        if not settings_path.exists():
            return {"exists": False, "enabled": False, "tools": []}
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            settings = {}
        return {"exists": True, "enabled": bool(settings.get("enabled")), "tools": settings.get("tools", [])}

    def enable(self, project_path, options):
        root = self._project(project_path)
        strlink_dir = root / ".strlink"
        strlink_dir.mkdir(exist_ok=True)
        (strlink_dir / "cache").mkdir(exist_ok=True)

        info = self.detect(project_path)
        atomic_json(strlink_dir / "project.json", info)

        summary_path = strlink_dir / "PROJECT_SUMMARY.md"
        if not summary_path.exists():
            summary_path.write_text(self._render_summary(info), encoding="utf-8")

        state_path = strlink_dir / "SESSION_STATE.md"
        if not state_path.exists():
            state_path.write_text(SESSION_STATE_TEMPLATE, encoding="utf-8")

        (strlink_dir / "token-policy.md").write_text(TOKEN_POLICY_TEXT, encoding="utf-8")

        ignore_path = strlink_dir / "ignore.json"
        if not ignore_path.exists():
            atomic_json(ignore_path, DEFAULT_IGNORE)

        tools = [tool for tool in options.get("tools", []) if tool in ADAPTER_FILES]
        written = []
        for tool in tools:
            target = root / ADAPTER_FILES[tool]
            existing = target.read_text(encoding="utf-8") if target.exists() else ""
            if MARKER_START in existing and MARKER_END in existing:
                start = existing.index(MARKER_START)
                end = existing.index(MARKER_END) + len(MARKER_END)
                updated = existing[:start] + ADVISORY_BLOCK + existing[end:]
            elif existing:
                updated = existing.rstrip("\n") + "\n\n" + ADVISORY_BLOCK + "\n"
            else:
                updated = ADVISORY_BLOCK + "\n"
            target.write_text(updated, encoding="utf-8")
            written.append(ADAPTER_FILES[tool])

        atomic_json(strlink_dir / "settings.json", {"enabled": True, "tools": tools})
        scan = self.scan(project_path)
        return {"strlink_dir": str(strlink_dir), "written_files": written, "project": info, "scan": scan}

    def disable(self, project_path):
        root = self._project(project_path)
        removed = []
        for name in ADAPTER_FILES.values():
            target = root / name
            if not target.exists():
                continue
            text = target.read_text(encoding="utf-8")
            if MARKER_START not in text or MARKER_END not in text:
                continue
            start = text.index(MARKER_START)
            end = text.index(MARKER_END) + len(MARKER_END)
            remainder = (text[:start] + text[end:]).strip("\n")
            target.write_text(remainder + ("\n" if remainder else ""), encoding="utf-8")
            removed.append(name)

        settings_path = root / ".strlink" / "settings.json"
        if settings_path.exists():
            try:
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                settings = {}
            settings["enabled"] = False
            atomic_json(settings_path, settings)
        return {"removed_from": removed}

    def open_folder(self, project_path):
        root = self._project(project_path)
        target = root / ".strlink"
        if not target.is_dir():
            raise ValueError("Token Guard is not enabled for this project yet")
        os.startfile(target)
