#!/usr/bin/env python3
"""One recording round for Hack Day Sonipat, before the 6:30 freeze.

Each round, for every team in teams.csv: ask GitHub for the current HEAD, fetch the
repo into a bare mirror, and record facts about that exact commit. Nothing is checked
out and no team code is run; files are read straight from git objects.

A normal round writes data/rounds/<round>/ and appends to data/history.csv.
The --final round does the same and also writes data/freeze.csv, read-only, which
is the hand-off to the review stage after 6:30.

Python 3.9 standard library only, so it runs on the system interpreter.
"""
import argparse
import csv
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

GITHUB_URL = re.compile(
    r"^(?:https?://(?:www\.)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?(?:[/?#].*)?$"
)

TEST_FILE = re.compile(
    r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$",
    re.I,
)
TEST_COMMAND = re.compile(
    r"\b(pytest|unittest|(npm|yarn|pnpm|bun) (run )?[\w:.-]*test[\w:.-]*|go test|cargo (test|nextest)|"
    r"mvn (-\S+ )*test|gradle(w)? test|jest|vitest|mocha|playwright test|make test|tox|nox|"
    r"(hatch|pdm|rye) (run )?[\w:.-]*test|deno test|dotnet test|flutter test|swift test|rspec|phpunit|ctest)\b",
    re.I,
)
LICENSE_FILE = re.compile(r"^(LICEN[CS]E|COPYING)(\.[A-Za-z]+)?$", re.I)
PR_TEMPLATE = re.compile(r"(^|/)pull_request_template(\.md)?$|^\.github/PULL_REQUEST_TEMPLATE/", re.I)
MODELS_SECTION = re.compile(r"^\s*(#+\s*models?\b|\*\*models?\*\*|models?\s*(used)?\s*:)", re.I | re.M)

# Model names and AI SDKs, grepped from code, configs and HACKATHON.md.
MODEL_PATTERNS = {
    # open-weight families
    "gemma": r"\bgemma[-_:0-9a-z.]*",
    "llama": r"\b(meta-)?llama[-_:0-9a-z.]*",
    "mistral": r"\b(mistral|mixtral|codestral)[-_:0-9a-z.]*",
    "qwen": r"\bqwen[-_:0-9a-z.]*",
    "phi": r"\bphi-?[234][-_:0-9a-z.]*",
    "deepseek": r"\bdeepseek[-_:0-9a-z.]*",
    "falcon": r"\bfalcon-[0-9][-_0-9a-z.]*",
    "smollm": r"\bsmollm[-_:0-9a-z.]*",
    "whisper": r"\bwhisper[-_:0-9a-z.]*",
    "stable-diffusion": r"\b(stable-diffusion|sdxl)[-_0-9a-z.]*",
    "embedding": r"\b(bert-(base|large)|roberta-(base|large)|distilbert|all-minilm|minilm-l\d+|bge-(small|base|large|m3)|"
                 r"e5-(small|base|large|mistral)|nomic-embed|sentence-transformers|sentence_transformers)[-_0-9a-z./]*",
    # closed models
    "gpt": r"\b(gpt-[345][-_0-9a-z.]*|o[134]-(mini|preview)\b)",
    "claude": r"\bclaude-[-_0-9a-z.]+",
    "gemini": r"\bgemini-[-_0-9a-z.]+",
    # SDKs, providers and local servers
    "sdk:openai": r"\b(from openai import|import openai|require\(['\"]openai['\"]\)|from ['\"]openai['\"])",
    "sdk:anthropic": r"\b(import anthropic|from anthropic import|@anthropic-ai/sdk)",
    "sdk:google-genai": r"(google\.generativeai|google\.genai|google-genai|@google/genai|@google/generative-ai)",
    "sdk:ollama": r"\b(import ollama|from ollama import|ollama\.(chat|generate)|localhost:11434|127\.0\.0\.1:11434)",
    "sdk:transformers": r"\b(from transformers import|import transformers|@huggingface/transformers|@xenova/transformers)",
    "sdk:llama.cpp": r"\b(llama_cpp|llama-cpp|llama\.cpp|\.gguf\b)",
    "sdk:vllm": r"\b(import vllm|from vllm import|vllm serve)",
    "sdk:groq": r"\b(from groq import|import groq|groq-sdk)",
    "sdk:together": r"\b(from together import|import together)",
    "sdk:huggingface-hub": r"\b(huggingface_hub|InferenceClient|api-inference\.huggingface\.co)",
    "sdk:langchain": r"\blangchain[-_a-z]*",
    "endpoint:openai-compatible-local": r"(base_url|baseURL|api_base)\s*[=:]\s*['\"]https?://(localhost|127\.0\.0\.1|0\.0\.0\.0)",
}
MODEL_RES = {k: re.compile(v, re.I) for k, v in MODEL_PATTERNS.items()}

