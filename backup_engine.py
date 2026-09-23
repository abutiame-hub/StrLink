import base64
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


VERSION = "2.0.0"


def settings_path():
    return Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "StrLink" / "preferences.json"


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def digest(path):
    result = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def tree_size(path):
    if path.is_symlink() or path.is_junction():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for current, directories, names in os.walk(path, followlinks=False):
        for directory in list(directories):
            child = Path(current) / directory
            if child.is_symlink() or child.is_junction():
                directories.remove(directory)
        for name in names:
            try:
                total += (Path(current) / name).stat().st_size
            except OSError:
                continue
    return total


def inside(root, relative):
    root = Path(root).resolve()
    relative = Path(relative)
    if relative.is_absolute() or relative.drive or ".." in relative.parts or not relative.parts:
        raise ValueError("Unsafe relative path")
    target = root / relative
    current = root
    for part in relative.parts:
        if ":" in part:
            raise ValueError("Alternate data streams are not supported")
        current = current / part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ValueError("Symbolic links and junctions are not supported")
    if not target.resolve().is_relative_to(root):
        raise ValueError("Path escapes its data directory")
    return target


def derive_key(password, salt):
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=600000).derive(password.encode("utf-8"))


def seal(source, destination, key=None):
    if key is None:
        shutil.copy2(source, destination)
        return
    nonce = os.urandom(12)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    with open(source, "rb") as incoming, open(destination, "wb") as outgoing:
        outgoing.write(nonce)
        for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
            outgoing.write(encryptor.update(chunk))
        outgoing.write(encryptor.finalize())
        outgoing.write(encryptor.tag)


def unseal(source, destination, key=None):
    if key is None:
        shutil.copy2(source, destination)
        return
    with open(source, "rb") as incoming, open(destination, "wb") as outgoing:
        size = os.fstat(incoming.fileno()).st_size
        if size < 28:
            raise ValueError("Invalid encrypted file")
        nonce = incoming.read(12)
        incoming.seek(-16, 2)
        tag = incoming.read(16)
        incoming.seek(12)
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        remaining = size - 28
        while remaining:
            chunk = incoming.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError("Truncated encrypted file")
            remaining -= len(chunk)
            outgoing.write(decryptor.update(chunk))
        outgoing.write(decryptor.finalize())


def signature(manifest, key):
    unsigned = {name: value for name, value in manifest.items() if name != "signature"}
    return hmac.new(key, json.dumps(unsigned, sort_keys=True).encode("utf-8"), hashlib.sha256).hexdigest()


