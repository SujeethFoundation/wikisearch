import json
import logging
import math
import os
import re
import stat
import tempfile
import threading
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

cache = {"time": 0, "docs": [], "index": None, "source": None}

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

# Words that belong together match each other ("job" and "jobs", "donate", "donation" and "donating"), and words that only
# look alike do not ("interview", "interest" and "internship"; "program" and "progress"). WIKI_STEMMER=prefix goes back to
# comparing the first five letters, which merged those and kept "job" and "jobs" apart.
STEMMER = os.getenv("WIKI_STEMMER", "english").strip().lower()
# Keyword scoring ignores word order. A page that has the question's words together, in order and at most a few words apart
# ("tailor my resume", "reach out to a professional"), scores up to 1 + WIKI_PHRASE_BOOST times as much. Only the words that
# say what the question is about are paired (not "how do" or "do I"), and a pair weighs what its commoner word weighs.
# 0 turns it off.
PHRASE_BOOST = float(os.getenv("WIKI_PHRASE_BOOST", "0.5"))
# The excerpt shown for a page is the stretch of it that holds the most of the question (true), or the stretch starting at the
# rarest matched word (false, the old behaviour).
BEST_PASSAGE = os.getenv("WIKI_BEST_PASSAGE", "true").strip().lower() in ("1", "true", "yes", "on")
SNIPPET_CHARS = 1500
SNIPPET_LEAD = 200
PAIR_WINDOW = 4  # two of the question's words count as together when at most this many words apart, in order

try:
    import snowballstemmer
    _english = snowballstemmer.stemmer("english")
except Exception:  # the package is missing: search still works, with the old rule
    _english = None
    logging.warning("snowballstemmer is not installed; comparing words by their first five letters")
_stem_memo = {}


def stemmer_in_use():
    return "english" if STEMMER != "prefix" and _english is not None else "prefix"

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
    if stemmer_in_use() == "prefix":
        return token[:5] if len(token) >= 6 else token
    found = _stem_memo.get(token)
    if found is None:
        found = token if token.isdigit() else _english.stemWord(token)
        if len(_stem_memo) < 200000:
            _stem_memo[token] = found
    return found


def stems(text):
    return [stem(t) for t in re.findall(r"[a-z0-9]+", str(text).lower())]


def stem_positions(lowered):
    """[(where the word starts, its stem), ...] for a lower-case text."""
    return [(m.start(), stem(m.group())) for m in re.finditer(r"[a-z0-9]+", lowered)]


def is_excluded(doc):
    label = f"{doc['title']} {doc['path']}".replace("-", " ").replace("%3A", " ")
    return bool(WIKI_EXCLUDE.search(label))


def _places(stem_list):
    """{stem: [its places in the page, in order]}"""
    where = {}
    for i, s in enumerate(stem_list):
        where.setdefault(s, []).append(i)
    return where


def build_index(docs):
    """Precompute term counts for every page. Rebuilt whenever the wiki is reloaded."""
    entries, df = [], Counter()
    for doc in docs:
        if is_excluded(doc):
            continue
        body = plain(doc["text"])
        positions = stem_positions(body.lower())
        body_stems = [s for _, s in positions]
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
            "positions": positions,
            "where": _places(body_stems),
            "length": max(1, len(body_stems)),
        })
    average = sum(e["length"] for e in entries) / len(entries) if entries else 1.0
    return {"entries": entries, "df": df, "average_length": average}


def query_words(query):
    """The question's words in lower case, in order."""
    return re.findall(r"[a-z0-9]+", str(query).lower())


def query_stems(query):
    raw = query_words(query)
    # Keep single digits: "week 1" and "week 4" are different questions.
    kept = [t for t in raw if t not in STOPWORDS and (len(t) > 1 or t.isdigit())]
    chosen = kept or [t for t in raw if len(t) > 1 or t.isdigit()]
    return list(dict.fromkeys(stem(t) for t in chosen))


def content_words(query):
    """The question's words that say what it is about, in order, as stems: no stop words, no single letters."""
    return [stem(t) for t in query_words(query) if t not in STOPWORDS and (len(t) > 1 or t.isdigit())]


def query_pairs(query, index):
    """{(stem, next stem): weight} for each two words of the question that follow each other once the stop words are taken
    out: "reach out to a professional" gives (reach, out) and (out, professional). A pair weighs what its commoner word
    weighs. Pairs with a word no page has are left out: nothing can hold them."""
    n = len(index["entries"])
    df = index["df"]
    idf = lambda t: math.log(1 + (n - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5))
    words = content_words(query)
    pairs = {}
    for a, b in zip(words, words[1:]):
        if a != b and df.get(a) and df.get(b):
            pairs[(a, b)] = min(idf(a), idf(b))
    return pairs


