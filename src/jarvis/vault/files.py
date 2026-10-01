"""File-based vault: the Obsidian vault is a plain folder on the server (shared with Nextcloud).

Jarvis reads and writes the Markdown files directly and keeps its own index in SQLite:
- full-text search (FTS5, ranked with BM25)
- tags (frontmatter `tags:` and inline #tags, including nested #a/b)
- frontmatter fields (for lookups such as emails or birthdays)
- links between notes (so backlinks are an instant lookup)

It exposes the same methods as the Obsidian REST client, so the rest of Jarvis doesn't care which
one is in use. No Obsidian app has to be running anywhere for this to work.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
from pathlib import Path, PurePosixPath

from .. import diag
from ..db import Database
from .client import VaultError, VaultUnavailable, note_title
from .markdown import split_frontmatter

STOPWORDS = frozenset(
    "a an and are about any anything as at be can could did do does for from give have how i in is it know me "
    "my of on or our please show tell than that the their them there this to us was what whats what's when where "
    "which who whos who's why will with would you your".split())

SCHEMA = """
CREATE TABLE IF NOT EXISTS vault_notes (
    path TEXT PRIMARY KEY,
    mtime_ns INTEGER NOT NULL,
    size INTEGER NOT NULL,
    title TEXT NOT NULL,
    frontmatter TEXT NOT NULL,
    tags TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vault_links (
    src TEXT NOT NULL,
    target_name TEXT NOT NULL,     -- lower-case note name the link points at
    target_path TEXT NOT NULL      -- lower-case path (without .md) when the link includes folders
);
CREATE INDEX IF NOT EXISTS vault_links_name ON vault_links(target_name);
CREATE INDEX IF NOT EXISTS vault_links_src ON vault_links(src);
CREATE TABLE IF NOT EXISTS vault_tags (
    path TEXT NOT NULL,
    tag TEXT NOT NULL              -- lower-case, without '#'
);
CREATE INDEX IF NOT EXISTS vault_tags_tag ON vault_tags(tag);
CREATE INDEX IF NOT EXISTS vault_tags_path ON vault_tags(path);
"""
FTS = "CREATE VIRTUAL TABLE IF NOT EXISTS vault_fts USING fts5(path UNINDEXED, title, content, tokenize='porter unicode61')"

WIKILINK = re.compile(r"!?\[\[([^\]|#^]+)(?:[#^][^\]|]*)?(?:\|[^\]]*)?\]\]")
MDLINK = re.compile(r"\]\((?!https?:)([^)\s]+?\.md)(?:#[^)]*)?\)")
INLINE_TAG = re.compile(r"(?<![\w/#&\]])#([A-Za-z_][\w/-]*)")
CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
TEMP_SUFFIX = ".jarvis~"   # "*~" is in Nextcloud's and Syncthing's default ignore lists


def extract_tags(frontmatter: dict, body: str) -> list[str]:
    tags: list[str] = []
    raw = frontmatter.get("tags") or frontmatter.get("tag") or []
    if isinstance(raw, str):
        raw = re.split(r"[,\s]+", raw)
    for tag in raw if isinstance(raw, list) else []:
        if isinstance(tag, str) and tag.strip("# "):
            tags.append(tag.strip("# ").casefold())
    for match in INLINE_TAG.finditer(CODE.sub(" ", body)):
        tags.append(match.group(1).casefold())
    expanded: list[str] = []
    for tag in tags:  # a nested tag #family/kids also counts as #family
        parts = tag.split("/")
        for i in range(1, len(parts) + 1):
            candidate = "/".join(parts[:i])
            if candidate not in expanded:
                expanded.append(candidate)
    return expanded


def extract_links(body: str) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    for match in WIKILINK.finditer(body):
        target = match.group(1).strip().removesuffix(".md")
        if target:
            links.append((target.rsplit("/", 1)[-1].casefold(), target.casefold() if "/" in target else ""))
    for match in MDLINK.finditer(body):
        target = match.group(1).replace("%20", " ").removesuffix(".md")
        links.append((target.rsplit("/", 1)[-1].casefold(), target.casefold() if "/" in target else ""))
    return links


class FileVault:
    def __init__(self, root: str | Path, db: Database, file_mode: int = 0o664, dir_mode: int = 0o775,
                 writer=None) -> None:
        """`writer` (e.g. NextcloudWriter) receives all writes; without it Jarvis writes to disk itself."""
        self.root = Path(root)
        self.writer = writer
        self.db = db
        self.file_mode = file_mode
        self.dir_mode = dir_mode
        self.base_url = f"file://{self.root}"  # shown in status / diagnostics
        with db._lock:
            db._conn.executescript(SCHEMA)
            try:
                db._conn.execute(FTS)
                self.fts = True
            except Exception:
                self.fts = False
        self._refresh_lock = asyncio.Lock()

    configured = True

    # ------------------------------------------------------------------ paths
    def _full(self, path: str) -> Path:
        clean = path.replace("\\", "/").strip("/")
        parts = PurePosixPath(clean).parts
        if not clean or any(p in ("..", "") for p in parts) or clean.startswith("~"):
            raise VaultError(f"Invalid note path: {path!r}")
        full = self.root.joinpath(*parts)
        if self.root.resolve() not in full.resolve().parents and full.resolve() != self.root.resolve():
            raise VaultError(f"Path escapes the vault: {path!r}")
        return full

    def _check_root(self) -> None:
        if not self.root.is_dir():
            raise VaultUnavailable(f"The vault folder {self.root} is not mounted.")

    @staticmethod
    def _ignored(relative: str) -> bool:
        parts = relative.split("/")
        return any(p.startswith(".") for p in parts) or relative.endswith("~") or not relative.endswith(".md")

    # ------------------------------------------------------------------ status
    async def health(self) -> dict:
        if not self.root.is_dir():
            return {"ok": False, "detail": f"folder {self.root} not found — check the volume mount"}
        if not os.access(self.root, os.R_OK | os.X_OK):
            return {"ok": False, "detail": f"folder {self.root} is not readable by Jarvis (check the group id)"}
        count = self.db.one("SELECT COUNT(*) n FROM vault_notes")["n"]
        if self.writer is not None:
            written = await self.writer.health()
            if not written["ok"]:
                return written
            return {"ok": True, "detail": f"{count} notes indexed · {written['detail']}"}
        if not os.access(self.root, os.W_OK):
            return {"ok": False, "detail": f"folder {self.root} is not writable by Jarvis (check ownership)"}
        return {"ok": True, "detail": f"{count} notes indexed · writes to disk"}

    # ------------------------------------------------------------------ indexing
    def _index_file(self, relative: str, full: Path, stat: os.stat_result | None = None) -> None:
        stat = stat or full.stat()
        text = full.read_text(encoding="utf-8", errors="replace")
        frontmatter, body = split_frontmatter(text)
        tags = extract_tags(frontmatter, body)
        links = extract_links(body) + extract_links(json.dumps(frontmatter, default=str))
        title = note_title(relative)
        with self.db.transaction() as conn:  # one commit (one fsync) per note, not one per statement
            conn.execute("INSERT OR REPLACE INTO vault_notes (path, mtime_ns, size, title, frontmatter, tags) "
                         "VALUES (?, ?, ?, ?, ?, ?)",
                         (relative, stat.st_mtime_ns, stat.st_size, title, json.dumps(frontmatter, default=str),
                          json.dumps(tags)))
            conn.execute("DELETE FROM vault_links WHERE src = ?", (relative,))
            conn.executemany("INSERT INTO vault_links (src, target_name, target_path) VALUES (?, ?, ?)",
                             [(relative, name, path) for name, path in links])
            conn.execute("DELETE FROM vault_tags WHERE path = ?", (relative,))
            conn.executemany("INSERT INTO vault_tags (path, tag) VALUES (?, ?)", [(relative, t) for t in tags])
            if self.fts:
                conn.execute("DELETE FROM vault_fts WHERE path = ?", (relative,))
                conn.execute("INSERT INTO vault_fts (path, title, content) VALUES (?, ?, ?)", (relative, title, text))

    def _forget(self, relative: str) -> None:
        with self.db.transaction() as conn:
            for table, column in (("vault_notes", "path"), ("vault_links", "src"), ("vault_tags", "path")):
                conn.execute(f"DELETE FROM {table} WHERE {column} = ?", (relative,))
            if self.fts:
                conn.execute("DELETE FROM vault_fts WHERE path = ?", (relative,))

    def _refresh_sync(self) -> dict:
        self._check_root()
        known = {r["path"]: (r["mtime_ns"], r["size"]) for r in self.db.all("SELECT path, mtime_ns, size FROM vault_notes")}
        seen: set[str] = set()
        changed = 0
        for directory, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in files:
                full = Path(directory) / name
                relative = full.relative_to(self.root).as_posix()
                if self._ignored(relative):
                    continue
                seen.add(relative)
                try:
                    stat = full.stat()
                    if known.get(relative) != (stat.st_mtime_ns, stat.st_size):
                        self._index_file(relative, full, stat)
                        changed += 1
                except (OSError, UnicodeError) as error:
                    diag.warning("vault", f"could not index {relative}: {error}")
        removed = [p for p in known if p not in seen]
        for relative in removed:
            self._forget(relative)
        return {"notes": len(seen), "changed": changed, "removed": len(removed)}

    async def refresh(self) -> dict:
        async with self._refresh_lock:
            result = await asyncio.to_thread(self._refresh_sync)
        if result["changed"] or result["removed"]:
            diag.event("vault", f"index updated: {result['changed']} changed, {result['removed']} removed",
                       notes=result["notes"])
        return result

    def _fresh(self, relative: str, full: Path) -> None:
        """Re-index a single note if it changed on disk since it was indexed (e.g. edited via Nextcloud)."""
        try:
            stat = full.stat()
        except FileNotFoundError:
            self._forget(relative)
            return
        row = self.db.one("SELECT mtime_ns, size FROM vault_notes WHERE path = ?", (relative,))
        if row is None or (row["mtime_ns"], row["size"]) != (stat.st_mtime_ns, stat.st_size):
            self._index_file(relative, full, stat)

    # ------------------------------------------------------------------ files
    async def get_note(self, path: str) -> dict | None:
        self._check_root()
        full = self._full(path)
        if not full.is_file():
            return None
        text = await asyncio.to_thread(full.read_text, encoding="utf-8", errors="replace")
        frontmatter, body = split_frontmatter(text)
        relative = full.relative_to(self.root).as_posix()
        if not self._ignored(relative):
            self._fresh(relative, full)
        stat = full.stat()
        return {"path": relative, "content": text, "frontmatter": frontmatter, "tags": extract_tags(frontmatter, body),
                "stat": {"mtime": int(stat.st_mtime * 1000), "size": stat.st_size}}

    async def get_text(self, path: str) -> str | None:
        note = await self.get_note(path)
        return None if note is None else str(note["content"])

    def _write_sync(self, full: Path, text: str) -> None:
        full.parent.mkdir(parents=True, exist_ok=True, mode=self.dir_mode)
        temp = full.with_name(f".{full.name}.{secrets.token_hex(3)}{TEMP_SUFFIX}")
        try:
            with open(temp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp, self.file_mode)
            os.replace(temp, full)  # atomic: sync clients never see a half-written note
        finally:
            if temp.exists():
                temp.unlink()

    async def put_text(self, path: str, text: str) -> None:
        self._check_root()
        if not path.endswith(".md") or path.startswith("/"):
            raise VaultError(f"Jarvis only writes Markdown notes inside the vault: {path!r}")
        full = self._full(path)
        relative = full.relative_to(self.root).as_posix()
        if self.writer is not None:
            await self.writer.put(relative, text)
            if not full.is_file():  # Nextcloud writes the file to disk before it answers
                diag.warning("vault", f"saved {relative} via Nextcloud but it isn't visible at {full} — "
                                      "is VAULT_PATH mounted from the same folder as NEXTCLOUD_VAULT_DIR?")
                return
        else:
            try:
                await asyncio.to_thread(self._write_sync, full, text)
            except OSError as error:
                raise VaultError(f"Could not write {path}: {error}") from None
        if not self._ignored(relative):
            self._index_file(relative, full)

    async def list_dir(self, directory: str = "") -> list[str]:
        self._check_root()
        folder = self._full(directory) if directory.strip("/") else self.root
        if not folder.is_dir():
            return []
        items = []
        for entry in sorted(folder.iterdir()):
            if entry.name.startswith(".") or entry.name.endswith("~"):
                continue
            items.append(entry.name + "/" if entry.is_dir() else entry.name)
        return items

    # ------------------------------------------------------------------ search / discovery
    async def search_simple(self, query: str, context_length: int = 120) -> list[dict]:
        self._check_root()
        terms = [t for t in re.findall(r"[\w'-]+", query.casefold()) if len(t) > 1 and t not in STOPWORDS]
        if not terms:
            return []
        # names and other capitalised phrases ("Jennifer Key") also count as a whole phrase, which ranks higher
        phrases = [p.casefold() for p in re.findall(r"\b[A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)+", query)]
        if self.fts:
            match = " OR ".join('"' + t.replace('"', "") + '"' for t in phrases + terms)
            try:
                rows = self.db.all(
                    "SELECT path, bm25(vault_fts, 0.0, 5.0, 1.0) AS rank, "
                    "snippet(vault_fts, 2, '', '', '…', 16) AS snip FROM vault_fts WHERE vault_fts MATCH ? "
                    "ORDER BY rank LIMIT 25", (match,))
                return [{"filename": r["path"], "score": round(-r["rank"], 3),
                         "matches": [{"context": r["snip"]}]} for r in rows]
            except Exception as error:
                diag.debug("vault", f"FTS query failed, using simple match: {error}")
        results = []
        for row in self.db.all("SELECT path, title FROM vault_notes"):
            full = self.root / row["path"]
            try:
                text = full.read_text(encoding="utf-8", errors="replace").casefold()
            except OSError:
                continue
            score = sum(text.count(t) for t in terms) + 3 * sum(t in row["title"].casefold() for t in terms)
            if score:
                results.append({"filename": row["path"], "score": score, "matches": []})
        return sorted(results, key=lambda r: -r["score"])[:25]

    async def paths_mentioning(self, words: list[str], folder: str = "") -> set[str] | None:
        """Every note (under `folder`) whose text contains any of `words` — no result limit. None without FTS."""
        if not self.fts:
            return None
        match = " OR ".join('"' + w.replace('"', "") + '"' for w in words)
        rows = self.db.all("SELECT path FROM vault_fts WHERE vault_fts MATCH ? AND path LIKE ?", (match, folder + "%"))
        return {r["path"] for r in rows}

    async def frontmatter_under(self, folder: str) -> list[tuple[str, dict]]:
        """(path, properties) for every note under `folder`, from the index — no file reads."""
        self._check_root()
        found = []
        for row in self.db.all("SELECT path, frontmatter FROM vault_notes WHERE path LIKE ? ESCAPE '\\'",
                               (folder.replace("%", "\\%").replace("_", "\\_") + "%",)):
            try:
                found.append((row["path"], json.loads(row["frontmatter"]) or {}))
            except ValueError:
                continue
        return found

    async def people_notes(self, folder: str = "People/") -> list[tuple[str, str, list[str]]]:
        """(path, title, aliases) for every note under People/ — including ones you wrote yourself."""
        self._check_root()
        found = []
        for row in self.db.all("SELECT path, title, frontmatter FROM vault_notes WHERE path LIKE ? ESCAPE '\\'",
                               (folder.replace("%", "\\%").replace("_", "\\_") + "%",)):
            try:
                frontmatter = json.loads(row["frontmatter"]) or {}
            except ValueError:
                frontmatter = {}
            aliases = frontmatter.get("aliases") or frontmatter.get("alias") or []
            if isinstance(aliases, str):
                aliases = [a.strip() for a in aliases.split(",")]
            found.append((row["path"], row["title"], [str(a) for a in aliases if a]))
        return found

    async def notes_with_tag(self, tag: str) -> list[str]:
        self._check_root()
        tag = tag.lstrip("#").casefold()
        return [r["path"] for r in self.db.all("SELECT DISTINCT path FROM vault_tags WHERE tag = ? ORDER BY path", (tag,))]

    async def notes_where(self, field: str, value: str) -> list[str]:
        self._check_root()
        if not re.fullmatch(r"[\w-]+", field):
            return []
        # in SQL: a scalar property, or any item of a list property, equal to the value (case-insensitive)
        rows = self.db.all(
            "SELECT DISTINCT n.path FROM vault_notes n WHERE json_valid(n.frontmatter) AND ("
            " lower(CAST(json_extract(n.frontmatter, '$.' || ?) AS TEXT)) = lower(?)"
            " OR EXISTS (SELECT 1 FROM json_each(n.frontmatter, '$.' || ?) j"
            "            WHERE json_type(n.frontmatter, '$.' || ?) = 'array' AND lower(CAST(j.value AS TEXT)) = lower(?)))"
            " ORDER BY n.path", (field, str(value), field, field, str(value)))
        return [r["path"] for r in rows]

    async def backlinks(self, path: str) -> list[str]:
        self._check_root()
        name = note_title(path).casefold()
        full_path = path.removesuffix(".md").casefold()
        rows = self.db.all("SELECT DISTINCT src FROM vault_links WHERE (target_name = ? AND (target_path = '' OR "
                           "target_path = ?)) AND src != ? ORDER BY src", (name, full_path, path))
        return [r["src"] for r in rows]

    async def search_jsonlogic(self, expression: dict) -> list[dict]:  # compatibility shim
        raise VaultError("JsonLogic search is only available with the Obsidian REST backend.")

    async def search_dql(self, query: str) -> list[dict]:  # compatibility shim
        raise VaultError("Dataview queries are only available with the Obsidian REST backend.")


__all__ = ["FileVault", "extract_tags", "extract_links"]
