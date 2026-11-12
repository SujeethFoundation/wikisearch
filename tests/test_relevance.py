"""Ranking: words that belong together match, the question's words found together count, and the excerpt is the stretch
of the page that holds the answer."""
import json

import azure.functions as func
import pytest

import function_app as fa


def page(name, text, title=None):
    return {"title": title or name.replace("-", " "), "url": f"https://github.com/o/r/wiki/{name}", "source": "wiki",
            "path": f"{name}.md", "text": text}


def use(monkeypatch, docs, **settings):
    """Load these pages with these ranking settings (STEMMER, PHRASE_BOOST, BEST_PASSAGE)."""
    for name, value in settings.items():
        monkeypatch.setattr(fa, name, value)
    fa._stem_memo.clear()
    fa.cache.update({"time": 9e18, "docs": docs, "index": fa.build_index(docs), "source": "test"})


@pytest.fixture(autouse=True)
def clean():
    fa._stem_memo.clear()
    fa.cache.update({"time": 0, "docs": [], "index": None, "source": None})
    yield
    fa._stem_memo.clear()


def order(question):
    return [r["path"] for r in fa.rank(question)]


# ---- words that belong together

def test_related_words_share_a_stem_and_lookalikes_do_not():
    same = lambda *words: len({fa.stem(w) for w in words}) == 1
    assert same("job", "jobs") and same("book", "books") and same("week", "weeks") and same("goal", "goals")
    assert same("donate", "donation", "donating", "donations") and same("mentor", "mentors", "mentoring")
    assert same("interview", "interviews", "interviewing")
    apart = lambda *words: len({fa.stem(w) for w in words}) == len(words)
    assert apart("interview", "interest", "internship") and apart("program", "progress") and apart("method", "methodology")
    assert fa.stem("4") == "4" and fa.stem("2025") == "2025" and fa.stemmer_in_use() == "english"


def test_the_old_five_letter_rule_is_still_there_as_a_setting(monkeypatch):
    monkeypatch.setattr(fa, "STEMMER", "prefix")
    assert fa.stemmer_in_use() == "prefix"
    assert fa.stem("interview") == fa.stem("internship") == "inter" and fa.stem("jobs") != fa.stem("job")


def test_search_still_works_without_the_stemmer_package(monkeypatch):
    monkeypatch.setattr(fa, "_english", None)
    assert fa.stemmer_in_use() == "prefix" and fa.ranking_settings()["stemmer"] == "prefix"
    use(monkeypatch, [page("Donor-FAQ", "How donations are used for students.")])
    assert order("donating") == ["Donor-FAQ.md"]


def test_a_question_about_internships_does_not_land_on_the_interviews_page(monkeypatch):
    docs = [page("Week-5-Interviews", "Interview practice comes first. Interviews are stories. Interviewing well takes prep. " * 3),
            page("FAQ-for-Students", "Students ask many things about sessions, mentors, books and careers. " * 6 + "Some look for an internship."),
            page("Home", "Welcome to the wiki.")]
    use(monkeypatch, docs)
    assert order("What about internships?") == ["FAQ-for-Students.md"]
    use(monkeypatch, docs, STEMMER="prefix", PHRASE_BOOST=0.0)
    assert order("What about internships?")[0] == "Week-5-Interviews.md"                        # the old rule's mistake


def test_a_plural_in_the_question_finds_the_singular_on_the_page(monkeypatch):
    docs = [page("Week-5", "How many job applications to send, and which job to choose."), page("Home", "Welcome to the program.")]
    use(monkeypatch, docs)
    assert order("jobs") == ["Week-5.md"]
    use(monkeypatch, docs, STEMMER="prefix")
    assert order("jobs") == []                                                                  # the old rule missed it


# ---- the question's words found together