def pair_places(entry, pair):
    """Where in the page (as places in entry["positions"]) the two words of a pair come in order, at most PAIR_WINDOW words
    apart, so "tailor my resume" and "reach out to a professional" count."""
    first, second = entry["where"].get(pair[0]), entry["where"].get(pair[1])
    if not first or not second:
        return []
    found, j = [], 0
    for i in first:
        while j < len(second) and second[j] <= i:
            j += 1
        if j < len(second) and second[j] - i <= PAIR_WINDOW:
            found.append(i)
    return found


def best_passage(entry, idf, pairs):
    """(where the best excerpt starts, the share of the question's words it holds, by rarity).

    The best excerpt is the SNIPPET_CHARS stretch, starting SNIPPET_LEAD before one of the question's words, that holds the
    most of the question: each different word counts once, by rarity, and so does each pair of neighbouring words."""
    total = sum(idf.values())
    positions = entry["positions"]
    marks = []  # (where, what, weight, counts as a word)
    for s, weight in idf.items():
        for i in entry["where"].get(s, ()):
            marks.append((positions[i][0], s, weight, True))
    for pair, weight in (pairs or {}).items():
        for i in pair_places(entry, pair):
            marks.append((positions[i][0], pair, weight, False))
    if not marks or total <= 0:
        return 0, 0.0
    marks.sort(key=lambda m: m[0])
    best = (-1.0, 0, 0.0)  # (score, start, word share)
    counts, score, words, left, right = Counter(), 0.0, 0.0, 0, 0
    for where, what, weight, is_word in marks:
        if not is_word:
            continue  # an excerpt starts just before one of the question's words
        start = max(0, where - SNIPPET_LEAD)
        stop = start + SNIPPET_CHARS
        while right < len(marks) and marks[right][0] < stop:
            _, k, w, word = marks[right]
            if counts[k] == 0:
                score += w
                words += w if word else 0.0
            counts[k] += 1
            right += 1
        while left < right and marks[left][0] < start:
            _, k, w, word = marks[left]
            counts[k] -= 1
            if counts[k] == 0:
                score -= w
                words -= w if word else 0.0
            left += 1
        if score > best[0] + 1e-9:
            best = (score, start, words / total)
    return best[1], max(0.0, min(1.0, best[2]))


def rank(query, debug=False):
    """Return every matching page, best first. debug=True adds to each result how its score came about."""
    index = cache.get("index") or build_index(cache["docs"])
    terms = query_stems(query)
    if not terms or not index["entries"]:
        return []
    n = len(index["entries"])
    idf = {
        t: math.log(1 + (n - index["df"].get(t, 0) + 0.5) / (index["df"].get(t, 0) + 0.5))
        for t in terms
    }
    pairs = query_pairs(query, index) if PHRASE_BOOST > 0 or BEST_PASSAGE else {}
    pair_total = sum(pairs.values())
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
        keyword_score = score
        score *= 0.5 + 0.5 * (len(matched) / len(terms))
        # A page whose title or page name matches the query is probably the target page.
        title_hits = sum(1 for t in terms if entry["title_tf"].get(t))
        score *= 1 + 1.5 * (title_hits / len(terms))
        # The question's neighbouring words found side by side.
        together = [pair for pair in pairs if pair_places(entry, pair)]
        phrase_share = (sum(pairs[pair] for pair in together) / pair_total) if pair_total > 0 else 0.0
        if PHRASE_BOOST > 0:
            score *= 1 + PHRASE_BOOST * phrase_share
        # The stretch of the page that holds the most of the question: that is the excerpt shown.
        passage_start, passage_share = best_passage(entry, idf, pairs)

        if BEST_PASSAGE:
            start = passage_start
        else:
            # Start the snippet at the rarest matched term that appears in the text.
            start = 0
            for t in sorted(matched, key=lambda x: idf[x], reverse=True):
                where = next((p for p, s in entry["positions"] if s == t), None)
                if where is not None:
                    start = max(0, where - SNIPPET_LEAD)
                    break
        doc = entry["doc"]
        result = {
            "title": doc["title"],
            "url": doc["url"],
            "source": doc["source"],
            "path": doc["path"],
            "chunk_id": doc["path"],
            "score": round(score, 3),
            "snippet": entry["body"][start:start + SNIPPET_CHARS],
        }
        if debug:
            result["why"] = {
                "words": {t: {"rarity": round(idf[t], 2), "in_text": entry["body_tf"].get(t, 0), "in_title": entry["title_tf"].get(t, 0)}
                          for t in terms},
                "keyword_score": round(keyword_score, 3),
                "words_matched": f"{len(matched)} of {len(terms)}",
                "title_share": round(title_hits / len(terms), 2),
                "pairs_together": [" ".join(pair) for pair in together],
                "pairs_looked_for": [" ".join(pair) for pair in pairs],
                "phrase_share": round(phrase_share, 2),
                "passage_share": round(passage_share, 2),
                "excerpt_starts_at": start,
            }
        results.append(result)
    results.sort(key=lambda r: (-r["score"], r["title"]))
    return results


