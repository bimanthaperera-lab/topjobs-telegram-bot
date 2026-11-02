import pytest

from jobbot.config import ConfigError, load_settings, parse_admin_ids_lenient

FAKE_TOKEN = "123456789:AAbbCCddEEffGGhhIIjjKKllMMnnOOppQQ"


@pytest.mark.parametrize(
    ("raw", "ids"),
    [
        ("", set()),
        ("123", {123}),
        ("1, 2;3", {1, 2, 3}),
        ("1 2\n3", {1, 2, 3}),
        ('"123456"', {123456}),
        ("[111, 222]", {111, 222}),
    ],
)
def test_valid_admin_ids(raw, ids):
    parsed, problems = parse_admin_ids_lenient(raw)
    assert parsed == ids and problems == []


def test_username_is_ignored_with_hint():
    ids, problems = parse_admin_ids_lenient("@bimantha, 42")
    assert ids == {42}
    assert len(problems) == 1 and "@username" in problems[0] and "userinfobot" in problems[0]


def test_bot_token_is_recognised_and_never_echoed():
    ids, problems = parse_admin_ids_lenient(FAKE_TOKEN)
    assert ids == set()
    assert "bot token" in problems[0]
    assert FAKE_TOKEN not in problems[0] and "123456789" not in problems[0]


def test_other_garbage_is_not_echoed():
    ids, problems = parse_admin_ids_lenient("my id")
    assert ids == set()
    assert len(problems) == 2
    assert all("not a number" in p and "my" not in p.split("entry")[1] for p in problems)


def _env(monkeypatch, **values):
    for key in ("TELEGRAM_BOT_TOKEN", "ADMIN_USER_IDS", "TOPJOBS_ENABLED", "XPRESSJOBS_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_bad_admin_ids_do_not_stop_the_bot(monkeypatch):
    _env(monkeypatch, TELEGRAM_BOT_TOKEN=FAKE_TOKEN, ADMIN_USER_IDS="@someone")
    settings = load_settings(env_file=None)
    assert settings.rate_limit.admin_user_ids == frozenset()
    assert len(settings.warnings) == 1


def test_missing_token_is_still_fatal(monkeypatch):
    _env(monkeypatch)
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        load_settings(env_file=None)


def test_no_sites_enabled_is_fatal(monkeypatch):
    _env(monkeypatch, TELEGRAM_BOT_TOKEN=FAKE_TOKEN, TOPJOBS_ENABLED="false")
    with pytest.raises(ConfigError, match="at least one job site"):
        load_settings(env_file=None)
