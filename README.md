# wikisearch

Azure Functions API (Python v2) that searches and reads the Sujeeth Foundation GitHub wiki. It returns ranked pages, URLs, and text snippets, and it serves as the keyword-search source for the MCP server (and so for ChatGPT, Claude, and Microsoft Copilot Studio clients).

It works independently of the Azure AI Search index, so wiki answers stay available when website search is unavailable.

## How it works

- On first use and every `CACHE_SECONDS` (default 900), it clones the public wiki repository (`<repo>.wiki.git`) with Dulwich, without calling system Git, and reads the Markdown files from the latest commit. The first request after a cold start pays for the clone unless the worker has already finished the preload or a shared snapshot is configured (see Settings). Searches always wait for wiki results; the snapshot only shortens that wait. The `/health` route reports `loaded_from` (`github` or `snapshot`).
- Pages are indexed in memory per instance. Pages whose title or path matches the exclusion pattern are left out of the index completely (see below).
- Ranking is BM25-style keyword scoring:
  - common words ("how", "can", "the", "to") are ignored;
  - scores are normalized by page length;
  - matches in the title or page name count extra, and pages matching more of the query rank higher;
  - words that belong together match each other ("job" and "jobs"; "donate", "donation" and "donating"), using a standard English stemmer. Words that only look alike do not ("interview", "interest" and "internship"; "program" and "progress"), which the earlier rule of comparing the first five letters merged;
  - a page that has the question's words together, in order and at most four words apart ("tailor my resume", "reach out to a professional"), scores up to 1.5 times as much. Only the words that say what the question is about are paired, never "how do" or "do I".
- There is no semantic or vector search.

## Endpoints

All routes require a Function key and are under `/api`.

| Route | Purpose |
|---|---|
| `GET /health` | Status, whether the cache is loaded, document count |
| `GET /search?q=...&limit=8` or `POST /search` with `{"query": "...", "limit": 8}` | Ranked results (limit 1–20) |
| `GET /document` or `POST /document` | One page as plain text. Provide `url`, `path`, or `title`. Optional `max_chars` (500–30000, default 8000) |
| `POST /refresh` | Reload the wiki now |

Search result fields: `title`, `url`, `source` (`wiki`), `path`, `chunk_id` (the page path), `score`, `snippet`. The response includes `search_mode: keyword_bm25` and `count` (total matches). Scores are decimals that are only meaningful within one response, and they are not comparable with website search scores.

The snippet is the 1,500-character stretch of the page that holds the most of the question (each different word counted once, by rarity, plus the pairs found together), so on a long FAQ page it is the question and answer that match, not the first place a rare word appears.

Add `debug=true` (or `"debug": true`) and each result gets a `why`: per word its rarity and how often it is in the text and the title, how many words matched, the pairs looked for and found together, and where the excerpt starts. The response and `/health` report `ranking`: the stemmer in use, the phrase boost, and whether the best excerpt is chosen.

`/document` returns `title`, `url`, `source`, `chunk_id`, `text`, `total_chars`, and `truncated`. A page that does not exist, or is excluded, returns 404.

## Privacy: excluded pages

Pages about recorded circle sessions (titles like "Spring 2025 Week 4 Circle J") hold recording links and meeting summaries, so they are never searched or returned. The default pattern is:

```
\b(spring|summer|fall|winter)\s+20\d{2}\b.*\bcircle\b
```

It is checked against the page title and file path. Change it with `WIKI_EXCLUDE_TITLE_PATTERN`.

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `GITHUB_OWNER` | `SujeethFoundation` | Repository owner |
| `GITHUB_REPO` | `sujeethfoundation.github.io` | Repository whose wiki is read |
| `GITHUB_TOKEN` | none | Optional token (only needed for rate limits or a private wiki) |
| `CACHE_SECONDS` | `900` | How long the loaded wiki is reused |
| `MAX_FILES` | `250` | Maximum pages loaded |
| `PRELOAD_ON_START` | `true` | Start reading the wiki when the worker starts, not at the first request |
| `WIKI_SNAPSHOT_CONNECTION_STRING` | none (off) | Storage connection string for a shared snapshot of the pages. When set, every read from GitHub is also saved there, and a new instance loads the snapshot first (under a second) and then refreshes from GitHub in the background |
| `WIKI_SNAPSHOT_CONTAINER` | `wiki-cache` | Container for the snapshot, created if it does not exist |
| `WIKI_SNAPSHOT_BLOB` | `wiki-snapshot.json` | Blob name of the snapshot |
| `WIKI_EXCLUDE_TITLE_PATTERN` | see above | Pages never returned |
| `WIKI_STEMMER` | `english` | How words are compared. `prefix` goes back to the first five letters. If the `snowballstemmer` package is missing the app falls back to `prefix` and says so in `/health` |
| `WIKI_PHRASE_BOOST` | `0.5` | How much a page gains for having the question's words together. `0` turns it off |
| `WIKI_BEST_PASSAGE` | `true` | Choose the excerpt that holds the most of the question. `false` starts at the rarest matching word, as before |

## Examples

```powershell
$url = "https://<your-wikisearch-app>.azurewebsites.net"
$headers = @{ "x-functions-key" = "<function key>" }

Invoke-RestMethod "$url/api/health" -Headers $headers
Invoke-RestMethod "$url/api/search?q=how can i donate&limit=3" -Headers $headers
Invoke-RestMethod -Method Post "$url/api/document" -Headers $headers -ContentType "application/json" `
  -Body '{"path":"FAQ-for-Mentors.md","max_chars":3000}'
```

## Local development

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
func start
```

## Measuring relevance

`tests/relevance_gold.json` holds 44 questions written from the wiki's own pages, each with the pages that answer it and, for most, a phrase from the passage that answers it. `tests/relevance_eval.py` scores two things: where the first right page stands, and whether the excerpt shown for a right page in the top three holds the answer.

    python tests/relevance_eval.py --local                      # reads the wiki from GitHub, ranks with this checkout: no deploy needed
    python tests/relevance_eval.py --local --try stemmer=prefix phrase_boost=0 best_passage=false   # a second column with other settings
    python tests/relevance_eval.py --explain                    # the deployed app, with the reasons for each miss

On the wiki as of 12 November 2026 (25 pages):

| | Right page first | In the top 3 | Mean reciprocal rank | Excerpt holds the answer |
|---|---|---|---|---|
| Before (first five letters, excerpt at the rarest word) | 36 of 44 | 42 of 44 | 0.894 | 34 of 39 |
| Now | 39 of 44 | 42 of 44 | 0.926 | 38 of 39 |

Seven questions improved and two got worse. "Where are the weekly materials?" fell from second to sixth, because "weekly" now also matches every "Week N" page, and "How do I negotiate a job offer?" fell from first to second. A bonus for a passage that holds the whole question, and one for a title that has the question's word unchanged, were tried and left out: neither improved these numbers. The questions were written by the person who made the change, so add your own before trusting the numbers.

## Limitations

- Keyword matching only. A query that shares no words with a page will not find it.
- Each instance keeps its own copy of the wiki. A wiki edit can take up to `CACHE_SECONDS` to appear unless you call `POST /refresh`.
- Only pages in the wiki repository are covered. The website is searched by the websearch app.