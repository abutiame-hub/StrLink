# StrLink 2.0

Windows desktop backup utility. Requires Microsoft Edge WebView2 Runtime.
The EXE bundles Python; it is not digitally signed. A trusted signing certificate
must be supplied by the publisher before signing a release.

Licensed under the [MIT License](LICENSE).

## Build

Use Python 3.14 on Windows, then from this directory:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm StrLink.spec
```

The entry point is `main.py`; `backend.py` exposes the UI API;
`backup_engine.py` owns verified snapshots and restore transactions;
`project_handoff.py` implements Project Handoff; `token_guard.py`
implements Token Guard. The UI consists of `ui/index.html`, `ui/safe.js`
and `ui/token_guard.js`, embedded by `main.py`. `installer/StrLink.iss`
builds a Windows installer (Start Menu shortcut, uninstaller,
Add/Remove Programs entry) from the compiled EXE via Inno Setup 6;
`assets/StrLink.ico` is the app/installer icon.

## Backup and restore

Select apps and individual data items, then run Backup. No items are selected
implicitly. Data is stored separately by source application in a unique
`Snapshots/<UTC timestamp>-<random id>` snapshot, never merged into the old
library or `Latest`. Only committed manifests appear in the restore selector.
Failed or cancelled snapshots without a manifest are not restorable.
Keep them for diagnosis; the program does not delete old backup data.

Encryption is enabled by default. Any non-empty password is accepted; there is
no minimum length, and the field defaults to a pre-filled value the user can
change. This is a deliberate convenience/security tradeoff for a single-user
local tool, not an oversight - a weak or guessed password on an encrypted
snapshot is exactly as recoverable as no encryption at all. Each snapshot
derives its key using PBKDF2-HMAC-SHA256 with 600,000 iterations and a random
128-bit salt. Each file uses AES-256-GCM with a random 96-bit nonce. The
encrypted manifest is authenticated with HMAC-SHA256, but is not itself
encrypted: file names, sizes and source app names remain visible.
Passwords are not saved in preferences or logs. No password recovery is possible.
Existing plaintext backups are not retroactively encrypted.

SHA-256 verifies each copied file. SQLite data is copied using SQLite's backup
API; other files that change during copying are reported as failures. This is
not an application-wide transactional snapshot: close apps before backing up
if cross-file consistency is required. Linked files/folders are refused or
reported as skipped; they are not followed outside the source tree.

Restore requires a selected dated snapshot or one of the Skills/Plugin/MCP
Libraries, explicit selection, an unchanged preview, and confirmation that
target apps are closed. Existing files are skipped by default. Replacement preserves
verified originals in `Recovery` first. Restore errors trigger automatic
rollback. Recovery data uses the snapshot's encryption settings. Manual rollback
refuses to overwrite files changed since restoration. It does not remove empty
directories created during a failed restore. Keep applications closed throughout
restore/rollback. Databases with WAL files are refused for restore.

Preferences are stored in `%LOCALAPPDATA%\StrLink\preferences.json` and logs in
`%LOCALAPPDATA%\StrLink\logs`. For snapshots/recovery/templates, the saved
destination takes precedence; otherwise the portable library beside the EXE is
used, followed by an existing `D:\AI_BACKUP` (the original development
machine's layout; harmless on a machine without one), then `~/StrLink_Backup`.
Changing the destination does not migrate old snapshots.

Skill/Plugin/MCP downloads are a separate case: they always live under
`<Desktop>\StrLink\Downloads\{Skills,Plugins,MCP}`, resolved from the real
per-user Desktop folder (via the registry, so it still finds the right place
when the Desktop is OneDrive-redirected) - independent of the snapshot
destination above, so it stays in one predictable spot regardless of where
backups are stored or whether the app is run portably. On first startup after
upgrading from an older version that kept these under the backup destination
(`Skills_Library`, `Downloads/mcp`, `Downloads/plugin`), StrLink moves
anything found there to the new Desktop location automatically; a name that
already exists at the destination is left at the old location rather than
overwritten, for manual review.

## Custom tools

Tools StrLink has no built-in detection for can be added from "Add Custom
Tool" next to the AI-tool cards: point it at the tool's own data folder, give
it a name, and confirm (or correct) three subfolder names it guesses by
scanning that folder for skills/plugins/mcp-like names - where it keeps
skills, plugins, and MCP servers, each relative to the folder just picked.
The guess is only ever a starting value the user sees and can change; there's
no real convention to rely on for a tool StrLink has never heard of.

For backup/restore, StrLink still cannot tell what's inside a folder it
doesn't otherwise recognize, so it does not sort a custom tool's contents
into skills/sessions/settings/etc. beyond those three - every item directly
inside the chosen folder becomes its own selectable entry, so picking exactly
which parts to back up works the same way selecting individual skills does
for a recognized tool. The three named subfolders are also what the Skills/
Plugin/MCP Libraries deploy into for that tool (see below) - a plain file
copy, same as backup restore already is; StrLink never edits that tool's own
settings or runs anything on its behalf. Removing a custom tool only forgets
it in StrLink; nothing on disk is touched.

## Project handoff

The Project Handoff tab copies a chosen working directory into a new
`Project_Handoffs/<project>-<timestamp>-<id>/project` folder. The source is never
modified. Export requires preview approval and verifies every copied file with
SHA-256. `HANDOFF.md`, `WORK_MAP.json`, and Arabic `START_HERE.md` sit beside the
copy; `COMPLETE.json` marks a successfully committed export. An incomplete folder
must not be treated as a successful handoff.

The map includes the last 50 available Git commits, working status, user-written
state/next steps, and bounded Claude Code/Codex tool-call metadata for matching
project paths. Raw conversations, tool arguments and executed command contents are
not copied. Gemini/Cursor sources currently provide Git history only. This is a
reconstructed timeline, not a live recorder, and does not prove a tool succeeded.

Open the copied `project` folder in Antigravity (or the chosen target), attach the
two handoff files, and follow `START_HERE.md`. No target conversation database is
modified, no AI tool is launched, and no files are uploaded. The export excludes
Git metadata, common dependency/build directories, environment files and known
credential filenames. Embedded secrets may still exist: inspect the copy before
sharing. Project handoff packages are plaintext and are not covered by the backup
password field. Git hooks and repository scripts are never run by the exporter.

## Token Guard

Token Guard does not intercept, filter, or have any visibility into what
Claude Code, Codex, or Gemini actually send to their models - StrLink is a
separate process with no hook into that request path, and no external program
can add one. What it does instead: local, mechanical detection of a project's
language/framework/package manager/entry points (no LLM involved), a `.strlink/`
folder per enabled project (`project.json`, `PROJECT_SUMMARY.md`,
`SESSION_STATE.md`, `token-policy.md`, `ignore.json`, `FILE_MAP.json`), and an
advisory instructions block appended between `<!-- STRLINK TOKEN GUARD START/END
-->` markers in `CLAUDE.md`/`AGENTS.md`/`GEMINI.md` for whichever tools are
selected. Enabling/disabling only ever touches that marked block; pre-existing
content in those files is preserved. A local scan reports indexed file
count/size and the largest excluded folders/files as plain byte counts, not
claimed token counts.

## Additional limitations

- Catalog entries are known local data paths, not proof an application is installed.
- Source files for skills, conversations, settings, plugins and connections can
  be selected individually by file/folder group. MCP servers sharing one config
  file are backed up as that file, not edited or selected as individual keys.
- API credentials can exist in settings as well as connection files. Authentication
  bound to a device/account may require sign-in again after restoration.
- Cloud-only ChatGPT/Claude chats, OS credential stores and unlisted application
  data paths are not automatically captured. Browser exports remain separate.
- Library restore does not apply templates. There is no direct cross-app
  transfer mode: incompatible settings must not be silently merged or
  overwritten, so only per-app snapshot restore and Skills/Plugin/MCP Library
  deployment are offered.
- Online packages are downloaded into `Quarantine` first. Suspicious packages and
  name conflicts remain quarantined. Transient clone failures (DNS, timeout,
  dropped connection) are retried up to 3 times before being reported; a repo
  that genuinely doesn't exist or isn't public fails immediately instead. The
  heuristic scan is not a security guarantee. Review source before trusting it.
- Skill downloads additionally require a `SKILL.md` somewhere in the repo or its
  subfolders (searched up to 3 levels deep, so multi-skill packs install as
  separate library entries); a repository with none stays quarantined instead of
  installing. MCP server and plugin downloads have no such marker to check for,
  so the whole repository is copied as-is into the MCP or Plugin Library
  (`<Desktop>\StrLink\Downloads\MCP` / `\Plugins`) instead. Downloading is not
  the same as deploying: a plugin only reaches an AI tool once explicitly
  restored from the Plugin Library (known apps with a real plugins folder, or
  a custom tool's own configured subfolder), and an MCP server only ever
  reaches a custom tool's own configured MCP subfolder the same way - no known
  app has a folder-based MCP convention, only a single JSON config file, so
  MCP Library deployment isn't offered for them. Either way this is always a
  plain file copy: it still needs its dependencies installed and a manual
  entry added to the target tool's own MCP configuration; StrLink never edits
  that configuration or runs anything on the tool's behalf.
- Portable export uses a new folder and excludes Snapshots, Recovery, credentials
  and conversations. Manually review skills/templates for embedded private data
  before sharing; the software does not promise that these files are secret-free.
- Free space is checked before backup. Restore can still fail due to space,
  permissions, antivirus or locked files; review recovery results before retrying.
- No scheduled jobs, deletion/retention policy, cloud upload, or automatic update
  is enabled. No older EXE, archive, skill library or backup is deleted by upgrade.
