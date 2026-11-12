"""The relevance scorer for the wiki search."""
import importlib.util
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import function_app as fa

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location("wiki_relevance_eval", os.path.join(HERE, "relevance_eval.py"))
ev = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ev)

PAGES = [
    {"title": "Faq For Students", "url": "https://github.com/o/r/wiki/FAQ-for-Students", "source": "wiki", "path": "FAQ-for-Students.md",
     "text": "Do not miss the kickoff. " + "Students meet mentors weekly. " * 80 + "What if I miss a session? Tell your mentor and catch up. Some look for an internship."},
    {"title": "Week 5: Interviews", "url": "https://github.com/o/r/wiki/Week-5", "source": "wiki", "path": "Week-5.md",
     "text": "Interview practice. Interviews are stories. " * 5},
    {"title": "Home", "url": "https://github.com/o/r/wiki/Home", "source": "wiki", "path": "Home.md", "text": "Welcome to the wiki."},
]
GOLD = [{"question": "What happens if I miss a session?", "expect": ["FAQ-for-Students"], "passage": ["What if I miss a session"]},
        {"question": "What about internships?", "expect": ["FAQ-for-Students"]}]


def test_a_result_is_right_by_file_name_address_or_title_and_the_excerpt_is_judged_on_right_pages_only():
    results = [{"path": "Home.md", "url": "u/Home", "title": "Home", "snippet": "what if i miss a session"},
               {"path": "FAQ-for-Students.md", "url": "u/FAQ", "title": "FAQ", "snippet": "nothing useful"}]
    assert ev.judge({"expect": ["faq-for-students"], "passage": ["miss a session"]}, results) == (2, False)   # the phrase is on the wrong page
    assert ev.judge({"expect": ["Home"], "passage": ["Miss a Session"]}, results) == (1, True)
    assert ev.judge({"expect": ["Home"]}, results) == (1, None) and ev.judge({"expect": ["nowhere"], "passage": ["x"]}, results) == (None, False)


def test_the_summary_counts_places_and_excerpts():
    rows = [{"place": 1, "excerpt": True}, {"place": 3, "excerpt": False}, {"place": None, "excerpt": None}]
    assert ev.summarize(rows) == {"questions": 3, "first": 1, "top3": 2, "mrr": round((1 + 1 / 3) / 3, 3), "excerpts": 1, "excerpts_judged": 2}


def test_tried_settings_are_parsed_and_put_back(monkeypatch):
    assert ev.parse_trial(["stemmer=prefix", "phrase_boost=0", "best_passage=false"]) == {"stemmer": "prefix", "phrase_boost": 0.0, "best_passage": False}
    for bad in (["stemmer"], ["nonsense=1"]):
        try:
            ev.parse_trial(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad} should be refused")
    fa.cache.update({"docs": PAGES, "index": fa.build_index(PAGES)})
    before = ev.apply_settings(fa, {"stemmer": "prefix", "phrase_boost": 0.0})
    assert fa.STEMMER == "prefix" and fa.PHRASE_BOOST == 0.0 and before == {"stemmer": "english", "phrase_boost": 0.5}
    ev.apply_settings(fa, before)
    assert fa.STEMMER == "english" and fa.PHRASE_BOOST == 0.5


def test_a_local_run_scores_this_checkout_and_a_trial_beside_it(tmp_path, capsys, monkeypatch):
    def local(read=None):
        fa.cache.update({"time": 9e18, "docs": PAGES, "index": fa.build_index(PAGES), "source": "test"})
        return fa
    monkeypatch.setattr(ev, "load_local", local)
    gold = tmp_path / "g.json"
    gold.write_text(json.dumps(GOLD))
    code = ev.main(["--local", "--gold", str(gold), "--try", "stemmer=prefix", "phrase_boost=0", "best_passage=false", "--explain"])
    out = capsys.readouterr().out
    assert code == 0 and " here: right page first 2/2" in out and "excerpt holds the answer 1/1" in out
    assert "  try: right page first 1/2" in out and "excerpt holds the answer 0/1" in out       # the old settings on the same pages
    assert "try ranks the right page higher than here on 0 question(s), lower on 1." in out
    assert "Every question is answered first." in out and fa.STEMMER == "english" and fa.BEST_PASSAGE is True


class App(BaseHTTPRequestHandler):
    status = 200

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        if App.status != 200:
            self.send_response(App.status)
            self.send_header("content-length", "0")
            self.end_headers()
            return
        results = [{"path": "Home.md", "url": "u", "title": "Home", "snippet": "welcome", "score": 2.0,
                    "why": {"words": {"session": {"rarity": 0.5, "in_text": 0, "in_title": 0}}, "words_matched": "1 of 2", "title_share": 0,
                            "pairs_together": [], "pairs_looked_for": ["miss session"]}} if body.get("debug") else
                   {"path": "Home.md", "url": "u", "title": "Home", "snippet": "welcome", "score": 2.0},
                   {"path": "FAQ-for-Students.md", "url": "u", "title": "FAQ", "snippet": "What if I miss a session? ...", "score": 1.0}]
        data = json.dumps({"results": results}).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def run_live(tmp_path, args, status=200):
    App.status = status
    server = HTTPServer(("127.0.0.1", 0), App)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    gold = tmp_path / "g.json"
    gold.write_text(json.dumps(GOLD[:1]))
    try:
        return ev.main(["--url", f"http://127.0.0.1:{server.server_port}", "--gold", str(gold)] + args)
    finally:
        server.shutdown()


def test_a_live_run_scores_the_deployed_app_and_explains_a_miss(tmp_path, capsys):
    assert run_live(tmp_path, ["--key", "k", "--explain"]) == 0
    out = capsys.readouterr().out
    assert " live: right page first 0/1   in the top 3 1/1" in out and "excerpt holds the answer 1/1" in out
    assert "right page at place 2" in out and "first result: Home.md" in out and "pairs found together: none (of miss session)" in out


def test_a_wrong_key_or_address_or_misuse_ends_with_a_clear_message(tmp_path, capsys, monkeypatch):
    assert run_live(tmp_path, ["--key", "wrong"], status=401) == 2 and "rejected the key" in capsys.readouterr().out
    gold = tmp_path / "g.json"
    assert ev.main(["--url", "127.0.0.1:9", "--key", "k", "--gold", str(gold)]) == 2 and "Could not reach the app" in capsys.readouterr().out
    assert ev.main(["--try", "phrase_boost=0", "--gold", str(gold)]) == 2 and "needs --local" in capsys.readouterr().out
    monkeypatch.delenv("WIKISEARCH_KEY", raising=False)
    monkeypatch.setattr(ev.sys, "stdin", None)
    assert ev.main(["--gold", str(gold)]) == 2 and "use --local" in capsys.readouterr().out
    assert ev.normalize_url("host.net/") == "https://host.net"


def test_the_gold_file_is_valid():
    gold = json.load(open(os.path.join(HERE, "relevance_gold.json"), encoding="utf-8"))
    assert len(gold) >= 40 and all(item["question"].strip() and item["expect"] for item in gold)
