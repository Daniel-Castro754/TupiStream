#!/usr/bin/env python3
import argparse
import fnmatch
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path.cwd().resolve()
PROTECTED_PREFIXES = (".github/", ".ai/")
DENIED_READ_NAMES = {".env", ".env.local", ".env.production"}
MAX_FILE_BYTES = 200_000
MAX_TOOL_OUTPUT = 60_000


def safe_rel(path_str: str) -> tuple[Path, str]:
    raw = (path_str or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or raw.startswith("../") or "/../" in f"/{raw}":
        raise ValueError("invalid repository-relative path")
    path = (ROOT / raw).resolve()
    try:
        rel = path.relative_to(ROOT).as_posix()
    except ValueError as exc:
        raise ValueError("path escapes repository") from exc
    if rel == ".git" or rel.startswith(".git/"):
        raise ValueError(".git is not accessible")
    return path, rel


def ensure_writable(rel: str) -> None:
    if rel == ".github" or rel == ".ai" or any(rel.startswith(p) for p in PROTECTED_PREFIXES):
        raise ValueError("protected path: .github/** and .ai/** cannot be modified")


def clip(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def read_file(path: str) -> str:
    p, rel = safe_rel(path)
    if p.name in DENIED_READ_NAMES:
        raise ValueError("sensitive dotenv file is not accessible")
    if not p.is_file():
        raise FileNotFoundError(rel)
    if p.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"file too large ({p.stat().st_size} bytes)")
    return clip(p.read_text(encoding="utf-8", errors="replace"))


def list_directory(path: str = ".") -> str:
    if path in ("", "."):
        p, rel = ROOT, "."
    else:
        p, rel = safe_rel(path)
    if not p.is_dir():
        raise NotADirectoryError(rel)
    items = []
    for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        if child.name == ".git":
            continue
        items.append(child.name + ("/" if child.is_dir() else ""))
    return clip("\n".join(items))


def glob_paths(pattern: str) -> str:
    pattern = (pattern or "").strip().replace("\\", "/")
    if not pattern or pattern.startswith("/"):
        raise ValueError("invalid glob pattern")
    matches = []
    for p in ROOT.rglob("*"):
        if ".git" in p.parts:
            continue
        rel = p.relative_to(ROOT).as_posix()
        if fnmatch.fnmatch(rel, pattern):
            matches.append(rel + ("/" if p.is_dir() else ""))
            if len(matches) >= 500:
                break
    return clip("\n".join(matches))


def grep_search(query: str, glob: str = "*") -> str:
    if not query:
        raise ValueError("query is required")
    out = []
    q = query.lower()
    for p in ROOT.rglob("*"):
        if ".git" in p.parts or not p.is_file():
            continue
        rel = p.relative_to(ROOT).as_posix()
        if p.name in DENIED_READ_NAMES or not fnmatch.fnmatch(rel, glob):
            continue
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                continue
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if q in line.lower():
                out.append(f"{rel}:{i}:{line}")
                if len(out) >= 200:
                    return clip("\n".join(out))
    return clip("\n".join(out))


def write_file(path: str, content: str) -> str:
    p, rel = safe_rel(path)
    ensure_writable(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"wrote {rel} ({len(content)} chars)"


def replace_in_file(path: str, old: str, new: str) -> str:
    p, rel = safe_rel(path)
    ensure_writable(rel)
    if not p.is_file():
        raise FileNotFoundError(rel)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise ValueError(f"expected old text exactly once, found {count}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    return f"updated {rel}"


TOOLS_READ = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file from the repository. Use repository-relative paths.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and directories at a repository-relative path.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob_paths",
            "description": "Find repository paths using a glob pattern such as 'app/**/*.py'.",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_search",
            "description": "Case-insensitive text search across repository files. Optional glob limits files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "glob": {"type": "string"},
                },
                "required": ["query"],
            },
        },
    },
]

TOOLS_WRITE = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or fully rewrite a repository text file. Cannot modify .github/** or .ai/**.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_in_file",
            "description": "Replace one exact unique text fragment in a file. Cannot modify .github/** or .ai/**.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
]

TOOL_FUNCS = {
    "read_file": read_file,
    "list_directory": list_directory,
    "glob_paths": glob_paths,
    "grep_search": grep_search,
    "write_file": write_file,
    "replace_in_file": replace_in_file,
}


def api_call(api_key: str, model: str, messages: list[dict], tools: list[dict]) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "max_tokens": 12000,
        "thinking": {"type": "disabled"},
    }
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"DeepSeek API HTTP {exc.code}: {clip(body, 2000)}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"DeepSeek API connection error: {exc.reason}") from exc


def execute_tool(name: str, arguments: str) -> str:
    if name not in TOOL_FUNCS:
        return f"ERROR: unknown tool {name}"
    try:
        args = json.loads(arguments or "{}")
        return str(TOOL_FUNCS[name](**args))
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("implement", "review"), required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--model", default=os.getenv("AI_DEEPSEEK_MODEL") or "deepseek-v4-flash")
    parser.add_argument("--output-file", required=True)
    parser.add_argument("--max-turns", type=int, default=18)
    args = parser.parse_args()

    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print("DEEPSEEK_API_KEY is not configured.", file=sys.stderr)
        return 2

    prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    if args.mode == "implement":
        system = """You are the implementation agent for a GitHub repository.
Use repository tools to investigate and edit the workspace. Keep changes small and scoped.
Never modify .github/** or .ai/**. Never request or expose credentials.
Do not use network or shell access. Tests and validation will be run by the workflow after you finish.
Preserve public APIs unless the user explicitly authorizes a change.
When behavior changes, add or update tests. Finish with a concise implementation summary."""
        tools = TOOLS_READ + TOOLS_WRITE
    else:
        system = """You are a read-only code reviewer for a GitHub pull request.
Use repository read tools and the provided PR context. Do not modify files.
Focus on correctness, regressions, security/privacy, compatibility, missing tests,
edge cases, performance regressions, and unnecessary complexity.
Classify meaningful findings as critical/high/medium/low. If there are no meaningful
findings, say so explicitly. Return only the review text."""
        tools = TOOLS_READ

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]

    final = ""
    for _turn in range(1, args.max_turns + 1):
        response = api_call(api_key, args.model, messages, tools)
        try:
            message = response["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected DeepSeek response: {clip(json.dumps(response), 3000)}") from exc

        assistant_msg = {
            "role": "assistant",
            "content": message.get("content") or "",
        }
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        if not tool_calls:
            final = message.get("content") or ""
            break

        for call in tool_calls:
            fn = call.get("function") or {}
            result = execute_tool(fn.get("name", ""), fn.get("arguments", "{}"))
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": clip(result),
                }
            )
    else:
        raise RuntimeError(f"DeepSeek agent exceeded max turns ({args.max_turns}).")

    if not final.strip():
        raise RuntimeError("DeepSeek returned an empty final response.")
    Path(args.output_file).write_text(final.strip() + "\n", encoding="utf-8")
    print(final.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
