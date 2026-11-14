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
    assert plain["ranking"] == {"stemmer": "english", "phrase_boost": 0.5, "best_passage": True, "unique_pages": True, "hub_boost": 0.5, "hub_expand": True, "synonym_weight": 0.7, "also_weight": 0.4, "heading_boost": 1.5} and "why" not in plain["results"][0]
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


# ---- an overview page that links to the pages that answer

def weekly_wiki():
    weeks = [page(f"Week-{n}", f"Week {n} agenda: icebreaker and discussion.", title=f"Week {n}") for n in range(1, 7)]
    hub = page("Weekly-Content", "Each week has an agenda. " + " ".join(f"The agenda covers week {n} goals. [[Week {n}]]" for n in range(1, 7)))
    home = page("Home", "Welcome. [[Weekly-Content]] holds the agenda for each week.")
    return weeks + [hub, home]


def test_the_overview_that_links_to_every_week_comes_before_one_week(monkeypatch):
    use(monkeypatch, weekly_wiki(), HUB_BOOST=0.5)
    assert order("weekly agendas")[0] == "Weekly-Content.md"


def test_a_question_about_one_week_is_still_answered_by_that_week(monkeypatch):
    use(monkeypatch, weekly_wiki(), HUB_BOOST=0.5)
    assert order("agenda for week 4")[0] == "Week-4.md"


def test_the_overview_boost_can_be_turned_off_and_needs_three_linked_pages(monkeypatch):
    docs = weekly_wiki()
    use(monkeypatch, docs, HUB_BOOST=0)
    off = {r["path"]: r["score"] for r in fa.rank("weekly agendas")}
    use(monkeypatch, docs, HUB_BOOST=0.5)
    on = {r["path"]: r["score"] for r in fa.rank("weekly agendas")}
    assert on["Weekly-Content.md"] > off["Weekly-Content.md"] and on["Week-1.md"] == off["Week-1.md"]
    assert fa.rank("weekly agendas", debug=True)[0]["why"]["hub_pages"] == 6
    few = [page("A", "agenda of the week"), page("B", "agenda of the week"), page("Hub", "agenda of the week [[A]] [[B]]")]
    use(monkeypatch, few, HUB_BOOST=0.5)
    assert all("hub_pages" not in r["why"] for r in fa.rank("agenda week", debug=True))


def search_route(monkeypatch, params):
    monkeypatch.setattr(fa, "documents", lambda force=False: fa.cache["docs"])
    request = func.HttpRequest("GET", "/api/search", params=params, headers={}, body=b"")
    return json.loads(fa.search._function._func(request).get_body())


def test_the_overview_brings_the_pages_it_links_to_even_past_the_limit(monkeypatch):
    use(monkeypatch, weekly_wiki(), HUB_BOOST=0.5)
    body = search_route(monkeypatch, {"q": "weekly agendas", "limit": "2", "full_text": "false"})
    paths = [r["path"] for r in body["results"]]
    # the overview, then every week in the order it links them, not in score order
    assert paths[0] == "Weekly-Content.md" and paths[1:] == [f"Week-{n}.md" for n in range(1, 7)]
    hub = body["results"][0]
    assert hub["hub_pages"] == [f"https://github.com/o/r/wiki/Week-{n}" for n in range(1, 7)]
    assert all(r["hub_of"] == hub["url"] for r in body["results"][1:]) and "hub_of" not in hub
    assert len(paths) == len(set(paths))


def test_a_question_about_one_week_gets_no_expansion(monkeypatch):
    use(monkeypatch, weekly_wiki(), HUB_BOOST=0.5)
    body = search_route(monkeypatch, {"q": "agenda for week 4", "limit": "2", "full_text": "false"})
    assert len(body["results"]) == 2 and not any("hub_pages" in r or "hub_of" in r for r in body["results"])


def test_the_expansion_can_be_turned_off(monkeypatch):
    use(monkeypatch, weekly_wiki(), HUB_BOOST=0.5, HUB_EXPAND=False)
    body = search_route(monkeypatch, {"q": "weekly agendas", "limit": "2", "full_text": "false"})
    assert len(body["results"]) == 2 and body["ranking"]["hub_expand"] is False
    assert not any("hub_pages" in r or "hub_of" in r for r in body["results"])


