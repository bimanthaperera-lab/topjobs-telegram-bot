"""IT job categories offered as buttons on /start and /categories.

Each category is a ready-made keyword search, so it is matched against the same cached
IT list as a typed keyword and costs no extra site requests. Keywords were picked against
real TopJobs titles: a category is only worth a button if its keyword finds jobs there
(e.g. "designer" finds ~35 of ~160 IT jobs; "network" found one, so it has no button).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    id: str       # short: it goes into callback_data (Telegram allows 64 bytes)
    label: str
    keyword: str  # already normalized: lowercase, single spaces


CATEGORIES: tuple[Category, ...] = (
    Category("swe", "💻 Software Engineer", "software engineer"),
    Category("dev", "👨‍💻 Developer", "developer"),
    Category("qa", "🧪 QA / Testing", "qa"),
    Category("devops", "⚙️ DevOps", "devops"),
    Category("cloud", "☁️ Cloud", "cloud"),
    Category("uiux", "🖌 UI / UX", "ui ux"),
    Category("design", "🎨 Graphic Design", "designer"),
    Category("data", "📊 Data", "data"),
    Category("ai", "🤖 AI", "ai"),
    Category("sec", "🔐 Cyber Security", "security"),
    Category("pm", "📋 Project Manager", "project manager"),
    Category("intern", "🎓 Internships", "intern"),
)

_BY_ID = {c.id: c for c in CATEGORIES}


def category_by_id(category_id: str) -> Category | None:
    return _BY_ID.get(category_id)
