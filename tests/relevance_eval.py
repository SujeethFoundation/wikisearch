"""Score the wiki search on questions whose right page you know.

    python tests/relevance_eval.py                  the deployed wikisearch app
    python tests/relevance_eval.py --local          read the wiki from GitHub and rank it here, with this checkout's code
                                                    (nothing is deployed, so a change can be measured before it ships)
    python tests/relevance_eval.py --local --try stemmer=prefix phrase_boost=0 best_passage=false
                                                    ...and a second column with those ranking settings
    python tests/relevance_eval.py --explain        for each question not answered first, how the scores came about

The app's address is --url, else WIKISEARCH_URL, else the production address; "https://" is added when left off. The
Function key is --key, else WIKISEARCH_KEY; with neither it is asked for (hidden). --local needs neither.

The questions are in tests/relevance_gold.json. Each has a "question", "expect" (pieces of the right pages' file names,
addresses or titles; one is enough) and, optionally, "passage" (phrases from the part of the page that answers it). Two
things are scored: where the first right page stands, and whether the excerpt shown for a right page among the top three
holds one of the phrases, because the excerpt is what the reader (or the assistant) sees first.
"""
import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "https://wikisearch-gmebdbewaec6axdp.eastus-01.azurewebsites.net"
HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = {"stemmer": ("STEMMER", str), "phrase_boost": ("PHRASE_BOOST", float),
            "best_passage": ("BEST_PASSAGE", lambda v: str(v).strip().lower() in ("1", "true", "yes", "on"))}


def normalize_url(url):
    text = str(url or "").strip().rstrip("/")
    return text if not text or "://" in text else "https://" + text


def is_right(result, expect):
    hay = " ".join(str(result.get(k) or "") for k in ("path", "url", "title")).lower()
    return any(str(piece).lower() in hay for piece in expect or [])


def judge(item, results):
    """(place of the first right page or None, whether a right page's excerpt in the top three holds the answer or None)."""
    place = next((i for i, r in enumerate(results, 1) if is_right(r, item.get("expect"))), None)
    phrases = [str(p).lower() for p in item.get("passage") or []]
    if not phrases:
        return place, None
    excerpts = [str(r.get("snippet") or "").lower() for r in results[:3] if is_right(r, item.get("expect"))]
    return place, any(p in e for e in excerpts for p in phrases)


def summarize(rows):
    n = len(rows)
    places = [r["place"] for r in rows if r["place"]]
    judged = [r["excerpt"] for r in rows if r["excerpt"] is not None]
    return {"questions": n, "first": sum(1 for p in places if p == 1), "top3": sum(1 for p in places if p <= 3),
            "mrr": round(sum(1.0 / p for p in places) / n, 3) if n else 0.0,
            "excerpts": sum(1 for e in judged if e), "excerpts_judged": len(judged)}


def search_live(url, key, question, debug=False, timeout=60):
    """(results, note)"""
    try:
        body = {"query": question, "limit": 8, "full_text": False, "debug": bool(debug)}
        request = urllib.request.Request(normalize_url(url) + "/api/search", data=json.dumps(body).encode("utf-8"),
                                         headers={"content-type": "application/json", "x-functions-key": key or ""}, method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            return json.loads(reply.read().decode("utf-8")).get("results") or [], ""
    except urllib.error.HTTPError as exc:
        return [], f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:120]}"
    except Exception as exc:
        return [], f"unreachable: {exc.__class__.__name__}: {exc}"[:140]


def load_local(read=None):
    """The app's own module with the wiki loaded: read from GitHub unless `read` supplies the pages."""
    os.environ["PRELOAD_ON_START"] = "false"
    sys.path.insert(0, os.path.dirname(HERE))
    import function_app as app
    docs = (read or app.read_wiki)()
    app.cache.update({"time": 9e18, "docs": docs, "index": app.build_index(docs), "source": "relevance_eval"})
    return app


def apply_settings(app, settings):
    """Set ranking settings on the app module and rebuild its index. Returns what they were, to put back."""
    before = {}
    for name, value in settings.items():
        attribute, _ = SETTINGS[name]
        before[name] = getattr(app, attribute)
        setattr(app, attribute, value)
    app._stem_memo.clear()
    app.cache["index"] = app.build_index(app.cache["docs"])
    return before


def parse_trial(pairs):
    out = {}
    for pair in pairs or []:
        name, eq, value = str(pair).partition("=")
        name = name.strip()
        if not eq or name not in SETTINGS:
            raise ValueError(f"--try takes NAME=VALUE with NAME one of {', '.join(SETTINGS)}; got {pair!r}")
        out[name] = SETTINGS[name][1](value.strip())
    return out


def evaluate(gold, ask):
    rows = []
    for item in gold:
        results, note = ask(item["question"])
        place, excerpt = judge(item, results)
        rows.append({"question": item["question"], "place": place, "excerpt": excerpt, "note": note,
                     "top": results[0] if results else None, "right": next((r for r in results if is_right(r, item.get("expect"))), None)})
    return rows