def ranking_settings():
    return {"stemmer": stemmer_in_use(), "phrase_boost": PHRASE_BOOST, "best_passage": BEST_PASSAGE}



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


_refresh_lock = threading.Lock()
_last_failure = {"time": 0.0, "error": None}

# ---------------------------------------------------------------------------
# Cold starts. Reading the wiki from GitHub takes about 20 seconds, and a new instance starts with
# nothing in memory, so without help the first request on it (and every request that arrives while it
# waits) pays for the whole read. Two things reduce that. Neither makes the wiki optional: a search
# still waits for wiki results, it just waits much less.
#
# PRELOAD_ON_START (default true): start reading as soon as the worker starts, not at the first request.
#
# A shared snapshot (optional, off unless WIKI_SNAPSHOT_CONNECTION_STRING is set): after every
# successful read from GitHub the pages are saved to one blob. A new instance loads that blob in a
# fraction of a second, and the usual background refresh brings it up to date from GitHub afterwards.
# If the snapshot is missing, unreadable or invalid the instance reads GitHub as before.
# ---------------------------------------------------------------------------
PRELOAD_ON_START = os.getenv("PRELOAD_ON_START", "true").strip().lower() in ("1", "true", "yes", "on")
SNAPSHOT_CONNECTION = os.getenv("WIKI_SNAPSHOT_CONNECTION_STRING", "")
SNAPSHOT_CONTAINER = os.getenv("WIKI_SNAPSHOT_CONTAINER", "wiki-cache")
SNAPSHOT_BLOB = os.getenv("WIKI_SNAPSHOT_BLOB", "wiki-snapshot.json")
SNAPSHOT_TIMEOUT_SECONDS = 15
_DOC_FIELDS = ("title", "url", "source", "path", "text")


def _snapshot_enabled():
    return bool(SNAPSHOT_CONNECTION)


def _valid_docs(docs):
    return (
        isinstance(docs, list)
        and len(docs) > 0
        and all(isinstance(d, dict) and all(isinstance(d.get(f), str) for f in _DOC_FIELDS) for d in docs)
    )


def _snapshot_load():
    """(docs, saved_at) from the shared snapshot, or None. Never raises."""
    if not _snapshot_enabled():
        return None
    try:
        from azure.storage.blob import BlobClient  # imported here so the app runs without the package when unused

        client = BlobClient.from_connection_string(SNAPSHOT_CONNECTION, SNAPSHOT_CONTAINER, SNAPSHOT_BLOB)
        data = json.loads(client.download_blob(timeout=SNAPSHOT_TIMEOUT_SECONDS).readall())
        docs, saved_at = data.get("docs"), float(data.get("saved_at"))
        if not _valid_docs(docs):
            logging.warning("Wiki snapshot is not valid; reading GitHub instead")
            return None
        return docs, saved_at
    except Exception as exc:
        logging.warning("Wiki snapshot could not be read (%s); reading GitHub instead", type(exc).__name__)
        return None


def _snapshot_save(docs, saved_at):
    """Write the pages to the shared snapshot. Never raises."""
    if not _snapshot_enabled():
        return
    try:
        from azure.core.exceptions import ResourceExistsError
        from azure.storage.blob import ContainerClient

        container = ContainerClient.from_connection_string(SNAPSHOT_CONNECTION, SNAPSHOT_CONTAINER)
        try:
            container.create_container()
        except ResourceExistsError:
            pass
        payload = json.dumps({"saved_at": saved_at, "docs": docs}, ensure_ascii=False)
        container.upload_blob(SNAPSHOT_BLOB, payload.encode("utf-8"), overwrite=True,
                              timeout=SNAPSHOT_TIMEOUT_SECONDS)
        logging.info("Saved %d wiki pages to the shared snapshot", len(docs))
    except Exception as exc:
        logging.warning("Wiki snapshot could not be saved (%s)", type(exc).__name__)


def _rebuild():
    """Read the wiki and replace the cached copy. Callers hold _refresh_lock."""
    docs = read_wiki()

    if not docs:
        raise RuntimeError(
            "No wiki pages loaded. Check that the GitHub wiki exists "
            "and is accessible."
        )

    saved_at = time.time()
    cache.update({"time": saved_at, "docs": docs, "index": build_index(docs), "source": "github"})
    if _snapshot_enabled():
        threading.Thread(target=_snapshot_save, args=(docs, saved_at), name="wiki-snapshot", daemon=True).start()
    return docs


