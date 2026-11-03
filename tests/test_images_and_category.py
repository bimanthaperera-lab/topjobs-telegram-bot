"""IT-category search, job ad images (download once, reuse Telegram file_id)."""

from types import SimpleNamespace

import httpx
from telegram.error import BadRequest

from jobbot.bot import send_job
from jobbot.images import NO_IMAGE_RECHECK_SECS, JobImages
from jobbot.models import Job
from jobbot.sources import SourceError
from jobbot.sources.http import PoliteHttpClient
from jobbot.sources.topjobs import TopJobsSource, find_ad_image_url

from .conftest import FakeSource, make_jobs, make_service

JOB_PAGE = """<html><body>
<img src="/logo/defzzz_small.gif">
<div id="remark" class="job-holder"><p><img alt="" src="/logo/DEFZZZ/1657c1255.png" style="width:895px"></p></div>
</body></html>"""
JOB_URL = "https://www.topjobs.lk/employer/JobAdvertismentServlet?rid=0&ac=DEFZZZ&jc=1&ec=DEFZZZ"


def test_finds_ad_image_inside_remark_only():
    assert find_ad_image_url(JOB_PAGE, JOB_URL) == "https://www.topjobs.lk/logo/DEFZZZ/1657c1255.png"
    assert find_ad_image_url("<div id='remark'><p>Text only ad</p></div>", JOB_URL) is None
    assert find_ad_image_url("<html></html>", JOB_URL) is None


def client(handler):
    return PoliteHttpClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), 0)


async def test_category_mode_fetches_category_not_keyword():
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, text="<html>vacancybyfunctionalarea</html>")

    source = TopJobsSource(client(handler), category="SDQ")
    assert source.keyword_independent
    await source.fetch("react developer", 10)
    assert seen["params"] == {"FA": "SDQ"}
    assert not TopJobsSource(client(handler)).keyword_independent


async def test_fetch_image_downloads_page_then_image():
    requested = []

    def handler(request):
        requested.append(request.url.path)
        if request.url.path.endswith(".png"):
            return httpx.Response(200, content=b"\x89PNG-bytes", headers={"content-type": "image/png"})
        return httpx.Response(200, text=JOB_PAGE)

    data = await TopJobsSource(client(handler)).fetch_image(JOB_URL)
    assert data == b"\x89PNG-bytes"
    assert requested == ["/employer/JobAdvertismentServlet", "/logo/DEFZZZ/1657c1255.png"]


async def test_fetch_image_rejects_non_images():
    def handler(request):
        if request.url.path.endswith(".png"):
            return httpx.Response(200, text="<html>error</html>", headers={"content-type": "text/html"})
        return httpx.Response(200, text=JOB_PAGE)

    assert await TopJobsSource(client(handler)).fetch_image(JOB_URL) is None


async def test_one_category_fetch_serves_every_keyword(storage):
    jobs = [Job(url=f"https://x.lk/{i}", title=t, source="Site A")
            for i, t in enumerate(["React Developer", "QA Engineer", "Graphic Designer"])]
    source = FakeSource(jobs=jobs)
    source.keyword_independent = True
    service = make_service(storage, source, rank_results=True)

    react = await service.search(1, "react")
    qa = await service.search(1, "qa")
    assert [j.title for j in react.first_page] == ["React Developer"]
    assert [j.title for j in qa.first_page] == ["QA Engineer"]
    assert source.calls == 1  # second keyword came from the same cached category list


# --- JobImages -------------------------------------------------------------------------

class ImageSource(FakeSource):
    def __init__(self, image=b"img", fail=False):
        super().__init__("Site A")
        self.image = image
        self.image_fail = fail
        self.image_calls = 0

    async def fetch_image(self, job_url):
        self.image_calls += 1
        if self.image_fail:
            raise SourceError("down")
        return self.image


JOB = make_jobs(1, source="Site A")[0]


async def test_image_downloaded_once_then_file_id_reused(storage):
    source = ImageSource()
    images = JobImages(storage, [source])
    assert await images.photo_for(JOB) == b"img"
    images.remember(JOB.url, "FILE123")
    assert await images.photo_for(JOB) == "FILE123"
    assert source.image_calls == 1


