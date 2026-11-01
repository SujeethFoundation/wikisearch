# wikisearch

Azure Functions API (Python v2) that searches and reads the Sujeeth Foundation GitHub wiki. It returns ranked pages, URLs, and text snippets, and it serves as the keyword-search source for the MCP server (and so for ChatGPT, Claude, and Microsoft Copilot Studio clients).

It works independently of the Azure AI Search index, so wiki answers stay available when website search is unavailable.

## How it works

- On first use and every `CACHE_SECONDS` (default 900), it clones the public wiki repository (`<repo>.wiki.git`) with Dulwich, without calling system Git, and reads the Markdown files from the latest commit. The first request after a cold start pays for the clone.
- Pages are indexed in memory per instance. Pages whose title or path matches the exclusion pattern are left out of the index completely (see below).
- Ranking is BM25-style keyword scoring:
  - common words ("how", "can", "the", "to") are ignored;
  - scores are normalized by page length;
  - matches in the title or page name count extra, and pages matching more of the query rank higher;
  - words are compared by their first five letters, so "donate", "donation", and "donating" match each other. This can also merge unrelated words with the same first five letters.
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

Snippets start about 200 characters before the rarest matching word.

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
| `WIKI_EXCLUDE_TITLE_PATTERN` | see above | Pages never returned |

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

## Limitations

- Keyword matching only. A query that shares no words with a page will not find it.
- Each instance keeps its own copy of the wiki. A wiki edit can take up to `CACHE_SECONDS` to appear unless you call `POST /refresh`.
- Only pages in the wiki repository are covered. The website is searched by the websearch app.