def test_a_page_is_marked_as_part_of_an_overview_only_when_that_overview_is_returned(monkeypatch):
    docs = weekly_wiki() + [page("Donate", "How to donate and give a gift. [[Week-1]] [[Week-2]] [[Week-3]] donate gift week agenda")]
    use(monkeypatch, docs, HUB_BOOST=0.5)
    body = search_route(monkeypatch, {"q": "weekly agendas", "limit": "20", "full_text": "false"})
    shown = {r["url"] for r in body["results"]}
    assert all(r["hub_of"] in shown for r in body["results"] if "hub_of" in r)


def test_an_expanded_page_that_is_already_shown_appears_once(monkeypatch):
    use(monkeypatch, weekly_wiki(), HUB_BOOST=0.5)
    body = search_route(monkeypatch, {"q": "weekly agendas", "limit": "20", "full_text": "false"})
    paths = [r["path"] for r in body["results"]]
    assert len(paths) == len(set(paths)) and paths[:7] == ["Weekly-Content.md"] + [f"Week-{n}.md" for n in range(1, 7)]


def syllabus_wiki():
    """The overview is titled Syllabus, a word the weeks never use; "syllabus" and "agenda" are synonyms."""
    weeks = [page(f"Week-{n}", f"Week {n} agenda: icebreaker and discussion.", title=f"Week {n}") for n in range(1, 7)]
    hub = page("Syllabus", "Each week has an agenda. " + " ".join(f"The agenda covers week {n} goals. [[Week {n}]]" for n in range(1, 7)))
    return weeks + [hub]


def test_a_rare_synonym_does_not_outscore_the_word_it_stands_for(monkeypatch):
    monkeypatch.setattr(fa, "SYNONYM_GROUPS", [["agenda", "syllabus"]])
    common = [page(f"Notes-{n}", "Notes with an agenda for the meeting.") for n in range(6)]
    with_word = page("With-Word", "Notes with an agenda for the meeting.")
    with_synonym = page("With-Synonym", "Notes with a syllabus for the meeting.")  # "syllabus" is on one page only, so it is rarer
    use(monkeypatch, common + [with_synonym, with_word])
    scores = {r["path"]: r["score"] for r in fa.rank("agenda")}
    # a page that has only the synonym counts for the word at SYNONYM_WEIGHT of what the word itself gives
    assert scores["With-Word.md"] > scores["With-Synonym.md"] > 0


def test_a_question_in_a_synonym_of_the_agenda_reaches_the_weeks_through_the_overview(monkeypatch):
    monkeypatch.setattr(fa, "SYNONYM_GROUPS", [["agenda", "syllabus"]])
    use(monkeypatch, syllabus_wiki(), HUB_BOOST=0.5)
    body = search_route(monkeypatch, {"q": "what is the syllabus", "limit": "2", "full_text": "false"})
    assert [r["path"] for r in body["results"]] == ["Syllabus.md"] + [f"Week-{n}.md" for n in range(1, 7)]


def test_give_is_a_request_word_not_a_topic(monkeypatch):
    assert fa.query_stems("can you give me the weekly agendas?") == fa.query_stems("weekly agendas")


# ---- synonyms

def test_a_page_with_a_synonym_of_the_question_word_is_found(monkeypatch):
    docs = [page("Resources", "Download the slides and templates here."), page("Other", "Nothing relevant to see.")]
    use(monkeypatch, docs, SYNONYM_GROUPS=[["material", "slide"]], SYNONYM_WEIGHT=0.7)
    assert order("weekly materials") == ["Resources.md"]
    use(monkeypatch, docs, SYNONYM_GROUPS=[["material", "slide"]], SYNONYM_WEIGHT=0)
    assert order("weekly materials") == []


