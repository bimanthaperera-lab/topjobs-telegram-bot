"""IT job categories offered as buttons on /start and /categories.

A category is a list of title phrases, most wanted first. A job is shown if its title
has every word of any phrase (strict: no loose fallback), and all matches for the first
phrase come before the second's, so the best kind of job fills the first page. Phrases
are matched against the same cached IT list as a typed keyword, so a category costs no
extra site requests. They were checked against real TopJobs titles: e.g. plain
"support" also finds customer support jobs, so IT Support lists only IT phrases, and
Data leaves out data entry / data processing jobs.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    id: str                    # short: it goes into callback_data (Telegram allows 64 bytes)
    label: str
    phrases: tuple[str, ...]   # normalized (lowercase, single spaces), most wanted first


CATEGORIES: tuple[Category, ...] = (
    Category("swe", "💻 Software Engineer", ("software engineer",)),
    Category("dev", "👨‍💻 Developer", ("developer",)),
    Category("qa", "🧪 QA / Testing", ("qa", "quality assurance", "test automation", "tester")),
    Category("devops", "⚙️ DevOps", ("devops", "site reliability", "sre")),
    Category("cloud", "☁️ Cloud", ("cloud", "aws", "azure")),
    Category("uiux", "🖌 UI / UX", ("ui ux", "ux", "ui designer")),
    Category("design", "🎨 Graphic Design", ("graphic designer", "graphic design", "designer", "video editor")),
    Category("data", "📊 Data", ("data scientist", "data engineer", "data analyst", "analytics",
                                "business intelligence", "power bi", "data governance", "database")),
    Category("ai", "🤖 AI", ("ai", "machine learning", "ml")),
    Category("sec", "🔐 Cyber Security", ("cyber security", "security")),
    Category("support", "🛠 Help Desk / Support", ("help desk", "helpdesk", "service desk", "it support",
                                                  "technical support", "desktop support", "support engineer",
                                                  "application support", "technician")),
    Category("network", "🌐 Networking", ("network", "system administrator", "sysadmin", "infrastructure", "noc")),
    Category("pm", "📋 Project Manager", ("project manager", "project coordinator", "scrum master")),
    Category("intern", "🎓 Internships", ("intern", "internship", "trainee")),
)

_BY_ID = {c.id: c for c in CATEGORIES}


def category_by_id(category_id: str) -> Category | None:
    return _BY_ID.get(category_id)
