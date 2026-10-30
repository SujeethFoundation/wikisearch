import json, logging, os, re, subprocess, tempfile, time
from pathlib import Path
from urllib.parse import quote
import azure.functions as func
import requests

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)
OWNER = os.getenv("GITHUB_OWNER", "SujeethFoundation")
REPO = os.getenv("GITHUB_REPO", "sujeethfoundation.github.io")
TOKEN = os.getenv("GITHUB_TOKEN", "")
CACHE_SECONDS = int(os.getenv("CACHE_SECONDS", "900"))
MAX_FILES = int(os.getenv("MAX_FILES", "250"))
session = requests.Session()
session.headers["User-Agent"] = "SujeethFoundation-ContentSearch/0.1"
if TOKEN: session.headers["Authorization"] = f"Bearer {TOKEN}"
cache = {"time": 0, "docs": []}

def get(url, params=None):
    r = session.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r

def title_of(text, fallback):
    for line in text.splitlines()[:30]:
        m = re.match(r"^\\s*#\\s+(.+?)\\s*#*\\s*$", line)
        if m: return m.group(1).strip()
    return fallback.replace("-", " ").replace("_", " ").title()

def read_repo():
    base = f"https://api.github.com/repos/{OWNER}/{REPO}"
    info = get(base).json()
    branch = info.get("default_branch", "main")
    tree = get(f"{base}/git/trees/{quote(branch, safe='')}", {"recursive":"1"}).json()
    docs = []
    for item in tree.get("tree", []):
        path = item.get("path", "")
        if item.get("type") != "blob" or not path.lower().endswith((".md", ".markdown")): continue
        if len(docs) >= MAX_FILES: break
        raw = f"https://raw.githubusercontent.com/{OWNER}/{REPO}/{quote(branch, safe='')}/{quote(path, safe='/')}"
        try:
            text = get(raw).text[:250000]
            docs.append({"title":title_of(text,Path(path).stem),"url":f"https://github.com/{OWNER}/{REPO}/blob/{branch}/{path}","source":"repository","path":path,"text":text})
        except requests.RequestException as e: logging.warning("Skip %s: %s", path, e)
    return docs

def read_wiki():
    docs = []

    with tempfile.TemporaryDirectory(prefix="sujeeth-wiki-") as tmp:
        repo_dir = Path(tmp) / "wiki.git"
        wiki_url = f"https://github.com/{OWNER}/{REPO}.wiki.git"

        # Download Git objects without checking out Windows-invalid filenames.
        result = subprocess.run(
            ["git", "clone", "--bare", "--depth", "1", wiki_url, str(repo_dir)],
            capture_output=True,
            text=True,
            timeout=120
        )

        if result.returncode != 0:
            logging.error("Wiki clone failed: %s", result.stderr)
            raise RuntimeError("Could not download the GitHub wiki.")

        tree = subprocess.run(
            ["git", "--git-dir", str(repo_dir), "ls-tree", "-r", "--name-only", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30
        )

        if tree.returncode != 0:
            logging.error("Could not list wiki files: %s", tree.stderr)
            raise RuntimeError("Could not list GitHub wiki pages.")

        for path in tree.stdout.splitlines():
            if not path.lower().endswith((".md", ".markdown")):
                continue

            if len(docs) >= MAX_FILES:
                break

            content_result = subprocess.run(
                ["git", "--git-dir", str(repo_dir), "show", f"HEAD:{path}"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30
            )

            if content_result.returncode != 0:
                logging.warning("Could not read wiki page %s", path)
                continue

            content = content_result.stdout[:250000]
            page_name = Path(path).stem

            docs.append({
                "title": title_of(content, page_name),
                "url": f"https://github.com/{OWNER}/{REPO}/wiki/{quote(page_name, safe='')}",
                "source": "wiki",
                "path": path,
                "text": content
            })

        logging.info("Loaded %d wiki pages", len(docs))
        return docs

def documents(force=False):
    if cache["docs"] and not force and time.time() - cache["time"] < CACHE_SECONDS:
        return cache["docs"]

    docs = read_wiki()

    if not docs:
        raise RuntimeError(
            "No wiki pages loaded. Check that the GitHub wiki exists and is accessible."
        )

    cache.update({"time": time.time(), "docs": docs})
    return docs


def plain(text):
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 \2", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[#*_>|]", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def respond(data,status=200):
    return func.HttpResponse(json.dumps(data,ensure_ascii=False),status_code=status,mimetype="application/json")

@app.route(route="health", methods=["GET"])
def health(req: func.HttpRequest):
    return respond({"status":"ok","repository":f"{OWNER}/{REPO}","cache_loaded":bool(cache["docs"]),"document_count":len(cache["docs"])})

@app.route(route="search", methods=["GET","POST"])
def search(req: func.HttpRequest):
    try:
        if req.method == "POST":
            body=req.get_json(); query=str(body.get("query","")).strip(); limit=int(body.get("limit",8))
        else:
            query=(req.params.get("q") or req.params.get("query") or "").strip(); limit=int(req.params.get("limit",8))
        if not query: return respond({"error":"Provide ?q=... or JSON {query: ...}"},400)
        if len(query)>500: return respond({"error":"Query too long"},400)
        terms=[x.lower() for x in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9'-]*",query) if len(x)>1]
        results=[]
        for d in documents():
            hay=(d["title"]+" "+d["path"]+" "+d["text"]).lower()
            score=sum(hay.count(t) for t in terms)
            if not score: continue
            body=plain(d["text"])
            positions=[body.lower().find(t) for t in terms if body.lower().find(t)>=0]
            start=max(0,(min(positions) if positions else 0)-160)
            results.append({"title":d["title"],"url":d["url"],"source":d["source"],"path":d["path"],"score":score,"snippet":body[start:start+1500]})
        results.sort(key=lambda x:x["score"],reverse=True)
        return respond({"query":query,"count":len(results),"results":results[:max(1,min(limit,20))]})
    except requests.RequestException:
        logging.exception("GitHub request failed"); return respond({"error":"Could not load content from GitHub"},502)
    except Exception:
        logging.exception("Search failed"); return respond({"error":"Unexpected server error"},500)

@app.route(route="refresh", methods=["POST"])
def refresh(req: func.HttpRequest):
    try:
        docs=documents(force=True); return respond({"status":"refreshed","document_count":len(docs)})
    except Exception:
        logging.exception("Refresh failed"); return respond({"error":"Could not refresh content"},502)