def why_lines(result, label):
    why = (result or {}).get("why")
    if not why:
        return [f"    {label}: {((result or {}).get('path') or '-')}  (no detail: ask with --explain against an app that has the debug option)"]
    words = ", ".join(f"{w} {v['in_text']}x in text, {v['in_title']}x in title (rarity {v['rarity']:g})" for w, v in why["words"].items())
    together = ", ".join(why.get("pairs_together") or []) or "none"
    return [f"    {label}: {result.get('path')}  score {result.get('score')}",
            f"      words: {words}",
            f"      {why['words_matched']} words matched, title share {why['title_share']:g}, pairs found together: {together} "
            f"(of {', '.join(why.get('pairs_looked_for') or []) or 'none looked for'})"]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Score the wiki search on questions whose right page is known.")
    ap.add_argument("--url", default=os.getenv("WIKISEARCH_URL", "") or DEFAULT_URL)
    ap.add_argument("--key", default=os.getenv("WIKISEARCH_KEY", ""))
    ap.add_argument("--gold", default=os.path.join(HERE, "relevance_gold.json"))
    ap.add_argument("--local", action="store_true", help="read the wiki from GitHub and rank it here with this checkout's code")
    ap.add_argument("--try", dest="trial", nargs="+", metavar="NAME=VALUE",
                    help="with --local: a second column with these ranking settings (stemmer, phrase_boost, best_passage)")
    ap.add_argument("--explain", action="store_true", help="for each question not answered first, how the scores came about")
    args = ap.parse_args(argv)
    try:
        trial = parse_trial(args.trial)
    except ValueError as exc:
        print(exc)
        return 2
    if trial and not args.local:
        print("--try changes this checkout's settings, so it needs --local.")
        return 2
    with open(args.gold, encoding="utf-8") as handle:
        gold = json.load(handle)
    runs = {}
    if args.local:
        print("Reading the wiki from GitHub ...")
        app = load_local()
        print(f"{len(app.cache['docs'])} pages; ranking with {app.ranking_settings()}")
        runs["here"] = evaluate(gold, lambda q: (app.rank(q, debug=args.explain)[:8], ""))
        if trial:
            before = apply_settings(app, trial)
            runs["try"] = evaluate(gold, lambda q: (app.rank(q, debug=args.explain)[:8], ""))
            apply_settings(app, before)
    else:
        args.url = normalize_url(args.url)
        if not args.key:
            if sys.stdin is not None and sys.stdin.isatty():
                args.key = getpass.getpass("Function key for the wikisearch app (hidden): ")
            else:
                print("No Function key: pass --key, set WIKISEARCH_KEY, or use --local.")
                return 2
        print(f"Searching {args.url}")
        _, note = search_live(args.url, args.key, gold[0]["question"])
        if note.startswith(("HTTP 401", "HTTP 403")):
            print(f"The app rejected the key ({note}). Check the Function key and try again.")
            return 2
        if note.startswith("unreachable:"):
            print(f"Could not reach the app: {note[len('unreachable:'):].strip()}\nCheck the address ({args.url}) and that the app is running.")
            return 2
        runs["live"] = evaluate(gold, lambda q: search_live(args.url, args.key, q, debug=args.explain))
    names = list(runs)
    mark = lambda row: f"{row['place'] or '-':>3}{'' if row['excerpt'] is None else (' +' if row['excerpt'] else ' x')}"
    print(f"\n{len(gold)} questions from {args.gold}")
    print("place of the right page; + the excerpt holds the answer, x it does not\n")
    print(f"{'Question':52}" + "".join(f" {name:>6}" for name in names) + "  First result" + (f" ({names[-1]})" if len(names) > 1 else ""))
    for i, item in enumerate(gold):
        last = runs[names[-1]][i]
        question = item["question"] if len(item["question"]) <= 52 else item["question"][:49] + "..."
        note = f"   [{last['note']}]" if last["note"] else ""
        print(f"{question:52}" + "".join(f" {mark(runs[name][i]):>6}" for name in names) + f"  {(last['top'] or {}).get('path', '-')}{note}")
    print()
    for name in names:
        s = summarize(runs[name])
        print(f"{name:>5}: right page first {s['first']}/{s['questions']}   in the top 3 {s['top3']}/{s['questions']}   "
              f"mean reciprocal rank {s['mrr']:.3f}   excerpt holds the answer {s['excerpts']}/{s['excerpts_judged']}")
    if trial:
        print("  try = this checkout with " + ", ".join(f"{k}={v}" for k, v in trial.items()))
        a, b = runs["here"], runs["try"]
        better = sum(1 for x, y in zip(a, b) if (y["place"] or 99) < (x["place"] or 99))
        worse = sum(1 for x, y in zip(a, b) if (y["place"] or 99) > (x["place"] or 99))
        print(f"  try ranks the right page higher than here on {better} question(s), lower on {worse}.")
    if args.explain:
        rows = runs[names[0]]
        misses = [r for r in rows if r["place"] != 1 or r["excerpt"] is False]
        print(f"\nQuestions not answered first, or whose excerpt misses the answer ({len(misses)}):" if misses else "\nEvery question is answered first.")
        for row in misses:
            print(f"\n  {row['question']}   (right page at place {row['place'] or '-'}"
                  + ("" if row["excerpt"] is None else ", excerpt " + ("holds the answer" if row["excerpt"] else "MISSES the answer")) + ")")
            for line in why_lines(row["top"], "first result"):
                print(line)
            if row["right"] is not None and row["right"] is not row["top"]:
                for line in why_lines(row["right"], "right page  "):
                    print(line)
    failed = [row for name in names for row in runs[name] if row["note"]]
    if failed:
        print(f"\n{len(failed)} search(es) did not complete normally; their notes are in brackets above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