# Text in a repo that tries to steer the reviewer. Recorded so the review stage can flag it.
INJECTION = re.compile(
    # Aimed at the judge or the score only: "You are an AI assistant" opens most LLM apps' prompts.
    r"(ignore (all |any )?(previous|prior|above|earlier) (instructions|rules|rubric)|"
    r"give (this|the|our) (repo|project|team|submission)? ?(full|max(imum)?|perfect|top) (marks|score|points)|"
    r"(dear|note to|attention|hey) (ai|llm|claude|gpt|judges?|reviewers?|evaluators?|graders?)\b|"
    r"(judges?|reviewers?|evaluators?|graders?),? (please )?(give|award|score|rate)|"
    r"(this|our) (project|repo|submission) (deserves|should (get|receive|be given)) (full|max(imum)?|top|perfect)|"
    r"score this (repo|project|submission) (as )?(10|100|70|30))",
    re.I,
)

# Keys sitting in the files at the recorded commit. History is not scanned.
SECRET_PATTERNS = {
    "openai-key": r"\bsk-(proj-)?[A-Za-z0-9_-]{20,}",
    "anthropic-key": r"\bsk-ant-[A-Za-z0-9_-]{20,}",
    "google-api-key": r"\bAIza[0-9A-Za-z_-]{35}\b",
    "github-token": r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{50,}",
    "huggingface-token": r"\bhf_[A-Za-z0-9]{30,}\b",
    "aws-access-key": r"\bAKIA[0-9A-Z]{16}\b",
    "groq-key": r"\bgsk_[A-Za-z0-9]{40,}\b",
    "private-key": r"-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
}
SECRET_RES = {k: re.compile(v) for k, v in SECRET_PATTERNS.items()}

TEXT_EXT = {
    ".py", ".ipynb", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".kt", ".go", ".rs",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".swift", ".dart", ".scala", ".r",
    ".sh", ".bash", ".ps1", ".sql", ".html", ".css", ".scss", ".vue", ".svelte", ".lua", ".jl",
    ".md", ".txt", ".rst", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env", ".xml",
    ".gradle", ".properties", ".dockerfile", ".tf",
}
TEXT_NAMES = {"dockerfile", "makefile", "procfile", ".env", ".env.example", "requirements.txt"}
# Generated lock files: huge, full of hashes, and never the team's own code.
LOCK_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "cargo.lock", "uv.lock",
              "pipfile.lock", "composer.lock", "gemfile.lock", "go.sum", "bun.lockb"}
# Lines of code are counted only for these; docs and data are reported separately.
CODE_EXT = {
    ".py", ".ipynb", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".kt", ".go", ".rs",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".swift", ".dart", ".scala", ".r",
    ".sh", ".bash", ".sql", ".html", ".css", ".scss", ".vue", ".svelte", ".lua", ".jl",
}

MAX_LOCATIONS = 5
PR_PAGES = 5  # 500 PRs


def now():
    return dt.datetime.now().astimezone()


def iso(t):
    return t.isoformat(timespec="seconds")


def run(cmd, cwd=None, timeout=120, check=True, input_bytes=None):
    p = subprocess.run(
        cmd, cwd=cwd, input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout
    )
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])}: {p.stderr.decode(errors='replace').strip()[:300]}")
    return p


def git(repo, *args, timeout=120, check=True, input_bytes=None):
    return run(["git", "-C", str(repo), *args], timeout=timeout, check=check, input_bytes=input_bytes)


def load_config(path):
    cfg = json.loads(Path(path).read_text())
    base = Path(path).resolve().parent
    cfg["teams_csv"] = (base / cfg["teams_csv"]).resolve()
    cfg["data_dir"] = (base / cfg["data_dir"]).resolve()
    cfg["skip_dirs"] = set(cfg.get("skip_dirs", []))
    cfg["_hash"] = hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]
    return cfg


HEADER_ALIASES = {
    "team_id": ("team_id", "teamid", "team", "team_no", "team_number", "id"),
    "problem": ("problem", "problem_id", "problem_no", "problem_number", "problem_statement", "track"),
    "repo_url": ("repo_url", "repo", "repository", "repository_url", "repo_link", "github", "github_url",
                 "github_repo", "github_link", "github_repository"),
}


