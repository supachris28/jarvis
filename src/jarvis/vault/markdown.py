"""Markdown helpers: frontmatter, Jarvis-managed blocks, safe names and links.

Jarvis only rewrites text between its own markers, so anything you type elsewhere
in a note is preserved:

    <!-- jarvis:timeline -->
    ...generated...
    <!-- /jarvis:timeline -->
"""

from __future__ import annotations

import re
import yaml

FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
UNSAFE = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]')


def split_frontmatter(text: str) -> tuple[dict, str]:
    match = FRONTMATTER.match(text)
    if not match:
        return {}, text
    try:
        data = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(data, dict):
        return {}, text
    return data, text[match.end():]


def join_frontmatter(frontmatter: dict, body: str) -> str:
    if not frontmatter:
        return body
    dumped = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True, default_flow_style=None).strip()
    return f"---\n{dumped}\n---\n{body.lstrip(chr(10))}"


def merge_frontmatter(existing: dict, updates: dict, union_keys: tuple[str, ...] = ("aliases", "emails", "phones", "tags")) -> dict:
    """Add missing keys and union list keys; never overwrite a value the user set."""
    merged = dict(existing)
    for key, value in updates.items():
        if key in union_keys:
            current = merged.get(key) or []
            if not isinstance(current, list):
                current = [current]
            for item in value if isinstance(value, list) else [value]:
                if item not in current:
                    current.append(item)
            merged[key] = current
        elif key == "updated":
            merged[key] = value
        elif key not in merged or merged[key] in (None, ""):
            merged[key] = value
    return merged


def block_markers(name: str) -> tuple[str, str]:
    return f"<!-- jarvis:{name} -->", f"<!-- /jarvis:{name} -->"


def replace_block(body: str, name: str, content: str, heading: str | None = None) -> str:
    start, end = block_markers(name)
    new_block = f"{start}\n{content.strip()}\n{end}"
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    if pattern.search(body):
        return pattern.sub(lambda _m: new_block, body, count=1)
    prefix = body.rstrip() + "\n\n" if body.strip() else ""
    if heading:
        prefix += f"{heading}\n\n"
    return prefix + new_block + "\n"


def read_block(body: str, name: str) -> str | None:
    start, end = block_markers(name)
    match = re.search(re.escape(start) + r"\n?(.*?)\n?" + re.escape(end), body, re.DOTALL)
    return match.group(1) if match else None


def safe_name(text: str, limit: int = 80) -> str:
    cleaned = UNSAFE.sub(" ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return (cleaned[:limit].rstrip(" .") or "Untitled")


def link(path: str, alias: str | None = None) -> str:
    target = path.removesuffix(".md")
    if alias and alias != target.rsplit("/", 1)[-1]:
        alias = alias.replace("|", "-").replace("]", ")").replace("[", "(")
        return f"[[{target}|{alias}]]"
    return f"[[{target}]]"


def one_line(text: str, limit: int = 140) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