async def test_job_without_image_is_rechecked_later(storage, clock):
    source = ImageSource(image=None)
    images = JobImages(storage, [source])
    assert await images.photo_for(JOB) is None
    assert await images.photo_for(JOB) is None
    assert source.image_calls == 1  # remembered "no image"
    clock.advance(NO_IMAGE_RECHECK_SECS + 1)
    await images.photo_for(JOB)
    assert source.image_calls == 2


async def test_image_errors_are_not_remembered(storage):
    source = ImageSource(fail=True)
    images = JobImages(storage, [source])
    assert await images.photo_for(JOB) is None
    source.image_fail = False
    assert await images.photo_for(JOB) == b"img"


async def test_images_disabled_or_unsupported_source(storage):
    assert await JobImages(storage, [ImageSource()], enabled=False).photo_for(JOB) is None
    assert await JobImages(storage, [FakeSource("Site A")]).photo_for(JOB) is None


LOGO = "https://cdn.example.lk/logo_1.jpg"


async def test_logo_used_when_there_is_no_ad_image(storage):
    job = Job(url="https://x.lk/1", title="Dev", source="Site A", logo_url=LOGO)
    assert await JobImages(storage, [FakeSource("Site A")]).photo_for(job) == LOGO
    assert await JobImages(storage, [ImageSource(image=None)]).photo_for(job) == LOGO
    assert await JobImages(storage, [ImageSource()], enabled=False).photo_for(job) is None


async def test_ad_image_preferred_over_logo(storage):
    job = Job(url="https://x.lk/1", title="Dev", source="Site A", logo_url=LOGO)
    assert await JobImages(storage, [ImageSource()]).photo_for(job) == b"img"


# --- sending a job as a photo ------------------------------------------------------------

class PhotoBot:
    def __init__(self, reject_photo=False):
        self.sent = []
        self.reject_photo = reject_photo

    async def send_photo(self, chat_id, photo, **kwargs):
        if self.reject_photo:
            raise BadRequest("Photo_invalid_dimensions")
        self.sent.append(SimpleNamespace(kind="photo", photo=photo, **kwargs))
        return SimpleNamespace(photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="BIG")])

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(SimpleNamespace(kind="text", text=text, **kwargs))


async def test_job_sent_as_photo_with_caption_and_button(storage):
    images = JobImages(storage, [ImageSource()])
    bot = PhotoBot()
    job = Job(url="https://x.lk/1", title="Backend Developer", company="Acme", source="Site A")
    await send_job(bot, 42, job, 1, images)
    (msg,) = bot.sent
    assert msg.kind == "photo" and msg.photo == b"img"
    assert msg.caption.startswith("<b>1. Backend Developer")
    assert "<a href" not in msg.caption
    assert msg.reply_markup.inline_keyboard[0][0].url == "https://x.lk/1"
    # The largest size's file_id is stored for reuse.
    assert storage.get_job_photo("https://x.lk/1")[0] == "BIG"


async def test_job_sent_with_logo_and_short_description(storage):
    bot = PhotoBot()
    job = Job(url="https://x.lk/1", title="Dev", description="Full-Time. " + "D" * 400,
              source="Site A", logo_url=LOGO)
    await send_job(bot, 42, job, 1, JobImages(storage, [FakeSource("Site A")]))
    (msg,) = bot.sent
    assert msg.kind == "photo" and msg.photo == LOGO
    assert "📝 Full-Time. DDD" in msg.caption and msg.caption.endswith("…")
    assert storage.get_job_photo("https://x.lk/1") is None  # a logo isn't the job's ad image


async def test_rejected_photo_falls_back_to_text(storage):
    bot = PhotoBot(reject_photo=True)
    await send_job(bot, 42, Job(url="https://x.lk/1", title="Dev", source="Site A"), 1,
                   JobImages(storage, [ImageSource()]))
    (msg,) = bot.sent
    assert msg.kind == "text"


async def test_too_long_caption_falls_back_to_text(storage):
    bot = PhotoBot()
    job = Job(url="https://x.lk/1", title="T" * 300, company="C" * 150, location="L" * 150,
              posted="P" * 150, closing="X" * 150, description="D" * 200, source="Site A")
    await send_job(bot, 42, job, 1, JobImages(storage, [ImageSource()]))
    (msg,) = bot.sent
    assert msg.kind == "text"