def file_key(team_id):
    """A team id safe to use in a file name: "Team Alpha #3" becomes "Team-Alpha-3"."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", team_id).strip("-.") or "team"


def load_teams(cfg):
    """Read teams.csv, normalise each GitHub URL, and split off rows that cannot be recorded."""
    path = cfg["teams_csv"]
    if not path.exists():
        sys.exit(f"teams file missing: {path} (columns: team_id,problem,repo_url; see teams.example.csv)")
    teams, rejected, seen_ids, seen_keys, by_repo = [], [], set(), set(), {}
    with path.open(newline="", encoding="utf-8-sig") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            raw = {re.sub(r"[\s-]+", "_", k.strip().lower()): (v or "").strip() for k, v in row.items() if k}
            row = {}
            for field, names in HEADER_ALIASES.items():
                row[field] = next((raw[n] for n in names if raw.get(n)), "")
            tid, url = row["team_id"], row["repo_url"]
            if not tid:
                rejected.append({"line": i, "team_id": "", "repo_url": url, "reason": "no team_id"})
                continue
            if tid in seen_ids:
                rejected.append({"line": i, "team_id": tid, "repo_url": url, "reason": "duplicate team_id"})
                continue
            m = GITHUB_URL.match(url)
            if not m:
                rejected.append({"line": i, "team_id": tid, "repo_url": url, "reason": "not a GitHub repo URL"})
                continue
            key = file_key(tid)
            if key.lower() in seen_keys:
                rejected.append({"line": i, "team_id": tid, "repo_url": url, "reason": f"team_id clashes with another as file name {key}"})
                continue
            seen_ids.add(tid)
            seen_keys.add(key.lower())
            slug = f"{m.group(1)}/{m.group(2)}"
            branch_in_url = re.search(r"/tree/([^/?#]+)", url)
            team = {
                "team_id": tid,
                "key": key,
                # A /tree/<branch> link is recorded, but the freeze takes the default branch.
                "url_branch": branch_in_url.group(1) if branch_in_url else None,
                "problem": row.get("problem", ""),
                "repo_url": url,
                "slug": slug,
                "clone_url": f"https://github.com/{slug}.git",
            }
            by_repo.setdefault(slug.lower(), []).append(tid)
            teams.append(team)
    for t in teams:
        others = [x for x in by_repo[t["slug"].lower()] if x != t["team_id"]]
        t["shared_repo_with"] = others
    return teams, rejected


# ---------------------------------------------------------------- GitHub API

def gh_api(path, paginate=False):
    cmd = ["gh", "api", "-H", "Accept: application/vnd.github+json", path]
    if paginate:
        # One compact object per line across all pages, so pages never need stitching.
        cmd[2:2] = ["--paginate", "--jq", ".[]"]
    p = run(cmd, timeout=120, check=False)
    if p.returncode != 0:
        return None, p.stderr.decode(errors="replace").strip()[:300]
    out = p.stdout.decode(errors="replace").strip()
    try:
        if paginate:
            return [json.loads(line) for line in out.splitlines() if line.strip()], None
        return json.loads(out or "null"), None
    except ValueError as e:
        return None, f"bad JSON: {e}"


def github_facts(team, sha, api_dir):
    """Repo visibility and license, PRs, and CI on the recorded SHA. Raw responses are kept."""
    slug = team["slug"]
    api_dir.mkdir(parents=True, exist_ok=True)
    facts, errors = {}, {}

    def fetch(name, path, paginate=False):
        data, err = gh_api(path, paginate)
        if err:
            errors[name] = err
        else:
            (api_dir / f"{name}.json").write_text(json.dumps(data, indent=1))
        return data

    repo = fetch("repo", f"repos/{slug}")
    if repo:
        lic = repo.get("license") or {}
        facts["repo"] = {
            "private": repo.get("private"),
            "visibility": repo.get("visibility"),
            "archived": repo.get("archived"),
            "fork": repo.get("fork"),
            "default_branch": repo.get("default_branch"),
            "size_kb": repo.get("size"),
            "license_spdx": lic.get("spdx_id"),
            "created_at": repo.get("created_at"),
            "pushed_at": repo.get("pushed_at"),
        }

    # Pages one by one, newest first, and stops at PR_PAGES: a fork of a popular repo can
    # carry tens of thousands of PRs.
    pulls, truncated = [], False
    for page in range(1, PR_PAGES + 1):
        batch = fetch(f"pulls-{page}", f"repos/{slug}/pulls?state=all&per_page=100&page={page}")
        if not isinstance(batch, list):
            pulls = pulls or None
            break
        pulls.extend(batch)
        if len(batch) < 100:
            break
    else:
        truncated = True
    if isinstance(pulls, list):
        prs = []
        for p in pulls:
            body = p.get("body") or ""
            prs.append({
                "number": p.get("number"),
                "title": (p.get("title") or "")[:120],
                "author": (p.get("user") or {}).get("login"),
                "state": p.get("state"),
                "merged_at": p.get("merged_at"),
                "merge_commit_sha": p.get("merge_commit_sha") if p.get("merged_at") else None,
                "base": (p.get("base") or {}).get("ref"),
                "body_chars": len(body),
                "models_section": bool(MODELS_SECTION.search(body)),
            })
        facts["prs"] = {
            "count": len(prs),
            "merged": sum(1 for p in prs if p["merged_at"]),
            "open": sum(1 for p in prs if p["state"] == "open"),
            "with_models_section": sum(1 for p in prs if p["models_section"]),
            "truncated_at": len(prs) if truncated else None,
            "items": sorted(prs, key=lambda p: p["number"] or 0),
        }

    if sha:
        runs = fetch("check_runs", f"repos/{slug}/commits/{sha}/check-runs?per_page=100")
        status = fetch("status", f"repos/{slug}/commits/{sha}/status")
        facts["ci"] = ci_summary(runs, status)

    return facts, errors


def ci_summary(runs, status):
    """CI on one commit. Re-runs of a check are collapsed to the newest; GitHub Actions is
    reported on its own, since the rubric asks for Actions CI, not Dependabot or other apps."""
    latest = {}
    for r in (runs or {}).get("check_runs", []) or []:
        app = (r.get("app") or {}).get("slug") or ""
        key = (app, r.get("name"))
        if key not in latest or (r.get("id") or 0) > (latest[key].get("id") or 0):
            latest[key] = {"id": r.get("id"), "app": app, "name": r.get("name"),
                           "status": r.get("status"), "conclusion": r.get("conclusion")}
    items = sorted(latest.values(), key=lambda i: (i["app"], i["name"] or ""))
    for s in (status or {}).get("statuses", []) or []:
        st = s.get("state")
        items.append({
            "id": s.get("id"),
            "app": "commit-status",
            "name": s.get("context"),
            "status": "completed" if st in ("success", "failure", "error") else "in_progress",
            "conclusion": {"error": "failure"}.get(st, st),
        })

    def state(checks):
        if not checks:
            return "none"
        if any(i["status"] != "completed" for i in checks):
            return "pending"
        if any(i["conclusion"] in ("failure", "timed_out", "cancelled", "action_required", "startup_failure") for i in checks):
            return "fail"
        return "pass"

    actions = [i for i in items if i["app"] == "github-actions"]
    return {
        "state": state(actions),
        "all_checks_state": state(items),
        "failing": sorted({f"{i['app']}: {i['name']}" for i in items if i["conclusion"] not in (None, "success", "neutral", "skipped")}),
        "checks": items,
    }


# ---------------------------------------------------------------- git

def ls_remote(team, timeout, attempts=3):
    """Current default branch and HEAD SHA as GitHub reports them. A network error is
    retried, so one dropped connection at 6:30 does not leave a team out of the freeze."""
    for attempt in range(attempts):
        try:
            p = run(["git", "ls-remote", "--symref", team["clone_url"], "HEAD"], timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            err = "timed out"
        else:
            if p.returncode == 0:
                break
            err = p.stderr.decode(errors="replace").lower()
            if "not found" in err or "could not read username" in err or "authentication" in err or "terminal prompts disabled" in err:
                return None, None, "not found or private"
        if attempt + 1 == attempts:
            return None, None, f"ls-remote failed after {attempts} tries: {err.strip()[:200]}"
        time.sleep(3 * (attempt + 1))
    branch, sha = None, None
    for line in p.stdout.decode().splitlines():
        if line.startswith("ref:"):
            branch = line.split()[1].replace("refs/heads/", "")
        elif line.endswith("\tHEAD"):
            sha = line.split()[0]
    if not sha:
        return None, None, "empty repo (no commits)"
    return branch, sha, None


def sync_mirror(team, mirror, timeout):
    """Bare clone on first sight, fetch afterwards. Only branches and tags, never a work tree.
    Returns what it did; a team that changed its repo URL gets a fresh clone."""
    action = "cloned"
    if (mirror / "HEAD").exists():
        origin = git(mirror, "config", "--get", "remote.origin.url", check=False).stdout.decode().strip()
        if origin.lower() == team["clone_url"].lower():
            git(mirror, "fetch", "--quiet", "--prune", "--tags", "origin", timeout=timeout)
            return "fetched"
        action = f"recloned (repo changed from {origin})"
    if mirror.exists():
        shutil.rmtree(mirror)
    mirror.parent.mkdir(parents=True, exist_ok=True)
    try:
        run(["git", "clone", "--bare", "--quiet", team["clone_url"], str(mirror)], timeout=timeout)
    except Exception:
        shutil.rmtree(mirror, ignore_errors=True)  # never leave a half clone behind
        raise
    git(mirror, "config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*")
    return action


def read_blobs(mirror, entries):
    """Contents of many blobs in one git process, keyed by path."""
    if not entries:
        return {}
    req = "".join(f"{e['oid']}\n" for e in entries).encode()
    out = git(mirror, "cat-file", "--batch", input_bytes=req, timeout=300).stdout
    blobs, pos = {}, 0
    for e in entries:
        nl = out.index(b"\n", pos)
        header = out[pos:nl].split()
        if len(header) < 3:  # "<oid> missing": no content follows
            pos = nl + 1
            continue
        size = int(header[2])
        start = nl + 1
        blobs[e["path"]] = out[start:start + size]
        pos = start + size + 1
    return blobs


def notebook_code_lines(text):
    """Non-blank lines in a notebook's code cells; markdown cells and outputs do not count."""
    try:
        cells = json.loads(text).get("cells", [])
    except (ValueError, AttributeError):
        return 0
    n = 0
    for c in cells:
        if isinstance(c, dict) and c.get("cell_type") == "code":
            src = c.get("source", "")
            src = "".join(src) if isinstance(src, list) else str(src)
            n += sum(1 for l in src.splitlines() if l.strip())
    return n