def test_only_the_words_that_say_what_the_question_is_about_are_paired(monkeypatch):
    docs = [page("A", "reach out to a professional about work"), page("B", "tailor your resume"), page("C", "other words here")]
    use(monkeypatch, docs)
    index = fa.cache["index"]
    assert fa.content_words("How do I reach out to a professional?") == ["reach", "out", "profession"]
    pairs = fa.query_pairs("How do I reach out to a professional?", index)
    assert set(pairs) == {("reach", "out"), ("out", "profession")}                    # no "how do", no "do i", no "to a"
    assert fa.query_pairs("How do I tailor my resume for zebras?", index) == {("tailor", "resum"): pytest.approx(pairs[("reach", "out")])}
    assert fa.query_pairs("how do i", index) == {} and fa.query_pairs("resume resume", index) == {}


def test_a_pair_counts_when_in_order_and_at_most_a_few_words_apart(monkeypatch):
    use(monkeypatch, [page("P", "tailor my resume today. resume first, tailor later. tailor one two three four five resume.")])
    entry = fa.cache["index"]["entries"][0]
    assert fa.pair_places(entry, ("tailor", "resum")) == [0]             # "tailor my resume": yes; reversed: no; six words apart: no
    assert fa.pair_places(entry, ("resum", "tailor")) and fa.pair_places(entry, ("tailor", "zebra")) == []


def test_the_page_that_has_the_words_together_ranks_above_one_that_scatters_them(monkeypatch):
    docs = [page("Scattered", "Jobs are posted online every day. A tailor made my suit and my shoes for the big day. "
                              "Much later that year I finally wrote a resume, and then another resume. The tailor was kind, and the job was mine."),
            page("Together", "How do I tailor my resume for a specific job? Read the posting, then match what it asks for line by line."),
            page("Other", "Nothing relevant on this page at all.")]
    use(monkeypatch, docs, PHRASE_BOOST=0.0)
    assert order("How do I tailor my resume to a job?")[0] == "Scattered.md"
    use(monkeypatch, docs, PHRASE_BOOST=0.5)
    assert order("How do I tailor my resume to a job?")[0] == "Together.md"


# ---- the excerpt

LONG = ("Do not miss the kickoff call. " + "Mentors volunteer their time. " * 70
        + "What if I miss a session? Tell your mentor before the session and catch up from the notes afterwards. "
        + "Other topics follow. " * 40)


def test_the_excerpt_is_the_stretch_that_holds_the_answer_not_the_first_mention(monkeypatch):
    docs = [page("FAQ-for-Students", LONG), page("Home", "Welcome.")]
    use(monkeypatch, docs)
    best = fa.rank("What happens if I miss a session?")[0]["snippet"]
    assert "What if I miss a session?" in best and "catch up from the notes" in best and len(best) <= fa.SNIPPET_CHARS
    use(monkeypatch, docs, BEST_PASSAGE=False)
    old = fa.rank("What happens if I miss a session?")[0]["snippet"]
    assert "What if I miss a session?" not in old                       # the old rule started at the first rare word it met


def test_the_best_stretch_reports_how_much_of_the_question_it_holds(monkeypatch):
    use(monkeypatch, [page("P", "alpha here. " + "filler " * 400 + "alpha beta gamma together."), page("Q", "delta only"), page("R", "none")])
    index = fa.cache["index"]
    entry = index["entries"][0]
    idf = {t: 1.0 for t in ("alpha", "beta", "gamma")}
    start, share = fa.best_passage(entry, idf, {})
    assert share == pytest.approx(1.0) and "alpha beta gamma" in entry["body"][start:start + fa.SNIPPET_CHARS]
    assert fa.best_passage(entry, {"zebra": 1.0}, {}) == (0, 0.0)


# ---- what stays the same, and the debug view

def test_results_keep_their_shape_and_excluded_pages_stay_out(monkeypatch):
    docs = [page("Week-4-Networking", "Week 4 agenda: networking strategies."),
            page("Fall-2025-Circle-A", "Recording of the week 4 networking session.", title="Fall 2025 Circle A")]
    use(monkeypatch, docs)
    results = fa.rank("week 4 networking")
    assert [r["path"] for r in results] == ["Week-4-Networking.md"]
    assert set(results[0]) == {"title", "url", "source", "path", "chunk_id", "score", "snippet"}
    assert fa.rank("") == [] and fa.rank("zebra") == []


