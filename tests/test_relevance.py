"""Relevance ranking of job titles against the user's keyword."""

from jobbot.models import Job
from jobbot.relevance import keyword_terms, rank_jobs, word_matches

from .conftest import FakeSource, make_service


def jobs_from(titles):
    return [Job(url=f"https://x.lk/{i}", title=t, source="TopJobs.lk") for i, t in enumerate(titles)]


def titles(jobs):
    return [j.title for j in jobs]


# Shaped like TopJobs' real answer to "software engineer": newest first, mostly noise.
SOFTWARE_ENGINEER_PAGE = [
    "A/C Technician / General Technician / Motor Mechanic",
    "Assistant Engineer | Technical Executive - Telecommunications",
    "Engineering Assistant",
    "Senior Software Engineer - .NET",
    "Marketing Engineer",
    "Auto Electrical Engineer",
    "Full Stack Mobile App Developer (Flutter + Node.js)",
    "Backend Software Engineer",
    "Software Sales Consultant",
    "Cloud Engineer - UAE",
    "Head of Engineering",
    "Senior Software Developer",
    "Civil Engineer",
    "Mechanical Engineer",
    "Electrical Engineer",
    "Quantity Surveyor",
]


def test_software_engineer_real_matches_first_noise_dropped():
    ranked = titles(rank_jobs(jobs_from(SOFTWARE_ENGINEER_PAGE), "software engineer"))
    # Both words, newest first.
    assert ranked[:2] == ["Senior Software Engineer - .NET", "Backend Software Engineer"]
    # "software" + synonym of engineer next.
    assert ranked[2] == "Senior Software Developer"
    # The noise from the screenshot is gone.
    for noise in ("A/C Technician / General Technician / Motor Mechanic", "Marketing Engineer",
                  "Engineering Assistant", "Civil Engineer", "Quantity Surveyor"):
        assert noise not in ranked


def test_single_word_keeps_site_order():
    page = ["Assistant Accountant", "Accountant", "Driver", "Senior Accountant"]
    assert titles(rank_jobs(jobs_from(page), "accountant")) == ["Assistant Accountant", "Accountant", "Senior Accountant"]


def test_plurals_and_word_forms_match():
    page = ["Graphic Designers & Video Editors", "Graphic Design Intern", "Web Developer"]
    ranked = titles(rank_jobs(jobs_from(page), "graphic designer"))
    assert ranked == ["Graphic Designers & Video Editors", "Graphic Design Intern"]


def test_synonyms():
    page = ["Accounts Executive", "Sales Executive", "Accounting Officer"]
    assert titles(rank_jobs(jobs_from(page), "accountant")) == ["Accounts Executive", "Accounting Officer"]


def test_exact_word_beats_synonym():
    page = ["Software Developer", "Software Engineer"]
    assert titles(rank_jobs(jobs_from(page), "software engineer")) == ["Software Engineer", "Software Developer"]


def test_short_terms_need_exact_match():
    assert word_matches("it", "it")
    assert not word_matches("italian", "it")
    assert not word_matches("quality", "qa")
    page = ["IT Executive", "Italian Chef", "Item Controller"]
    assert titles(rank_jobs(jobs_from(page), "it executive")) == ["IT Executive"]


def test_stop_words_ignored():
    assert keyword_terms("Jobs for a Driver") == ["driver"]
    assert keyword_terms("driver driver") == ["driver"]


def test_keyword_of_only_stop_words_returns_input_unchanged():
    page = jobs_from(["A", "B"])
    assert rank_jobs(page, "jobs") == page


def test_nothing_relevant_returns_empty():
    assert rank_jobs(jobs_from(["Chef", "Cashier"]), "software engineer") == []


def test_c_sharp_and_cpp_tokens():
    page = ["C# Developer", "C++ Engineer", "Chef"]
    assert titles(rank_jobs(jobs_from(page), "c# developer")) == ["C# Developer"]


def test_job_word_is_required():
    page = ["Software Sales Consultant", "Software Engineer", "Manager - Software Sales"]
    assert titles(rank_jobs(jobs_from(page), "software engineer")) == ["Software Engineer"]


def test_falls_back_to_job_word_when_nothing_matches_fully():
    # Real TopJobs case: no "web developer" titles, but developer jobs exist.
    page = ["Internal Auditor (Weboda)", "Web Project Manager (BPO)", "Backend Developer",
            "Civil Engineer", "Full Stack Mobile App Developer"]
    ranked = titles(rank_jobs(jobs_from(page), "web developer"))
    assert ranked == ["Backend Developer", "Full Stack Mobile App Developer"]


def test_only_real_word_endings_count():
    assert word_matches("engineering", "engineer")
    assert word_matches("designers", "designer")
    assert word_matches("design", "designer")
    assert not word_matches("weboda", "web")
    assert not word_matches("website", "web")
    assert not word_matches("soft", "software")


def test_weak_extra_word_is_not_enough():
    # "senior" is common, "software" is rare: Senior Civil Engineer must not pass.
    page = ["Senior Software Engineer", "Software Engineer", "Senior Civil Engineer",
            "Senior Accountant", "Senior Manager", "Senior Executive"]
    ranked = titles(rank_jobs(jobs_from(page), "senior software engineer"))
    assert ranked == ["Senior Software Engineer", "Software Engineer"]


def test_synonyms_are_one_way():
    page = ["Civil Engineer", "Backend Developer"]
    assert titles(rank_jobs(jobs_from(page), "developer")) == ["Backend Developer"]
    assert titles(rank_jobs(jobs_from(page), "engineer")) == ["Civil Engineer", "Backend Developer"]


def test_place_name_in_keyword_does_not_hide_results():
    page = ["Accountant", "Senior Accountant", "Driver"]
    assert titles(rank_jobs(jobs_from(page), "accountant colombo")) == ["Accountant", "Senior Accountant"]


# --- applied inside the search service ----------------------------------------

async def test_service_ranks_before_trimming(storage):
    """A good match deep in the site's list must survive the per-site cap."""
    noise = [f"Mechanical Engineer {i}" for i in range(60)]
    page = noise + ["Software Engineer"]  # the only real match is last
    source = FakeSource(jobs=jobs_from(page))
    service = make_service(storage, source, rank_results=True, jobs_per_source=30)
    result = await service.search(1, "software engineer")
    assert titles(result.first_page) == ["Software Engineer"]


async def test_service_asks_site_for_more_than_it_keeps(storage):
    seen = {}

    class Recording(FakeSource):
        async def fetch(self, keyword, limit):
            seen["limit"] = limit
            return await super().fetch(keyword, limit)

    service = make_service(storage, Recording(jobs=jobs_from(["Driver"])), rank_results=True)
    await service.search(1, "driver")
    assert seen["limit"] >= 500


async def test_ranking_disables_early_stop_but_never_repeats(storage):
    page = jobs_from(["Software Engineer A", "Chef", "Software Engineer B", "Software Engineer C"])
    source = FakeSource(jobs=page)
    service = make_service(storage, source, rank_results=True, cache_ttl_secs=0)
    first = await service.search(1, "software engineer")
    service.record_sent(1, "software engineer", first.first_page)
    again = await service.search(1, "software engineer")
    assert again.first_page == []  # all three already sent; chef never shown