def _refresh_in_background():
    """Start one background refresh if none is running. Never blocks."""
    if not _refresh_lock.acquire(blocking=False):
        return  # another request already started one

    def work():
        try:
            _rebuild()
        except Exception:
            logging.exception("Background wiki refresh failed; the older copy stays in use")
        finally:
            _refresh_lock.release()

    threading.Thread(target=work, name="wiki-refresh", daemon=True).start()


def documents(force=False):
    """Return cached wiki documents, refreshing when necessary.

    Reading the wiki takes about 20 seconds, so it is done once at a time. With a copy in
    memory, a request is never held up by a refresh: a stale copy is served while one
    background refresh runs. Only a request that finds nothing in memory waits (and only for
    one read, which every waiting request shares), and so does a forced refresh.
    """
    fresh = bool(cache["docs"]) and time.time() - cache["time"] < CACHE_SECONDS
    if fresh and not force:
        return cache["docs"]

    if cache["docs"] and not force:
        _refresh_in_background()
        return cache["docs"]

    with _refresh_lock:
        # Another request may have finished a read while this one waited for the lock.
        if not force and cache["docs"] and time.time() - cache["time"] < CACHE_SECONDS:
            return cache["docs"]
        # If that read just failed, say so now instead of making every waiting request repeat it.
        if not force and not cache["docs"]:
            loaded = _snapshot_load()
            if loaded:
                docs, saved_at = loaded
                cache.update({"time": saved_at, "docs": docs, "index": build_index(docs), "source": "snapshot"})
                logging.info("Loaded %d wiki pages from the shared snapshot (%d seconds old)",
                             len(docs), time.time() - saved_at)
                # If the snapshot is older than CACHE_SECONDS, the next request starts the usual background refresh.
                return docs
        if not force and _last_failure["error"] and time.time() - _last_failure["time"] < 15:
            raise _last_failure["error"]
        try:
            return _rebuild()
        except Exception as exc:
            _last_failure.update(time=time.time(), error=exc)
            raise


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
            "loaded_from": cache.get("source"),
            "snapshot_enabled": _snapshot_enabled(),
            "ranking": ranking_settings(),
        }
    )


def parse_flag(value, default):
    """Read a true/false setting from a query string or JSON body."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def attach_full_text(results, max_chars):
    """Add each result's whole page as text, cut at max_chars.

    Only the results being returned are looked up. Excluded pages are not in the index,
    so their text can never be returned here.
    """
    entries = {
        entry["doc"]["path"]: entry
        for entry in (cache.get("index") or {}).get("entries", [])
    }
    for item in results:
        entry = entries.get(item.get("path"))
        if not entry:
            continue
        body = entry["body"]
        item["text"] = body[:max_chars]
        item["total_chars"] = len(body)
        item["truncated"] = len(body) > max_chars


@app.route(route="search", methods=["GET", "POST"])
def search(req: func.HttpRequest):
    try:
        if req.method == "POST":
            body = req.get_json()
            query = str(body.get("query", "")).strip()
            limit = int(body.get("limit", 8))
            full_text = parse_flag(body.get("full_text"), True)
            max_chars = int(body.get("max_chars", 8000))
            debug = parse_flag(body.get("debug"), False)
        else:
            query = (
                req.params.get("q")
                or req.params.get("query")
                or ""
            ).strip()
            limit = int(req.params.get("limit", 8))
            full_text = parse_flag(req.params.get("full_text"), True)
            max_chars = int(req.params.get("max_chars", 8000))
            debug = parse_flag(req.params.get("debug"), False)

        if not query:
            return respond(
                {"error": "Provide ?q=... or JSON {query: ...}"},
                400,
            )

        if len(query) > 500:
            return respond({"error": "Query too long"}, 400)

        documents()  # load or refresh the wiki and its ranking index
        # debug=true adds to each result how its score came about ("why"): the words, the title, the pairs found together.
        results = rank(query, debug=debug)
        shown = results[: max(1, min(limit, 20))]
        if full_text:
            attach_full_text(shown, max(500, min(max_chars, 30000)))

        return respond(
            {
                "query": query,
                "search_mode": "keyword_bm25",
                "ranking": ranking_settings(),
                "full_text": full_text,
                "count": len(results),
                "results": shown,
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


def _preload():
    """Read the wiki now, in the background, so a new instance does not wait for its first request to start."""
    try:
        documents()
    except Exception:
        logging.warning("Wiki preload failed; the first request will try again", exc_info=True)


if PRELOAD_ON_START:
    threading.Thread(target=_preload, name="wiki-preload", daemon=True).start()