class BackupEngine:
    def __init__(self, api):
        self.api = api
        self.lock = threading.Lock()
        self.cancel = threading.Event()

    def roots(self):
        home = Path(self.api.user_profile)
        roaming = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        result = {
            "claude": home / ".claude", "claude_mcp": home,
            "gemini": home / ".gemini", "codex": home / ".codex",
            "cursor": home / ".cursor", "windsurf": home / ".codeium" / "windsurf",
            "claude_desktop": roaming / "Claude", "continue": home / ".continue",
            "lmstudio": home / ".lmstudio",
        }
        for tool in self.api._read_custom_tools():
            result[tool["id"]] = Path(tool["root"])
        return result

    def catalog(self):
        definitions = {
            "claude": {"skills": ["skills/*"], "sessions": ["projects/*", "sessions/*", "history.jsonl"], "settings": ["settings.json", "settings.local.json", "CLAUDE.md"], "plugins": ["plugins/*"]},
            "claude_mcp": {"connections": [".claude.json"]},
            "gemini": {"skills": ["config/skills/*", "skills/*"], "sessions": ["antigravity/brain/*", "tmp/*"], "settings": ["settings.json", "config/config.json", "GEMINI.md"], "connections": ["config/mcp_config.json", "antigravity/mcp_config.json", "oauth_creds.json"], "plugins": ["config/plugins/*", "extensions/*"]},
            "codex": {"skills": ["skills/*"], "sessions": ["sessions/*", "archived_sessions/*", "history.jsonl", "session_index.jsonl", "state_*.sqlite", "thread_history_*.sqlite"], "settings": ["config.toml", "AGENTS.md"], "connections": ["auth.json"], "plugins": ["plugins/*"]},
            "cursor": {"skills": ["skills/*"], "settings": ["rules/*"], "connections": ["mcp.json"]},
            "windsurf": {"connections": ["mcp_config.json"], "settings": ["rules/*"]},
            "claude_desktop": {"connections": ["claude_desktop_config.json"]},
            "continue": {"settings": ["config.yaml", "config.json"], "connections": ["mcpServers/*"]},
            "lmstudio": {"connections": ["mcp.json"]},
        }
        for tool in self.api._read_custom_tools():
            # StrLink has no per-tool knowledge of a custom folder's layout,
            # so it can't tell skills/sessions/settings apart inside it the
            # way it can for a recognized tool. "*" lists everything directly
            # inside the folder as its own item instead - the same mechanism
            # "skills/*" already uses to list each skill, just at the root.
            definitions[tool["id"]] = {"data": ["*"]}
        items = []
        for root_name, categories in definitions.items():
            root = self.roots()[root_name]
            for category, patterns in categories.items():
                for pattern in patterns:
                    for source in sorted(root.glob(pattern)):
                        relative = source.relative_to(root).as_posix()
                        try:
                            inside(root, relative)
                        except ValueError:
                            continue
                        app = "claude" if root_name == "claude_mcp" else root_name
                        try:
                            size = tree_size(source)
                        except OSError:
                            size = 0
                        items.append({"id": f"{root_name}:{category}:{relative}", "app": app, "root": root_name, "category": category, "relative": relative, "name": relative, "exists": True, "size": size})
        return items

    def _check_cancel(self):
        if self.cancel.is_set():
            raise InterruptedError("Operation cancelled")

    def _validate_target(self, entry):
        root_name = entry["root"]
        if root_name not in self.roots():
            raise ValueError("Unknown app data root")
        if root_name == "claude_mcp" and entry["relative"] != ".claude.json":
            raise ValueError("Invalid Claude configuration target")
        return inside(self.roots()[root_name], entry["relative"])

    def _load(self, snapshot_id, password="", recovery=False):
        if not snapshot_id or Path(snapshot_id).name != snapshot_id or not all(char.isalnum() or char in "-_" for char in snapshot_id):
            raise ValueError("Select a valid dated snapshot")
        folder = inside(Path(self.api.backup_dir) / ("Recovery" if recovery else "Snapshots"), snapshot_id)
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("schema") != 1:
            raise ValueError("Unsupported manifest version")
        key = None
        if manifest.get("encrypted"):
            key = derive_key(password, base64.b64decode(manifest["salt"]))
            if not hmac.compare_digest(signature(manifest, key), manifest.get("signature", "")):
                raise ValueError("Wrong password or damaged manifest")
        return folder, manifest, key

    def list_snapshots(self):
        snapshots = []
        for path in sorted((Path(self.api.backup_dir) / "Snapshots").glob("*/manifest.json"), reverse=True):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                snapshots.append({"id": path.parent.name, "created": manifest["created"], "status": manifest["status"], "encrypted": manifest.get("encrypted", False), "files": len(manifest["files"])})
            except (ValueError, OSError, KeyError):
                continue
        return snapshots

    def snapshot_items(self, snapshot_id, password=""):
        if snapshot_id == "library":
            library = Path(self.api.skills_library_dir)
            return [{"id": "library:" + path.name, "app": "library", "category": "skills", "name": path.name, "exists": True, "present": False, "size": tree_size(path)} for path in sorted(library.glob("*")) if path.is_dir() and not path.is_symlink() and not path.is_junction()]
        _, manifest, _ = self._load(snapshot_id, password)
        items = {}
        for entry in manifest["files"]:
            target = self._validate_target(entry)
            item = items.setdefault(entry["item_id"], {"id": entry["item_id"], "app": entry["app"], "category": entry["category"], "name": entry["item_name"], "exists": True, "present": False, "files": 0, "size": 0})
            item["files"] += 1
            item["size"] += entry.get("size", 0)
            item["present"] = item["present"] or target.exists()
        return list(items.values())

    def backup(self, options):
        if not self.lock.acquire(blocking=False):
            return {"success": False, "error": "Another operation is running"}
        self.cancel.clear()
        errors, records = [], []
        folder = None
        try:
            chosen = set(options.get("item_ids", []))
            selected = [item for item in self.catalog() if item["id"] in chosen and item["app"] in options.get("apps", [])]
            if not selected or len(selected) != len(chosen):
                raise ValueError("Select existing items belonging to the selected apps")
            password = options.get("password", "")
            encrypted = bool(options.get("encrypt"))
            if encrypted and not password:
                raise ValueError("Enter a verification word for encryption")
            salt = os.urandom(16)
            key = derive_key(password, salt) if encrypted else None
            identifier = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
            folder = Path(self.api.backup_dir) / "Snapshots" / identifier
            folder.mkdir(parents=True, exist_ok=False)
            files = []
            for item in selected:
                source = self._validate_target(item)
                if folder.resolve().is_relative_to(source.resolve()):
                    raise ValueError("Backup folder cannot be inside selected source data")
                if source.is_file():
                    files.append((item, source))
                else:
                    def walk_error(error):
                        errors.append(str(error))
                    for current, directories, names in os.walk(source, followlinks=False, onerror=walk_error):
                        for directory in list(directories):
                            child = Path(current) / directory
                            if child.is_symlink() or child.is_junction():
                                directories.remove(directory)
                                errors.append(f"Skipped linked folder: {child}")
                        files.extend((item, Path(current) / name) for name in names if not name.endswith(("-wal", "-shm")))
            required = sum(source.stat().st_size for _, source in files)
            if shutil.disk_usage(folder).free < required + 16 * 1024 * 1024:
                raise OSError("Not enough free space for this backup")
            with tempfile.TemporaryDirectory(prefix="strlink-verify-") as temporary:
                for index, (item, source) in enumerate(files):
                    self._check_cancel()
                    try:
                        # .resolve() here matters: on some machines %TEMP%/
                        # %USERPROFILE%-derived paths mix Windows 8.3
                        # short-name and long-name forms (confirmed on this
                        # exact box - os.environ['TEMP'] itself comes back
                        # short-form while Path.resolve() normalizes to
                        # long-form). `source` below was already normalized
                        # inside() earlier when _validate_target() built it;
                        # without resolving root the same way here,
                        # relative_to() does a pure string comparison and
                        # raises on every single file even though the paths
                        # are the same directory.
                        relative = source.relative_to(self.roots()[item["root"]].resolve()).as_posix()
                        source = inside(self.roots()[item["root"]], relative)
                        stable = source
                        if source.suffix in (".sqlite", ".db"):
                            stable = Path(temporary) / "database.sqlite"
                            stable.unlink(missing_ok=True)
                            with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5) as incoming, sqlite3.connect(stable) as outgoing:
                                incoming.backup(outgoing, pages=256, progress=lambda *_: self._check_cancel())
                        before = digest(stable)
                        blob = f"{index:08d}.blob"
                        seal(stable, folder / blob, key)
                        verified = Path(temporary) / "verified"
                        unseal(folder / blob, verified, key)
                        if digest(verified) != before or digest(stable) != before:
                            raise ValueError("Source changed during backup, or verification failed")
                        records.append({"root": item["root"], "app": item["app"], "category": item["category"], "relative": relative, "item_id": item["id"], "item_name": item["name"], "blob": blob, "sha256": before, "size": verified.stat().st_size})
                    except InterruptedError:
                        raise
                    except Exception as error:
                        errors.append(f"{source.name}: {error}")
                    self.api._emit_progress(index + 1, len(files), f"Verified {index + 1}/{len(files)}")
            status = "complete" if records and not errors else "partial" if records else "failed"
            manifest = {"schema": 1, "version": VERSION, "created": datetime.now(timezone.utc).isoformat(), "status": status, "encrypted": encrypted, "salt": base64.b64encode(salt).decode(), "files": records, "errors": errors}
            if key:
                manifest["signature"] = signature(manifest, key)
            atomic_json(folder / "manifest.json", manifest)
            return {"success": status == "complete", "status": status, "snapshot_id": identifier, "error": "; ".join(errors) or ("No files found" if not records else ""), "logs": [f"Snapshot: {identifier}", f"Verified files: {len(records)}", f"Status: {status}"] + errors}
        except Exception as error:
            return {"success": False, "status": "cancelled" if isinstance(error, InterruptedError) else "failed", "error": str(error), "logs": [str(error), "Incomplete data is not available for restore without a committed manifest."]}
        finally:
            self.lock.release()

    def _plan(self, options):
        chosen = set(options.get("item_ids", []))
        skipped = []
        if options.get("snapshot_id") == "library":
            # Resolved once up front so every relative_to() below compares
            # against the same normalized form that inside() itself returns
            # (see the matching comment in backup() for why this matters).
            folder = Path(self.api.skills_library_dir).resolve()
            manifest, key, entries = {"encrypted": False, "salt": ""}, None, []
            prefixes = {"claude": "skills", "codex": "skills", "gemini": "config/skills", "cursor": "skills"}
            if not options.get("apps") or any(app not in prefixes for app in options["apps"]):
                raise ValueError("Library deployment supports Claude Code, Codex, Gemini and Cursor only")
            # This hashes every file in every selected skill - unavoidable to
            # safely tell create/replace/unchanged apart, but with a few
            # thousand skills selected it's real work, not instant. Preview
            # and restore both call _plan() (restore re-verifies against a
            # fresh hash rather than trusting the preview's, so a file
            # changed in between is caught) - so this doubles up on a bulk
            # deploy. Report progress here so a big selection reads as
            # "working through it" instead of a stuck 0%.
            ordered = sorted(chosen)
            for item_index, item_id in enumerate(ordered):
                # No _check_cancel() here deliberately: preview() (unlike
                # backup()/restore()) never clears self.cancel at its own
                # start, so a stale flag left set by an earlier cancelled
                # operation would abort an unrelated later preview instantly.
                if not item_id.startswith("library:"):
                    raise ValueError("Invalid library item")
                name = item_id.removeprefix("library:")
                self.api._emit_progress(item_index + 1, len(ordered), f"Checking {item_index + 1}/{len(ordered)}: {name}")
                try:
                    source = inside(folder, name)
                    item_entries = []
                    for current, directories, names in os.walk(source):
                        for directory in directories:
                            inside(folder, (Path(current) / directory).relative_to(folder))
                        for filename in names:
                            file_path = Path(current) / filename
                            relative = file_path.relative_to(folder).as_posix()
                            file_path = inside(folder, relative)
                            checksum = digest(file_path)
                            for app in options["apps"]:
                                item_entries.append({"root": app, "app": app, "category": "skills", "relative": prefixes[app] + "/" + relative, "item_id": item_id, "item_name": name, "blob": relative, "sha256": checksum, "size": file_path.stat().st_size})
                    entries.extend(item_entries)
                except (OSError, ValueError) as error:
                    # One skill with a leftover empty/broken download, a
                    # Windows-invalid filename, or a locked file used to
                    # abort planning for every other selected skill along
                    # with it. Skip just this one and keep going - the
                    # caller still sees it (in "skipped"), it's just not
                    # fatal to the other 1,893 anymore.
                    skipped.append({"item_id": item_id, "name": name, "reason": str(error)})
                    chosen.discard(item_id)
        else:
            folder, manifest, key = self._load(options.get("snapshot_id"), options.get("password", ""))
            entries = [entry for entry in manifest["files"] if entry["item_id"] in chosen and entry["app"] in options.get("apps", [])]
        if not entries or chosen != {entry["item_id"] for entry in entries}:
            if skipped and not entries:
                raise ValueError("Every selected item failed to read: " + "; ".join(f"{s['name']} ({s['reason']})" for s in skipped[:5]))
            raise ValueError("Select items from the chosen snapshot and apps")
        policy = options.get("conflict", "skip")
        if policy not in ("skip", "replace"):
            raise ValueError("Unknown conflict policy")
        plan, seen = [], set()
        for entry in entries:
            target = self._validate_target(entry)
            normalized = str(target).casefold()
            if normalized in seen:
                raise ValueError("Duplicate restore target")
            seen.add(normalized)
            try:
                exists = target.exists()
                if exists and not target.is_file():
                    raise ValueError(f"Target is not a file: {target}")
                current_hash = digest(target) if exists else None
            except OSError as error:
                skipped.append({"item_id": entry["item_id"], "name": entry.get("item_name", entry["item_id"]), "reason": str(error)})
                continue
            action = "unchanged" if current_hash == entry["sha256"] else "skip" if exists and policy == "skip" else "replace" if exists else "create"
            plan.append({"entry": entry, "target": target, "action": action, "before": current_hash})
        fingerprint = hashlib.sha256(json.dumps([(str(row["target"]), row["before"], row["action"], row["entry"]["sha256"]) for row in plan], sort_keys=True).encode()).hexdigest()
        return folder, manifest, key, plan, fingerprint, skipped

    def preview(self, options):
        try:
            _, _, _, plan, fingerprint, skipped = self._plan(options)
            return {"success": True, "token": fingerprint, "counts": {action: sum(row["action"] == action for row in plan) for action in ("create", "replace", "skip", "unchanged")}, "files": [{"path": str(row["target"]), "action": row["action"]} for row in plan], "skipped": skipped}
        except Exception as error:
            return {"success": False, "error": str(error)}

    def restore(self, options):
        if not self.lock.acquire(blocking=False):
            return {"success": False, "error": "Another operation is running"}
        self.cancel.clear()
        applied, recovery_records = [], []
        recovery, key = None, None
        try:
            folder, manifest, key, plan, fingerprint, skipped = self._plan(options)
            if options.get("preview_token") != fingerprint:
                raise ValueError("Preview is missing or files changed. Preview again before restoring.")
            changes = [row for row in plan if row["action"] in ("create", "replace")]
            if not changes:
                return {"success": False, "status": "no_changes", "error": "No files to restore with this conflict policy", "logs": []}
            if not options.get("apps_closed"):
                raise ValueError("Close the target applications before restoring")
            recovery_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
            recovery = Path(self.api.backup_dir) / "Recovery" / recovery_id
            recovery.mkdir(parents=True)
            journal = {"schema": 1, "created": datetime.now(timezone.utc).isoformat(), "encrypted": manifest.get("encrypted", False), "salt": manifest["salt"], "status": "preparing", "files": recovery_records}
            def save_journal(status):
                journal["status"] = status
                if key:
                    journal["signature"] = signature(journal, key)
                atomic_json(recovery / "manifest.json", journal)
            with tempfile.TemporaryDirectory(prefix="strlink-restore-") as temporary:
                for index, row in enumerate(changes):
                    self._check_cancel()
                    entry, target = row["entry"], row["target"]
                    if target.suffix in (".sqlite", ".db") and Path(str(target) + "-wal").exists():
                        raise ValueError("Database has a WAL file; close the app and checkpoint its database before restore")
                    staged = Path(temporary) / str(index)
                    unseal(inside(folder, entry["blob"]), staged, key)
                    if digest(staged) != entry["sha256"]:
                        raise ValueError(f"Damaged backup: {entry['relative']}")
                    row["staged"] = staged
                    if row["before"]:
                        original_blob = f"{index:08d}.blob"
                        seal(target, recovery / original_blob, key)
                        check = Path(temporary) / "original-check"
                        unseal(recovery / original_blob, check, key)
                        if digest(check) != row["before"]:
                            raise ValueError("Original changed while preparing recovery copy")
                    else:
                        original_blob = None
                    recovery_records.append({"root": entry["root"], "relative": entry["relative"], "blob": original_blob, "sha256": row["before"], "restored_sha256": entry["sha256"]})
                save_journal("ready")
                for index, row in enumerate(changes):
                    self._check_cancel()
                    target = self._validate_target(row["entry"])
                    if (digest(target) if target.exists() else None) != row["before"]:
                        raise ValueError("Target changed since preview; restore stopped")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary_target = target.with_name(target.name + ".strlink-" + uuid.uuid4().hex)
                    try:
                        shutil.copy2(row["staged"], temporary_target)
                        os.replace(temporary_target, target)
                    finally:
                        temporary_target.unlink(missing_ok=True)
                    applied.append(recovery_records[index])
                    if digest(target) != row["entry"]["sha256"]:
                        raise ValueError("Restored file verification failed")
                    self.api._emit_progress(index + 1, len(changes), f"Restored {index + 1}/{len(changes)}")
                save_journal("complete")
            logs = [f"Restored and verified {len(changes)} files", f"Recovery copy: {recovery_id}", "Unselected and skipped files were left untouched."]
            if skipped:
                logs.append(f"{len(skipped)} item(s) could not be read and were skipped:")
                logs.extend(f"  - {s['name']}: {s['reason']}" for s in skipped[:20])
            return {"success": True, "status": "complete", "logs": logs, "skipped": skipped}
        except Exception as error:
            rollback_errors = []
            if recovery:
                for entry in reversed(applied):
                    try:
                        self._recover_entry(recovery, entry, key)
                    except Exception as rollback_error:
                        rollback_errors.append(str(rollback_error))
                try:
                    save_journal("rollback_failed" if rollback_errors else "rolled_back")
                except Exception as journal_error:
                    rollback_errors.append(str(journal_error))
            return {"success": False, "status": "failed", "error": str(error), "logs": [str(error), "Automatic rollback: " + ("needs attention" if rollback_errors else "completed; no applied changes remain")] + rollback_errors}
        finally:
            self.lock.release()

    def _recover_entry(self, folder, entry, key):
        target = self._validate_target(entry)
        if entry["blob"] is None:
            target.unlink(missing_ok=True)
        else:
            temporary = target.with_name(target.name + ".strlink-" + uuid.uuid4().hex)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                unseal(inside(folder, entry["blob"]), temporary, key)
                if digest(temporary) != entry["sha256"]:
                    raise ValueError("Recovery file verification failed")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)

    def recovery_list(self):
        result = []
        for path in sorted((Path(self.api.backup_dir) / "Recovery").glob("*/manifest.json"), reverse=True):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                if manifest["status"] in ("complete", "ready", "rollback_failed"):
                    result.append({"id": path.parent.name, "status": manifest["status"], "encrypted": manifest.get("encrypted", False)})
            except (OSError, ValueError, KeyError):
                continue
        return result

    def rollback(self, identifier, password=""):
        if not self.lock.acquire(blocking=False):
            return {"success": False, "error": "Another operation is running"}
        try:
            folder, manifest, key = self._load(identifier, password, recovery=True)
            for entry in manifest["files"]:
                target = self._validate_target(entry)
                current = digest(target) if target.exists() else None
                if current not in (entry["sha256"], entry["restored_sha256"]):
                    raise ValueError("A file changed after restore; rollback refused to protect newer work")
                if entry["blob"]:
                    with tempfile.TemporaryDirectory(prefix="strlink-recovery-check-") as temporary:
                        check = Path(temporary) / "check"
                        unseal(inside(folder, entry["blob"]), check, key)
                        if digest(check) != entry["sha256"]:
                            raise ValueError("Damaged recovery data")
            for entry in reversed(manifest["files"]):
                self._recover_entry(folder, entry, key)
            manifest["status"] = "rolled_back"
            if key:
                manifest["signature"] = signature(manifest, key)
            atomic_json(folder / "manifest.json", manifest)
            return {"success": True, "logs": ["Recovery completed"]}
        except Exception as error:
            return {"success": False, "error": str(error)}
        finally:
            self.lock.release()