def find_lines(text, regex, path, limit):
    hits = []
    for n, line in enumerate(text.splitlines(), start=1):
        m = regex.search(line)
        if m:
            hits.append({"at": f"{path}:{n}", "match": m.group(0)[:80]})
            if len(hits) >= limit:
                break
    return hits


def tree_facts(cfg, mirror, sha):
    """Everything readable from the files at one commit."""
    raw = git(mirror, "ls-tree", "-r", "-l", "-z", "--full-tree", sha).stdout.decode(errors="replace")
    files = []
    for line in raw.split("\0"):
        if not line:
            continue
        meta, path = line.split("\t", 1)
        mode, typ, oid, size = meta.split()
        if typ != "blob":
            continue
        files.append({"path": path, "oid": oid, "size": int(size) if size.isdigit() else 0})

    paths = [f["path"] for f in files]
    skip = cfg["skip_dirs"]
    vendored = {p for p in paths if any(part in skip for part in p.split("/")[:-1])}
    kept = [f for f in files if f["path"] not in vendored]

    def is_text(path):
        name = path.rsplit("/", 1)[-1].lower()
        if name in LOCK_FILES:
            return False
        return name in TEXT_NAMES or os.path.splitext(name)[1] in TEXT_EXT or name.startswith(".env")

    text_files, budget, truncated = [], cfg.get("max_text_bytes_per_repo", 150_000_000), False
    for f in kept:
        if is_text(f["path"]) and f["size"] <= cfg["max_text_file_bytes"]:
            if f["size"] > budget:
                truncated = True
                continue
            budget -= f["size"]
            text_files.append(f)
    blobs = read_blobs(mirror, text_files)

    loc, models, injections, secrets = {}, {}, [], []
    for f in text_files:
        path = f["path"]
        text = blobs.get(path, b"").decode("utf-8", errors="replace")
        ext = os.path.splitext(path)[1].lower() or path.rsplit("/", 1)[-1].lower()
        if ext == ".ipynb":
            loc[ext] = loc.get(ext, 0) + notebook_code_lines(text)
        elif ext in CODE_EXT:
            loc[ext] = loc.get(ext, 0) + sum(1 for l in text.splitlines() if l.strip())
        for name, rx in MODEL_RES.items():
            if rx.search(text):
                entry = models.setdefault(name, {"files": 0, "locations": [], "names": set()})
                entry["files"] += 1
                hits = find_lines(text, rx, path, MAX_LOCATIONS)
                for h in hits:
                    entry["names"].add(h["match"].lower())
                room = MAX_LOCATIONS - len(entry["locations"])
                entry["locations"].extend(h["at"] for h in hits[:room])
        if len(injections) < 20:
            injections.extend(find_lines(text, INJECTION, path, 20 - len(injections)))
        for name, rx in SECRET_RES.items():
            for h in find_lines(text, rx, path, 3):
                # Never store the secret itself: rule, place and a short prefix only.
                secrets.append({"rule": name, "at": h["at"], "prefix": h["match"][:6] + "…"})
    for entry in models.values():
        entry["names"] = sorted(entry["names"])[:10]

    workflows = [f for f in files if re.match(r"^\.github/workflows/[^/]+\.ya?ml$", f["path"])]
    wf_text = read_blobs(mirror, workflows)
    wf = []
    for f in workflows:
        t = wf_text.get(f["path"], b"").decode(errors="replace")
        wf.append({
            "path": f["path"],
            "on_push": bool(re.search(r"\bpush\b", t)),
            "on_pull_request": bool(re.search(r"\bpull_request(_target)?\b", t)),
            "runs_tests": bool(TEST_COMMAND.search(t)),
        })

    license_files = [p for p in paths if LICENSE_FILE.match(p)]
    license_kind = None
    if license_files:
        lt = read_blobs(mirror, [f for f in files if f["path"] == license_files[0]])
        body = lt.get(license_files[0], b"").decode(errors="replace")
        if "Permission is hereby granted, free of charge" in body:
            license_kind = "MIT"
        elif "Apache License" in body and "Version 2.0" in body:
            license_kind = "Apache-2.0"
        else:
            license_kind = "other"

    env_files = [p for p in paths if re.search(r"(^|/)\.env(\.[a-z]+)?$", p, re.I)
                 and not re.search(r"\.(example|sample|template|dist)$", p, re.I)]
    examples = [p for p in paths if p.lower().startswith("examples/")]
    tests = [p for p in paths if TEST_FILE.search(p) and p not in vendored]
    return {
        "file_count": len(files),
        "text_scan_truncated": truncated,
        "vendored_file_count": len(vendored),
        "has_hackathon_md": "HACKATHON.md" in paths,
        "has_readme": any(re.match(r"^readme(\.[a-z]+)?$", p, re.I) for p in paths),
        "has_pr_template": any(PR_TEMPLATE.search(p) for p in paths),
        "license_files": license_files,
        "license_text_kind": license_kind,
        "examples": {"present": bool(examples), "file_count": len(examples), "files": examples[:20]},
        "tests": {"file_count": len(tests), "files": tests[:20]},
        "workflows": wf,
        "ci_runs_tests": any(w["runs_tests"] for w in wf),
        "ci_on_pull_request": any(w["on_pull_request"] for w in wf),
        "loc_by_ext": dict(sorted(loc.items(), key=lambda kv: -kv[1])),
        "loc_total": sum(loc.values()),
        "models": models,
        "injection_text": injections,
        "secrets_in_tree": secrets,
        "env_files": env_files,
    }