def test_debug_says_how_a_score_came_about(monkeypatch):
    use(monkeypatch, [page("Together", "How do I tailor my resume for a job?"), page("Other", "A resume."), page("Third", "Nothing.")])
    why = fa.rank("tailor my resume", debug=True)[0]["why"]
    assert why["words_matched"] == "2 of 2" and why["pairs_together"] == ["tailor resum"] and why["phrase_share"] == 1.0
    assert why["words"]["tailor"]["in_text"] == 1 and why["words"]["resum"]["rarity"] > 0 and "excerpt_starts_at" in why


def test_the_routes_report_the_ranking_settings_and_take_debug(monkeypatch):
    use(monkeypatch, [page("Together", "How do I tailor my resume for a job?"), page("Other", "Nothing here.")])
    monkeypatch.setattr(fa, "documents", lambda force=False: fa.cache["docs"])
    get = lambda params: json.loads(fa.search._function._func(func.HttpRequest("GET", "/api/search", params=params, headers={}, body=b"")).get_body())
    plain = get({"q": "tailor my resume", "full_text": "false"})
    assert plain["ranking"] == {"stemmer": "english", "phrase_boost": 0.5, "best_passage": True, "unique_pages": True} and "why" not in plain["results"][0]
    assert "why" in get({"q": "tailor my resume", "full_text": "false", "debug": "true"})["results"][0]
    post = func.HttpRequest("POST", "/api/search", params={}, headers={}, body=json.dumps({"query": "resume", "debug": True, "full_text": False}).encode())
    assert "why" in json.loads(fa.search._function._func(post).get_body())["results"][0]
    health = json.loads(fa.health._function._func(func.HttpRequest("GET", "/api/health", headers={}, body=b"")).get_body())
    assert health["ranking"]["stemmer"] == "english"


# ---- every result is a different page

def test_two_files_with_the_same_address_are_one_page_and_the_better_one_is_returned(monkeypatch):
    docs = [dict(page("Home", "Mentors and students. Welcome to the career circle wiki."), path="Home.md"),
            dict(page("Home", "Career circle career circle career circle career circle mentors."), path="archive/Home.md"),
            page("FAQ-for-Mentors", "Mentors volunteer for the career circle."), page("Other", "Nothing about it.")]
    use(monkeypatch, docs)
    paths = order("career circle mentors")
    homes = [p for p in paths if p.endswith("Home.md")]
    assert homes == ["archive/Home.md"]                                            # the better-ranked copy, once
    monkeypatch.setattr(fa, "UNIQUE_PAGES", False)
    assert len([p for p in order("career circle mentors") if p.endswith("Home.md")]) == 2


def test_addresses_that_differ_in_case_a_slash_or_a_fragment_are_one_page():
    rows = [{"url": "https://github.com/o/r/wiki/Home", "path": "a.md"}, {"url": "https://github.com/o/r/wiki/home/", "path": "b.md"},
            {"url": "https://github.com/o/r/wiki/Home#intro", "path": "c.md"}, {"url": "https://github.com/o/r/wiki/FAQ", "path": "d.md"},
            {"url": "", "path": "e.md"}, {"url": "", "path": "E.md"}]
    assert [r["path"] for r in fa.unique_pages(rows)] == ["a.md", "d.md", "e.md"]


def test_the_search_route_returns_five_different_pages(monkeypatch):
    docs = [page(f"P{i}", "career circle mentors") for i in range(4)] + [dict(page("P0", "career circle"), path="x/P0.md")] * 2
    use(monkeypatch, docs)
    monkeypatch.setattr(fa, "documents", lambda force=False: fa.cache["docs"])
    body = json.loads(fa.search._function._func(func.HttpRequest("GET", "/api/search", params={"q": "career circle", "limit": "8", "full_text": "false"},
                                                                  headers={}, body=b"")).get_body())
    urls = [r["url"] for r in body["results"]]
    assert len(urls) == len(set(urls)) == 4 and body["count"] == 4
