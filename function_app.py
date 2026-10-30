import json
import logging
import os
import re
import stat
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import azure.functions as func
from dulwich import porcelain
from dulwich.repo import Repo
import requests


app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

OWNER = os.getenv("GITHUB_OWNER", "SujeethFoundation")
REPO = os.getenv("GITHUB_REPO", "sujeethfoundation.github.io")
TOKEN = os.getenv("GITHUB_TOKEN", "")
CACHE_SECONDS = int(os.getenv("CACHE_SECONDS", "900"))
MAX_FILES = int(os.getenv("MAX_FILES", "250"))

session = requests.Session()
session.headers["User-Agent"] = "SujeethFoundation-ContentSearch/0.1"

if TOKEN:
    session.headers["Authorization"] = f"Bearer {TOKEN}"

cache = {"time": 0, "docs": []}


def get(url, params=None):
    """Make an HTTP GET request and raise on HTTP errors."""
    response = session.get(url, params=params, timeout=20)
    response.raise_for_status()
    return response


def title_of(text, fallback):
    """Extract the first Markdown H1 heading, or derive a title from a filename."""
    for line in text.splitlines()[:30]:
        match = re.match(r"^\s*#\s+(.+?)\s*#*\s*$", line)
        if match:
            return match.group(1).strip()

    return fallback.replace("-", " ").replace("_", " ").title()


def read_repo():
    """
    Read Markdown files from the ordinary GitHub repository.

    Retained for compatibility with the original implementation.
    The search endpoint continues to search wiki pages by default.
    """
    base = f"https://api.github.com/repos/{OWNER}/{REPO}"
    info = get(base).json()
    branch = info.get("default_branch", "main")

    tree_response = get(
        f"{base}/git/trees/{quote(branch, safe='')}",
        {"recursive": "1"},
    ).json()

    docs = []

    for item in tree_response.get("tree", []):
        path = item.get("path", "")

        if item.get("type") != "blob":
            continue

        if not path.lower().endswith((".md", ".markdown")):
            continue

        if len(docs) >= MAX_FILES:
            break

        raw_url = (
            f"https://raw.githubusercontent.com/{OWNER}/{REPO}/"
            f"{quote(branch, safe='')}/{quote(path, safe='/')}"
        )

        try:
            text = get(raw_url).text[:250000]

            docs.append(
                {
                    "title": title_of(text, Path(path).stem),
                    "url": (
                        f"https://github.com/{OWNER}/{REPO}/"
                        f"blob/{branch}/{path}"
                    ),
                    "source": "repository",
                    "path": path,
                    "text": text,
                }
            )

        except requests.RequestException as exc:
            logging.warning("Skip repository file %s: %s", path, exc)

    return docs


def walk_markdown(repo, tree_id, prefix=""):
    """
    Recursively enumerate Markdown blobs in a Dulwich Git tree.

    Yields (path, content) pairs without checking files out to disk.
    """
    tree = repo[tree_id]

    for name, mode, object_id in tree.iteritems():
        name_text = name.decode("utf-8", errors="replace")
        path = f"{prefix}/{name_text}" if prefix else name_text

        if stat.S_ISDIR(mode):
            yield from walk_markdown(repo, object_id, path)
            continue

        if not stat.S_ISREG(mode):
            continue

        if not path.lower().endswith((".md", ".markdown")):
            continue

        blob = repo[object_id]
        content = blob.data.decode("utf-8", errors="replace")

        yield path, content


def read_wiki():
    """
    Clone the public GitHub wiki using Dulwich, without calling system Git.

    Reads Markdown blobs directly from the latest commit tree.
    Does not check files out, avoiding Windows-invalid filename issues.
    """
    docs = []
    wiki_url = f"https://github.com/{OWNER}/{REPO}.wiki.git"

    with tempfile.TemporaryDirectory(prefix="sujeeth-wiki-") as tmp:
        repo_dir = Path(tmp) / "wiki.git"

        try:
            porcelain.clone(
                wiki_url,
                str(repo_dir),
                bare=True,
                depth=1,
            )

            wiki_repo = Repo(str(repo_dir))

            try:
                head = wiki_repo.head()
                commit = wiki_repo[head]
                count = 0

                for path, content in walk_markdown(
                    wiki_repo,
                    commit.tree,
                ):
                    if count >= MAX_FILES:
                        break

                    page_name = Path(path).stem

                    docs.append(
                        {
                            "title": title_of(content, page_name),
                            "url": (
                                f"https://github.com/{OWNER}/{REPO}/wiki/"
                                f"{quote(page_name, safe='')}"
                            ),
                            "source": "wiki",
                            "path": path,
                            "text": content[:250000],
                        }
                    )

                    count += 1

            finally:
                wiki_repo.close()

        except Exception:
            logging.exception("Dulwich wiki clone or read failed")
            raise RuntimeError(
                "Could not download or read the GitHub wiki."
            ) from None

    logging.info("Loaded %d wiki pages", len(docs))
    return docs


