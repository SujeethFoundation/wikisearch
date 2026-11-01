import json
import logging
import math
import os
import re
import stat
import tempfile
import time
from collections import Counter
from pathlib import Path
from urllib.parse import quote, unquote

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

cache = {"time": 0, "docs": [], "index": None}

# ---------------------------------------------------------------------------
# Ranking: BM25-style keyword scoring.
#   - Stopwords ("how", "can", "the", "to", ...) are ignored, so long pages that
#     simply use common words often no longer win.
#   - Scores are normalized by page length.
#   - Title and page-name matches count extra.
#   - Pages matching more of the query's distinct terms rank higher.
#   - Words are compared by their first five letters, so "donate", "donation",
#     and "donating" match each other.
# ---------------------------------------------------------------------------
STOPWORDS = {
    "a", "about", "am", "an", "and", "any", "are", "as", "at", "be", "been", "by",
    "can", "could", "did", "do", "does", "for", "from", "get", "had", "has", "have",
    "how", "i", "if", "in", "into", "is", "it", "just", "may", "me", "might", "must",
    "my", "not", "of", "on", "or", "our", "should", "so", "than", "that", "the",
    "their", "then", "there", "these", "they", "this", "those", "to", "us", "was",
    "we", "what", "when", "where", "which", "who", "why", "will", "with", "would",
    "you", "your",
    # request verbs that start questions but say nothing about the topic
    "describe", "explain", "please", "show", "tell",
}
BM25_K1 = 1.2
BM25_B = 0.75
TITLE_BOOST = 3.0

# Pages about recorded circle sessions are never returned. Override the pattern with
# WIKI_EXCLUDE_TITLE_PATTERN in the app settings.
WIKI_EXCLUDE = re.compile(
    os.getenv(
        "WIKI_EXCLUDE_TITLE_PATTERN",
        r"\b(spring|summer|fall|winter)\s+20\d{2}\b.*\bcircle\b",
    ),
    re.IGNORECASE,
)


def stem(token):
    return token[:5] if len(token) >= 6 else token


def stems(text):
    return [stem(t) for t in re.findall(r"[a-z0-9]+", str(text).lower())]


def is_excluded(doc):
    label = f"{doc['title']} {doc['path']}".replace("-", " ").replace("%3A", " ")
    return bool(WIKI_EXCLUDE.search(label))


def build_index(docs):
    """Precompute term counts for every page. Rebuilt whenever the wiki is reloaded."""
    entries, df = [], Counter()
    for doc in docs:
        if is_excluded(doc):
            continue
        body = plain(doc["text"])
        body_stems = stems(body)
        body_tf = Counter(body_stems)
        title_tf = Counter(stems(doc["title"] + " " + doc["path"].rsplit(".", 1)[0]))
        for term in set(body_tf) | set(title_tf):
            df[term] += 1
        entries.append({
            "doc": doc,
            "body": body,
            "body_lower": body.lower(),
            "body_tf": body_tf,
            "title_tf": title_tf,
            "length": max(1, len(body_stems)),
        })
    average = sum(e["length"] for e in entries) / len(entries) if entries else 1.0
    return {"entries": entries, "df": df, "average_length": average}


def query_stems(query):
    raw = re.findall(r"[a-z0-9]+", query.lower())
    # Keep single digits: "week 1" and "week 4" are different questions.
    kept = [t for t in raw if t not in STOPWORDS and (len(t) > 1 or t.isdigit())]
    chosen = kept or [t for t in raw if len(t) > 1 or t.isdigit()]
    return list(dict.fromkeys(stem(t) for t in chosen))


def rank(query):
    """Return every matching page, best first."""
    index = cache.get("index") or build_index(cache["docs"])
    terms = query_stems(query)
    if not terms or not index["entries"]:
        return []
    n = len(index["entries"])
    idf = {
        t: math.log(1 + (n - index["df"].get(t, 0) + 0.5) / (index["df"].get(t, 0) + 0.5))
        for t in terms
    }
    results = []
    for entry in index["entries"]:
        score, matched = 0.0, []
        for t in terms:
            weighted = entry["body_tf"].get(t, 0) + TITLE_BOOST * entry["title_tf"].get(t, 0)
            if not weighted:
                continue
            matched.append(t)
            norm = 1 - BM25_B + BM25_B * entry["length"] / index["average_length"]
            score += idf[t] * weighted * (BM25_K1 + 1) / (weighted + BM25_K1 * norm)
        if not matched:
            continue
        score *= 0.5 + 0.5 * (len(matched) / len(terms))
        # A page whose title or page name matches the query is probably the target page.
        title_hits = sum(1 for t in terms if entry["title_tf"].get(t))
        score *= 1 + 1.5 * (title_hits / len(terms))

        # Start the snippet at the rarest matched term that appears in the text.
        start = 0
        for t in sorted(matched, key=lambda x: idf[x], reverse=True):
            hit = re.search(r"\b" + re.escape(t), entry["body_lower"])
            if hit:
                start = max(0, hit.start() - 200)
                break
        doc = entry["doc"]
        results.append({
            "title": doc["title"],
            "url": doc["url"],
            "source": doc["source"],
            "path": doc["path"],
            "chunk_id": doc["path"],
            "score": round(score, 3),
            "snippet": entry["body"][start:start + 1500],
        })
    results.sort(key=lambda r: (-r["score"], r["title"]))
    return results



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

    cache.update({"time": time.time(), "docs": docs, "index": build_index(docs)})
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

        documents()  # load or refresh the wiki and its ranking index
        results = rank(query)

        return respond(
            {
                "query": query,
                "search_mode": "keyword_bm25",
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


@app.route(route="document", methods=["GET", "POST"])
def document(req: func.HttpRequest):
    """Return one wiki page as plain text, found by url, path, or title."""
    try:
        try:
            data = req.get_json() if req.method == "POST" else dict(req.params)
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}

        wanted_url = unquote(str(data.get("url") or "")).strip().rstrip("/").lower()
        wanted_path = str(data.get("path") or "").strip().lower()
        wanted_title = str(data.get("title") or "").strip().lower()
        if not (wanted_url or wanted_path or wanted_title):
            return respond({"error": "Provide url, path, or title"}, 400)
        max_chars = max(500, min(int(data.get("max_chars", 8000)), 30000))

        documents()  # load or refresh the wiki and its index
        match = None
        # Excluded pages are not in the index, so they can never be returned here.
        for entry in cache["index"]["entries"]:
            doc = entry["doc"]
            if (
                (wanted_url and unquote(doc["url"]).rstrip("/").lower() == wanted_url)
                or (wanted_path and doc["path"].lower() == wanted_path)
                or (wanted_title and doc["title"].lower() == wanted_title)
            ):
                match = entry
                break
        if not match:
            return respond({"error": "Wiki page not found"}, 404)

        text = match["body"]
        doc = match["doc"]
        return respond({
            "status": "ok",
            "title": doc["title"],
            "url": doc["url"],
            "source": "wiki",
            "chunk_id": doc["path"],
            "total_chars": len(text),
            "truncated": len(text) > max_chars,
            "text": text[:max_chars],
        })

    except ValueError:
        return respond({"error": "max_chars must be a number"}, 400)
    except requests.RequestException:
        logging.exception("GitHub request failed")
        return respond({"error": "Could not load content from GitHub"}, 502)
    except Exception:
        logging.exception("Document lookup failed")
        return respond({"error": "Unexpected server error"}, 500)


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