def history_facts(mirror, sha, branch, pr_merge_shas):
    """Commit counts, the first commit, and which commits on the default branch skipped a PR."""
    count = int(git(mirror, "rev-list", "--count", sha).stdout.decode().strip() or 0)
    roots = git(mirror, "rev-list", "--max-parents=0", sha).stdout.decode().split()
    root = roots[-1] if roots else None
    first_files = []
    if root:
        first_files = [x for x in git(mirror, "ls-tree", "-r", "--name-only", "-z", root).stdout.decode(errors="replace").split("\0") if x]
    log = git(mirror, "log", "--first-parent", "--format=%H %P", sha).stdout.decode().splitlines()
    direct, via_pr = [], 0
    for line in log:
        parts = line.split()
        c, parents = parts[0], parts[1:]
        if c == root:
            continue
        if len(parents) > 1 or c in pr_merge_shas:
            via_pr += 1
        else:
            direct.append(c[:10])
    authors = git(mirror, "log", "--format=%ae", sha).stdout.decode().split()
    times = git(mirror, "log", "-1", "--format=%cI", sha).stdout.decode().strip()
    return {
        "commit_count": count,
        "root_commits": len(roots),
        "first_commit": root,
        "first_commit_files": first_files[:20],
        "first_commit_only_hackathon_md": first_files == ["HACKATHON.md"],
        "first_commit_has_hackathon_md": "HACKATHON.md" in first_files,
        "default_branch": branch,
        "first_parent_commits_after_init": len(direct) + via_pr,
        "via_pr": via_pr,
        "direct_pushes": len(direct),
        "direct_push_shas": direct[:20],
        "distinct_author_emails": len(set(authors)),
        "head_committed_at": times,
    }