def documents(force=False):
    """Return cached wiki documents, refreshing when necessary."""
    if (
        cache["docs"]
        and not force
        and time.time() - cache["time"] < CACHE_SECONDS
    ):
        return cache["docs"]

    docs = read_wiki()

    if not docs:
        raise RuntimeError(
            "No wiki pages loaded. Check that the GitHub wiki exists "
            "and is accessible."
        )

    cache.update({"time": time.time(), "docs": docs})
    return docs


def plain(text):
    """Convert Markdown content into plain text for search snippets."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 \2", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[#*_>|]", " ", text)

    return re.sub(r"\s+", " ", text).strip()


def respond(data, status=200):
    """Return a JSON HTTP response."""
    return func.HttpResponse(
        json.dumps(data, ensure_ascii=False),
        status_code=status,
        mimetype="application/json",
    )


@app.route(route="health", methods=["GET"])
def health(req: func.HttpRequest):
    return respond(
        {
            "status": "ok",
            "repository": f"{OWNER}/{REPO}",
            "cache_loaded": bool(cache["docs"]),
            "document_count": len(cache["docs"]),
        }
    )


@app.route(route="search", methods=["GET", "POST"])
def search(req: func.HttpRequest):
    try:
        if req.method == "POST":
            body = req.get_json()
            query = str(body.get("query", "")).strip()
            limit = int(body.get("limit", 8))
        else:
            query = (
                req.params.get("q")
                or req.params.get("query")
                or ""
            ).strip()
            limit = int(req.params.get("limit", 8))

        if not query:
            return respond(
                {"error": "Provide ?q=... or JSON {query: ...}"},
                400,
            )

        if len(query) > 500:
            return respond({"error": "Query too long"}, 400)

        terms = [
            term.lower()
            for term in re.findall(
                r"[a-zA-Z0-9][a-zA-Z0-9'-]*",
                query,
            )
            if len(term) > 1
        ]

        results = []

        for doc in documents():
            haystack = (
                doc["title"] + " " + doc["path"] + " " + doc["text"]
            ).lower()

            # Preserve the original scoring algorithm.
            score = sum(haystack.count(term) for term in terms)

            if not score:
                continue

            body = plain(doc["text"])
            body_lower = body.lower()

            positions = [
                body_lower.find(term)
                for term in terms
                if body_lower.find(term) >= 0
            ]

            start = max(
                0,
                (min(positions) if positions else 0) - 160,
            )

            results.append(
                {
                    "title": doc["title"],
                    "url": doc["url"],
                    "source": doc["source"],
                    "path": doc["path"],
                    "score": score,
                    "snippet": body[start : start + 1500],
                }
            )

        results.sort(
            key=lambda result: result["score"],
            reverse=True,
        )

        return respond(
            {
                "query": query,
                "count": len(results),
                "results": results[: max(1, min(limit, 20))],
            }
        )

    except requests.RequestException:
        logging.exception("GitHub request failed")
        return respond(
            {"error": "Could not load content from GitHub"},
            502,
        )

    except Exception:
        logging.exception("Search failed")
        return respond(
            {"error": "Unexpected server error"},
            500,
        )


@app.route(route="refresh", methods=["POST"])
def refresh(req: func.HttpRequest):
    try:
        docs = documents(force=True)

        return respond(
            {
                "status": "refreshed",
                "document_count": len(docs),
            }
        )

    except Exception:
        logging.exception("Refresh failed")

        return respond(
            {"error": "Could not refresh content"},
            502,
        )