def test_the_word_itself_beats_its_synonym_and_both_directions_work(monkeypatch):
    docs = [page("Exact", "The cost of the program."), page("Synonym", "The fee for the program.")]
    use(monkeypatch, docs, SYNONYM_GROUPS=[["cost", "fee", "price"]], SYNONYM_WEIGHT=0.7)
    assert order("cost") == ["Exact.md", "Synonym.md"]
    assert order("fee") == ["Synonym.md", "Exact.md"]
    assert fa.rank("cost", debug=True)[1]["why"]["found_through_synonyms"] == {"cost": "fee"}


def test_synonyms_count_a_word_once_not_once_per_synonym(monkeypatch):
    docs = [page("Many", "cost fee price cost fee price"), page("Both", "cost and the program schedule")]
    use(monkeypatch, docs, SYNONYM_GROUPS=[["cost", "fee", "price"]], SYNONYM_WEIGHT=0.7)
    assert fa.rank("cost", debug=True)[0]["why"]["words_matched"] == "1 of 1"


def test_synonyms_json_is_valid_and_a_broken_file_means_none(tmp_path):
    groups = fa.load_synonym_groups()
    assert len(groups) > 5 and all(isinstance(g, list) and len(g) > 1 and all(" " not in w for w in g) for g in groups)
    (tmp_path / "bad.json").write_text("{not json")
    assert fa.load_synonym_groups(tmp_path / "bad.json") == []
    assert fa.load_synonym_groups(tmp_path / "missing.json") == []


# ---- extra words from a caller (`also`)

def test_extra_words_lift_a_page_without_marking_others_down_or_adding_pages(monkeypatch):
    docs = [page("A", "Week 5 covers interviews and resumes."), page("B", "Week 5 covers interviews and mock practice."),
            page("C", "Only mock practice here.")]
    use(monkeypatch, docs, SYNONYM_WEIGHT=0)
    assert [r["path"] for r in fa.rank("interviews")] == ["A.md", "B.md"] or [r["path"] for r in fa.rank("interviews")] == ["B.md", "A.md"]
    plain = {r["path"]: r["score"] for r in fa.rank("interviews")}
    boosted = fa.rank("interviews", also="mock practice")
    assert [r["path"] for r in boosted][0] == "B.md" and {r["path"] for r in boosted} == {"A.md", "B.md"}   # C has no question word
    assert next(r for r in boosted if r["path"] == "A.md")["score"] == plain["A.md"]                         # A is not marked down
    assert fa.rank("interviews", also=["mock", "practice"])[0]["path"] == "B.md"
    assert fa.rank("interviews", also="mock", debug=True)[0]["why"]["found_extra_words"] == ["mock"]


def test_extra_words_can_be_turned_off_and_ignore_the_question_words_and_stop_words(monkeypatch):
    docs = [page("A", "interviews and resumes"), page("B", "interviews and mock practice")]
    use(monkeypatch, docs, SYNONYM_WEIGHT=0, ALSO_WEIGHT=0)
    assert fa.rank("interviews", also="mock") == fa.rank("interviews")
    use(monkeypatch, docs, SYNONYM_WEIGHT=0)
    assert fa.rank("interviews", also="interviews the and") == fa.rank("interviews")


# ---- a heading that asks the question

def faq_wiki():
    filler = "The program pairs students with mentors for six weeks. " * 6
    return [
        page("Overview", "Students join the program each fall. Contact details are on the site. " + filler + "Any question is welcome."),
        page("FAQ", filler + "\n\n**Who should I contact if I have a question about the program?**\n\nWrite to the coordinator.\n\n"
                             "### Q: Who are the students?\n\nUndergraduates."),
        page("Offer", filler + "\n\n**What does the Foundation offer partner institutions?**\n\nA six week program."),
        page("Books", "The Foundation sends books to students. Each book is one we offer for free. " + filler),
    ]


def test_a_page_whose_heading_asks_the_question_comes_first(monkeypatch):
    use(monkeypatch, faq_wiki(), HEADING_BOOST=0)
    assert order("Who do I contact with a question?")[0] == "Overview.md"  # the words, scattered, win without the lift
    use(monkeypatch, faq_wiki(), HEADING_BOOST=1.5)
    assert order("Who do I contact with a question?")[0] == "FAQ.md"
    why = fa.rank("Who do I contact with a question?", debug=True)[0]["why"]
    assert why["heading_share"] == 1.0


