"""IT-category search, job ad images (download once, reuse Telegram file_id)."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from telegram.error import BadRequest, Forbidden, NetworkError, TimedOut

from jobbot.bot import send_job, send_jobs
from jobbot.images import NO_IMAGE_RECHECK_SECS, JobImages, Photo
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


def test_ad_image_url_spaces_are_encoded():
    page = '<div id="remark"><img src="/logo/0000000375/4593cAI Transformation.png"></div>'
    assert find_ad_image_url(page, JOB_URL) == "https://www.topjobs.lk/logo/0000000375/4593cAI%20Transformation.png"


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


async def test_ad_image_url_needs_only_the_job_page():
    requested = []

    def handler(request):
        requested.append(request.url.path)
        return httpx.Response(200, text=JOB_PAGE)

    url = await TopJobsSource(client(handler)).ad_image_url(JOB_URL)
    assert url == "https://www.topjobs.lk/logo/DEFZZZ/1657c1255.png"
    assert requested == ["/employer/JobAdvertismentServlet"]


async def test_download_image():
    def handler(request):
        if request.url.path.endswith(".png"):
            return httpx.Response(200, content=b"\x89PNG-bytes", headers={"content-type": "image/png"})
        return httpx.Response(200, text="<html>error</html>", headers={"content-type": "text/html"})

    source = TopJobsSource(client(handler))
    assert await source.download_image("https://www.topjobs.lk/logo/DEFZZZ/1657c1255.png") == b"\x89PNG-bytes"
    assert await source.download_image("https://www.topjobs.lk/oops") is None  # not an image


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

IMG_URL = "https://x.lk/ad.png"


class ImageSource(FakeSource):
    def __init__(self, image_url=IMG_URL, fail=False, data=b"img"):
        super().__init__("Site A")
        self.image_url = image_url
        self.image_fail = fail
        self.data = data
        self.image_calls = 0
        self.downloads = 0

    async def ad_image_url(self, job_url):
        self.image_calls += 1
        if self.image_fail:
            raise SourceError("down")
        return self.image_url

    async def download_image(self, image_url):
        self.downloads += 1
        return self.data


JOB = make_jobs(1, source="Site A")[0]
AD = Photo(IMG_URL, is_ad_url=True)


async def test_ad_image_url_found_once_then_file_id_reused(storage):
    source = ImageSource()
    images = JobImages(storage, [source])
    assert await images.photo_for(JOB) == AD
    images.remember(JOB.url, "FILE123")
    assert await images.photo_for(JOB) == Photo("FILE123")
    assert source.image_calls == 1


async def test_job_without_image_is_rechecked_later(storage, clock):
    source = ImageSource(image_url=None)
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
    assert await images.photo_for(JOB) == AD


async def test_images_disabled_or_unsupported_source(storage):
    assert await JobImages(storage, [ImageSource()], enabled=False).photo_for(JOB) is None
    assert await JobImages(storage, [FakeSource("Site A")]).photo_for(JOB) is None


LOGO = "https://cdn.example.lk/logo_1.jpg"


async def test_logo_used_when_there_is_no_ad_image(storage):
    job = Job(url="https://x.lk/1", title="Dev", source="Site A", logo_url=LOGO)
    assert await JobImages(storage, [FakeSource("Site A")]).photo_for(job) == Photo(LOGO)
    assert await JobImages(storage, [ImageSource(image_url=None)]).photo_for(job) == Photo(LOGO)
    assert await JobImages(storage, [ImageSource()], enabled=False).photo_for(job) is None


async def test_ad_image_preferred_over_logo(storage):
    job = Job(url="https://x.lk/1", title="Dev", source="Site A", logo_url=LOGO)
    assert await JobImages(storage, [ImageSource()]).photo_for(job) == Photo(IMG_URL, is_ad_url=True, logo=LOGO)


# --- sending a job as a photo ------------------------------------------------------------

class PhotoBot:
    def __init__(self, reject_photo=False, reject_urls=False):
        self.sent = []
        self.reject_photo = reject_photo
        self.reject_urls = reject_urls  # like Telegram failing to fetch a URL itself

    async def send_photo(self, chat_id, photo, **kwargs):
        if self.reject_photo or (self.reject_urls and isinstance(photo, str) and photo.startswith("https://")):
            raise BadRequest("Wrong file identifier/http url specified")
        self.sent.append(SimpleNamespace(kind="photo", photo=photo, **kwargs))
        return SimpleNamespace(photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="BIG")])

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(SimpleNamespace(kind="text", text=text, **kwargs))


async def test_job_sent_as_photo_url_with_caption_and_button(storage):
    source = ImageSource()
    bot = PhotoBot()
    job = Job(url="https://x.lk/1", title="Backend Developer", company="Acme", source="Site A")
    await send_job(bot, 42, job, 1, JobImages(storage, [source]))
    (msg,) = bot.sent
    assert msg.kind == "photo" and msg.photo == IMG_URL  # Telegram fetches it; nothing downloaded here
    assert source.downloads == 0
    assert msg.caption.startswith("<b>1. Backend Developer")
    assert "<a href" not in msg.caption
    assert msg.reply_markup.inline_keyboard[0][0].url == "https://x.lk/1"
    # The largest size's file_id is stored for reuse.
    assert storage.get_job_photo("https://x.lk/1")[0] == "BIG"


async def test_image_downloaded_when_telegram_cannot_fetch_the_url(storage):
    source = ImageSource()
    bot = PhotoBot(reject_urls=True)
    await send_job(bot, 42, Job(url="https://x.lk/1", title="Dev", source="Site A"), 1, JobImages(storage, [source]))
    (msg,) = bot.sent
    assert msg.kind == "photo" and msg.photo == b"img"
    assert source.downloads == 1
    assert storage.get_job_photo("https://x.lk/1")[0] == "BIG"


async def test_logo_used_after_ad_image_fails(storage):
    class UrlOnlySource(FakeSource):
        async def ad_image_url(self, job_url):
            return IMG_URL

    class AdUrlRejectingBot(PhotoBot):
        async def send_photo(self, chat_id, photo, **kwargs):
            if photo == IMG_URL:
                raise BadRequest("Wrong type of the web page content")
            return await super().send_photo(chat_id, photo, **kwargs)

    bot = AdUrlRejectingBot()
    job = Job(url="https://x.lk/1", title="Dev", source="Site A", logo_url=LOGO)
    await send_job(bot, 42, job, 1, JobImages(storage, [UrlOnlySource("Site A")]))
    (msg,) = bot.sent
    assert msg.kind == "photo" and msg.photo == LOGO
    assert storage.get_job_photo(job.url)[0] is None  # recorded: no ad image
    images = JobImages(storage, [UrlOnlySource("Site A")])
    assert await images.photo_for(job) == Photo(LOGO)  # the ad image isn't tried again until the recheck


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


async def test_photo_timeout_falls_back_and_the_rest_still_sent(storage):
    """Telegram still fetching a photo URL at the timeout must not stop the page (it used to
    end the search after the jobs before it)."""
    class FlakyBot(PhotoBot):
        async def send_photo(self, chat_id, photo, **kwargs):
            assert kwargs["read_timeout"] >= 30
            if photo == IMG_URL:
                raise TimedOut()
            if kwargs["caption"].startswith("<b>2. "):
                raise NetworkError("Bad Gateway")
            return await super().send_photo(chat_id, photo, **kwargs)

        async def send_message(self, chat_id, text, **kwargs):
            if text.startswith("<b>2. "):
                raise NetworkError("Bad Gateway")
            await super().send_message(chat_id, text, **kwargs)

    jobs = make_jobs(3, source="Site A")
    bot = FlakyBot()
    await send_jobs(bot, 42, jobs, 1, JobImages(storage, [ImageSource()]))
    # Jobs 1 and 3: the ad image URL timed out, so it was downloaded and uploaded instead.
    # Job 2: nothing could be sent, and job 3 still was.
    assert [(m.kind, m.photo, m.caption.split("</b>")[0]) for m in bot.sent] == [
        ("photo", b"img", "<b>1. " + jobs[0].title), ("photo", b"img", "<b>3. " + jobs[2].title)]


async def test_blocked_user_stops_sending(storage):
    class BlockedBot(PhotoBot):
        async def send_photo(self, chat_id, photo, **kwargs):
            raise Forbidden("bot was blocked by the user")

    with pytest.raises(Forbidden):
        await send_jobs(BlockedBot(), 42, make_jobs(3, source="Site A"), 1, JobImages(storage, [ImageSource()]))


async def test_too_long_caption_falls_back_to_text(storage):
    bot = PhotoBot()
    job = Job(url="https://x.lk/1", title="T" * 300, company="C" * 150, location="L" * 150,
              posted="P" * 150, closing="X" * 150, description="D" * 200, source="Site A")
    await send_job(bot, 42, job, 1, JobImages(storage, [ImageSource()]))
    (msg,) = bot.sent
    assert msg.kind == "text"


async def test_photo_lookups_start_before_the_first_send(storage):
    started = []
    release = asyncio.Event()

    class SlowSource(ImageSource):
        async def ad_image_url(self, job_url):
            started.append(job_url)
            await release.wait()
            return IMG_URL

    jobs = make_jobs(3, source="Site A")
    bot = PhotoBot()
    sending = asyncio.create_task(send_jobs(bot, 42, jobs, 1, JobImages(storage, [SlowSource()])))
    for _ in range(3):
        await asyncio.sleep(0)
    assert started == [j.url for j in jobs]  # every lookup running while the first job waits
    release.set()
    await sending
    assert [m.caption.split("</b>")[0] for m in bot.sent] == ["<b>1. " + jobs[0].title, "<b>2. " + jobs[1].title,
                                                               "<b>3. " + jobs[2].title]