# ---------------------------------------------------------------- one team

def base_facts(team, head, started):
    branch, sha, err, seen_at = head
    return {
        "team_id": team["team_id"],
        "problem": team["problem"],
        "repo_url": team["repo_url"],
        "repo": team["slug"],
        "shared_repo_with": team["shared_repo_with"],
        "url_branch": team["url_branch"],
        "recorded_at": iso(started),
        "status": "ok",
        "reason": None,
        "errors": {},
        "sha": sha,
        "seen_at": seen_at,
    }


def record_team(cfg, team, round_dir, previous, head):
    started = now()
    tid, key = team["team_id"], team["key"]
    mirror = cfg["data_dir"] / "mirror" / f"{key}.git"
    facts = base_facts(team, head, started)
    branch, sha, err, seen_at = head
    prev = previous.get(tid) or {}
    if err:
        facts["status"] = "empty" if err.startswith("empty") else "unreachable"
        facts["reason"] = err
        # Still say what it was the last time we could see it.
        last = prev.get("sha") or prev.get("last_seen_sha")
        if last:
            facts["last_seen_sha"] = last
            facts["last_seen_at"] = prev.get("seen_at") if prev.get("sha") else prev.get("last_seen_at")

    if cfg["github_api"]:
        gh, errs = github_facts(team, sha, round_dir / "api" / key)
        facts["github"] = gh
        facts["errors"].update({f"github:{k}": v for k, v in errs.items()})
        if facts["status"] == "unreachable" and gh.get("repo", {}).get("private"):
            facts["reason"] = "private repo"

    size_kb = facts.get("github", {}).get("repo", {}).get("size_kb") or 0
    if facts["status"] == "ok" and size_kb > cfg["max_clone_mb"] * 1024:
        facts["status"] = "too_large"
        facts["reason"] = f"repo is {size_kb // 1024} MB, over max_clone_mb {cfg['max_clone_mb']}; not cloned"

    if facts["status"] == "ok":
        try:
            facts["mirror"] = sync_mirror(team, mirror, cfg["git_timeout_s"])
            if git(mirror, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode != 0:
                raise RuntimeError(f"{sha[:10]} missing after fetch")
        except Exception as e:  # clone or fetch failed: still record what we know
            facts["status"], facts["reason"] = "fetch_failed", str(e)[:300]

    if facts["status"] == "ok":
        merge_shas = {p["merge_commit_sha"] for p in facts.get("github", {}).get("prs", {}).get("items", []) if p.get("merge_commit_sha")}
        for name, fn in (
            ("history", lambda: history_facts(mirror, sha, branch, merge_shas)),
            ("tree", lambda: tree_facts(cfg, mirror, sha)),
        ):
            try:
                facts[name] = fn()
            except Exception as e:
                facts["errors"][name] = str(e)[:300]
        if facts.get("history", {}).get("commit_count") == 1:
            facts["status"], facts["reason"] = "init_only", "only the init commit"

    # Change since the previous round, and whether history was rewritten under us.
    if prev.get("repo") and prev["repo"].lower() != team["slug"].lower():
        # The team pointed teams.csv at another repo; commits cannot be compared.
        facts["repo_changed_from"] = prev["repo"]
        facts["changed_since_last_round"] = True
    elif prev.get("sha") and sha:
        facts["changed_since_last_round"] = prev["sha"] != sha
        if prev["sha"] != sha and facts["status"] in ("ok", "init_only"):
            anc = git(mirror, "merge-base", "--is-ancestor", prev["sha"], sha, check=False).returncode
            facts["history_rewritten"] = anc != 0
            if anc == 0:
                facts["new_commits"] = int(git(mirror, "rev-list", "--count", f"{prev['sha']}..{sha}").stdout.decode().strip() or 0)

    facts["seconds"] = round((now() - started).total_seconds(), 1)
    out = round_dir / "facts" / f"{key}.json"
    out.write_text(json.dumps(facts, indent=1, sort_keys=False))
    return facts


# ---------------------------------------------------------------- round

SUMMARY_COLUMNS = [
    "team_id", "problem", "repo", "status", "reason", "sha", "changed", "rewritten", "commits",
    "direct_pushes", "prs", "prs_merged", "prs_models_section", "ci", "public", "license",
    "first_commit_hackathon_md", "examples", "test_files", "ci_runs_tests", "loc", "models",
    "secrets_tree", "env_files", "injection_text", "last_seen_sha", "errors",
]


def summary_row(f):
    gh, h, t = f.get("github", {}), f.get("history", {}), f.get("tree", {})
    repo = gh.get("repo", {})
    return {
        "team_id": f["team_id"],
        "problem": f["problem"],
        "repo": f["repo"],
        "status": f["status"],
        "reason": f["reason"] or "",
        "sha": (f.get("sha") or "")[:12],
        "changed": f.get("changed_since_last_round", ""),
        "rewritten": f.get("history_rewritten", ""),
        "commits": h.get("commit_count", ""),
        "direct_pushes": h.get("direct_pushes", ""),
        "prs": gh.get("prs", {}).get("count", ""),
        "prs_merged": gh.get("prs", {}).get("merged", ""),
        "prs_models_section": gh.get("prs", {}).get("with_models_section", ""),
        "ci": gh.get("ci", {}).get("state", ""),
        "public": "" if not repo else (repo.get("visibility") == "public"),
        "license": repo.get("license_spdx") or t.get("license_text_kind") or "",
        "first_commit_hackathon_md": h.get("first_commit_only_hackathon_md", ""),
        "examples": t.get("examples", {}).get("file_count", ""),
        "test_files": t.get("tests", {}).get("file_count", ""),
        "ci_runs_tests": t.get("ci_runs_tests", ""),
        "loc": t.get("loc_total", ""),
        "models": " ".join(sorted(t.get("models", {}).keys())),
        "secrets_tree": len(t.get("secrets_in_tree", [])) if t else "",
        "injection_text": len(t.get("injection_text", [])) if t else "",
        "env_files": " ".join(t.get("env_files", [])) if t else "",
        "last_seen_sha": (f.get("last_seen_sha") or "")[:12],
        "errors": "; ".join(f"{k}: {v}" for k, v in f["errors"].items())[:300],
    }


def write_csv(path, rows, columns):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def load_previous(data_dir):
    latest = data_dir / "latest"
    prev = {}
    if latest.exists():
        for p in (latest / "facts").glob("*.json"):
            try:
                d = json.loads(p.read_text())
                prev[d["team_id"]] = d
            except (ValueError, KeyError):
                pass
    return prev


def check_clock(cfg, final, force):
    t = now()
    if force:
        return
    # Times in config are local wall-clock times; the window spans midnight.
    start = dt.datetime.fromisoformat(cfg["record_from"]).astimezone()
    freeze = dt.datetime.fromisoformat(cfg["freeze_at"]).astimezone()
    if not final:
        # A few minutes' grace: recurring scheduled tasks can fire slightly early or late.
        if t < start - dt.timedelta(minutes=15):
            sys.exit(f"recording starts at {cfg['record_from']}; nothing recorded. Use --force to override.")
        if t >= freeze:
            sys.exit(f"it is past the {cfg['freeze_at']} freeze; only --final may run now.")
    else:
        if t < freeze - dt.timedelta(minutes=1):
            sys.exit(f"--final runs at {cfg['freeze_at']}, not before. Use --force to override.")
        if t > freeze + dt.timedelta(hours=6):
            sys.exit(f"--final is for the {cfg['freeze_at']} freeze, which was more than 6 hours ago. Use --force to override.")


def write_freeze(cfg, teams, heads, round_id):
    """freeze.csv comes straight from the HEAD reads, before any clone or API call, so a
    crash or a hang later in the final round can never leave us without a freeze."""
    path = cfg["data_dir"] / "freeze.csv"
    rows = []
    for t in sorted(teams, key=lambda t: t["team_id"]):
        branch, sha, err, seen_at = heads[t["team_id"]]
        status = "ok" if sha else ("empty" if (err or "").startswith("empty") else "unreachable")
        rows.append({
            "team_id": t["team_id"],
            "problem": t["problem"],
            "repo_url": t["repo_url"],
            "sha": sha or "",
            "default_branch": branch or "",
            "seen_at": seen_at,
            "status": status,
            "reason": err or "",
            "round": round_id,
        })
    tmp = path.with_suffix(".tmp")
    write_csv(tmp, rows, ["team_id", "problem", "repo_url", "sha", "default_branch", "seen_at", "status", "reason", "round"])
    os.chmod(tmp, 0o444)
    os.replace(tmp, path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    (cfg["data_dir"] / "freeze.sha256").write_text(f"{digest}  freeze.csv\n")
    return path, digest


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--final", action="store_true", help="the 6:30 round; also writes data/freeze.csv")
    ap.add_argument("--force", action="store_true", help="ignore the event date and freeze-time checks")
    ap.add_argument("--only", help="comma-separated team_ids, for a quick check")
    args = ap.parse_args()

    cfg = load_config(args.config)
    check_clock(cfg, args.final, args.force)
    data = cfg["data_dir"]
    data.mkdir(parents=True, exist_ok=True)
    if args.final and (data / "freeze.csv").exists():
        sys.exit(f"{data / 'freeze.csv'} already exists; the freeze happens once. Move it aside to redo it.")

    teams, rejected = load_teams(cfg)
    if args.only:
        wanted = set(args.only.split(","))
        teams = [t for t in teams if t["team_id"] in wanted]

    started = now()
    # Every HEAD is read first, all at once and before taking the lock, so a round (above all
    # the freeze) pins each repo within seconds of its start, even if an earlier round is
    # still fetching. ls-remote touches nothing on disk.
    def head(team):
        branch, sha, err = ls_remote(team, 60)
        return team["team_id"], (branch, sha, err, iso(now()))

    with ThreadPoolExecutor(max_workers=32) as pool:
        heads = dict(pool.map(head, teams))
    print(f"  {sum(1 for h in heads.values() if h[1])}/{len(teams)} HEADs read in "
          f"{round((now() - started).total_seconds(), 1)}s", flush=True)
    round_id = ("final-" if args.final else "r-") + started.strftime("%Y%m%dT%H%M%S")
    if args.final:
        freeze_path, freeze_digest = write_freeze(cfg, teams, heads, round_id)
        print(f"FREEZE {freeze_path} sha256={freeze_digest}", flush=True)

    lock = open(data / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | (0 if args.final else fcntl.LOCK_NB))
    except BlockingIOError:
        sys.exit("another round is still running; this one is skipped.")

    round_dir = data / "rounds" / round_id
    (round_dir / "facts").mkdir(parents=True)
    write_csv(round_dir / "teams_rejected.csv", rejected, ["line", "team_id", "repo_url", "reason"])
    previous = load_previous(data)
    print(f"round {round_id}: {len(teams)} teams, {len(rejected)} rejected rows, {cfg['workers']} workers", flush=True)

    results, failures = [], []

    def one(team):
        try:
            return record_team(cfg, team, round_dir, previous, heads[team["team_id"]])
        except Exception as e:  # never let one repo stop the round, or drop it from the freeze
            failures.append({"team_id": team["team_id"], "error": str(e)[:300]})
            f = base_facts(team, heads[team["team_id"]], now())
            f["status"], f["reason"] = "record_failed", str(e)[:300]
            (round_dir / "facts" / f"{team['key']}.json").write_text(json.dumps(f, indent=1))
            return f

    with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
        for i, f in enumerate(pool.map(one, teams), start=1):
            if f:
                results.append(f)
            if i % 25 == 0:
                print(f"  {i}/{len(teams)} recorded", flush=True)

    rows = [summary_row(f) for f in sorted(results, key=lambda f: f["team_id"])]
    write_csv(round_dir / "summary.csv", rows, SUMMARY_COLUMNS)

    hist = data / "history.csv"
    new = not hist.exists()
    with hist.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["round", "recorded_at", "team_id", "status", "sha", "changed", "rewritten", "commits", "prs", "ci"])
        for r, f in zip(rows, sorted(results, key=lambda f: f["team_id"])):
            w.writerow([round_id, f["recorded_at"], r["team_id"], r["status"], f.get("sha") or "", r["changed"], r["rewritten"], r["commits"], r["prs"], r["ci"]])

    counts = {}
    for f in results:
        counts[f["status"]] = counts.get(f["status"], 0) + 1
    finished = now()
    meta = {
        "round": round_id,
        "final": args.final,
        "started_at": iso(started),
        "finished_at": iso(finished),
        "seconds": round((finished - started).total_seconds(), 1),
        "teams": len(teams),
        "rejected_rows": len(rejected),
        "status_counts": counts,
        "changed_since_last_round": sum(1 for f in results if f.get("changed_since_last_round")),
        "history_rewritten": [f["team_id"] for f in results if f.get("history_rewritten")],
        "repo_changed": [f["team_id"] for f in results if f.get("repo_changed_from")],
        "secrets_in_tree": [f["team_id"] for f in results if f.get("tree", {}).get("secrets_in_tree")],
        "injection_text": [f["team_id"] for f in results if f.get("tree", {}).get("injection_text")],
        "env_files": [f["team_id"] for f in results if f.get("tree", {}).get("env_files")],
        "too_large": [f["team_id"] for f in results if f["status"] == "too_large"],
        "failures": failures,
        "config_hash": cfg["_hash"],
        "disk_free_gb": round(shutil.disk_usage(data).free / 1e9, 1),
        "only": args.only,
        "git": run(["git", "--version"]).stdout.decode().strip(),
        "gh": run(["gh", "--version"], check=False).stdout.decode().splitlines()[0] if shutil.which("gh") else None,
    }

    if args.final:
        meta["freeze_csv"], meta["freeze_sha256"] = str(freeze_path), freeze_digest

    (round_dir / "round.json").write_text(json.dumps(meta, indent=1))
    if not args.only:  # a partial round would hide the other teams from the next change check
        latest = data / "latest"
        tmp = data / ".latest.tmp"
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        tmp.symlink_to(Path("rounds") / round_id)
        os.replace(tmp, latest)

    print(json.dumps({k: meta[k] for k in (
        "round", "seconds", "teams", "rejected_rows", "status_counts", "changed_since_last_round",
        "history_rewritten", "repo_changed", "secrets_in_tree", "injection_text", "env_files", "too_large",
        "failures", "disk_free_gb",
    )}, indent=1))
    if meta["disk_free_gb"] < cfg.get("min_free_gb", 5):
        print(f"WARNING: only {meta['disk_free_gb']} GB free on the data disk")
    if args.final:
        print(f"FREEZE {meta['freeze_csv']} sha256={meta['freeze_sha256']} (written at {meta['started_at']}, before the round)")
    print(f"ROUND_DIR={round_dir}")


if __name__ == "__main__":
    main()