def test_a_heading_that_misses_the_question_s_subject_does_not_win(monkeypatch):
    use(monkeypatch, faq_wiki(), HEADING_BOOST=1.5)
    assert order("What books does the Foundation offer?")[0] == "Books.md"


def test_headings_are_the_hash_lines_the_bold_lines_and_the_title():
    found = fa.heading_stems("# Partner FAQ\n\n**How long is each session?**\n\nAbout **an hour** each.\n\n### Q: Who are the students?\n- **Agenda**:")
    assert found == [set(fa.stems("Partner FAQ")), set(fa.stems("How long is each session")), set(fa.stems("Q Who are the students")), {"agenda"}]
    index = fa.build_index([page("Week-1", "text", title="Week 1: Self Assessment")])
    assert set(fa.stems("Week 1 Self Assessment")) in index["entries"][0]["headings"]


# ---- "give"

def test_give_is_a_topic_word_unless_it_asks(monkeypatch):
    assert "give" in fa.query_stems("Can I give to a specific program?")
    assert fa.query_stems("Give me the week 3 agenda") == fa.query_stems("the week 3 agenda")
    assert fa.query_stems("give us the agenda") == ["agenda"]


# ---- the overview, again

def test_the_overview_stays_ahead_of_weeks_whose_headings_ask_the_question(monkeypatch):
    weeks = [page(f"Week-{n}", f"## Week {n} Agenda\n\nIcebreaker and discussion.", title=f"Week {n}") for n in range(1, 7)]
    hub = page("Syllabus", "Each week has an agenda. " + " ".join(f"[[Week {n}]] covers goals." for n in range(1, 7)))
    use(monkeypatch, weeks + [hub], HUB_BOOST=0.5)
    body = search_route(monkeypatch, {"q": "weekly agendas", "limit": "2", "full_text": "false"})
    assert [r["path"] for r in body["results"]] == ["Syllabus.md"] + [f"Week-{n}.md" for n in range(1, 7)]


def test_an_overview_further_down_does_not_bring_its_pages(monkeypatch):
    weeks = [page(f"Week-{n}", f"Week {n}: students contribute to the discussion.", title=f"Week {n}") for n in range(1, 7)]
    hub = page("Overview", "Students contribute each week. " + " ".join(f"[[Week {n}]]" for n in range(1, 7)))
    donors = page("Donors", "How to donate: donate online. Every donation counts. Donate today.", title="Donate")
    use(monkeypatch, weeks + [hub, donors], HUB_BOOST=0.5)
    body = search_route(monkeypatch, {"q": "how do I donate", "limit": "3", "full_text": "false"})
    assert body["results"][0]["path"] == "Donors.md" and len(body["results"]) == 3
    assert not any("hub_pages" in r or "hub_of" in r for r in body["results"])


def test_how_many_words_are_not_the_topic():
    assert fa.query_stems("What are the agendas for all the weeks?") == fa.query_stems("agendas weeks")
    assert fa.query_stems("each") == ["each"]  # a question of nothing else keeps its words


# ---- the deployed package

def test_the_deploy_workflow_ships_the_synonym_file():
    """function_app.py reads synonyms.json at start-up and searches without synonyms when it is missing, so a package without it
    deploys fine and ranks worse (the syllabus overview was not found as an agenda page in production for that reason)."""
    from pathlib import Path
    workflows = list((Path(fa.__file__).parent / ".github" / "workflows").glob("*.y*ml"))
    assert workflows and all("synonyms.json" in w.read_text(encoding="utf-8") for w in workflows)
    assert fa.SYNONYM_GROUPS, "synonyms.json did not load"


def test_what_a_week_covers_is_its_agenda_in_the_shipped_synonyms():
    """The Syllabus never says "cover", so "what does each week cover" found it only through this group."""
    assert "agenda" in fa.synonym_map()[fa.stem("cover")] and fa.stem("covers") == fa.stem("covered") == fa.stem("cover")

