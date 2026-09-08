import os

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "ci_test_hash")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ci_test_token")

import voroa_stable as v


def test_single_links():
    assert v.parse_single_link("https://t.me/c/123456789/42") == ("-100123456789", 42)
    assert v.parse_single_link("https://t.me/examplechannel/99") == ("@examplechannel", 99)


def test_ranges():
    assert v.parse_range("https://t.me/c/123456789/100-110") == ("-100123456789", 100, 110)
    assert v.parse_range("https://t.me/examplechannel/100 to 105") == ("@examplechannel", 100, 105)


def test_sizes():
    assert v.human_size(1024) == "1.0 KB"
    assert v.human_size(1024 * 1024) == "1.0 MB"
    assert v.human_size(1024 * 1024 * 1024) == "1.0 GB"


def test_filename_safety():
    class File:
        name = 'bad:/name*.mp4'

    class Message:
        id = 17
        file = File()

    assert '/' not in v.filename(Message())
    assert ':' not in v.filename(Message())


def test_authorization_defaults_to_private_list():
    original = set(v.ALLOWED_IDS)
    v.ALLOWED_IDS = {123}
    v.OWNER_ID = 456
    assert v.authorized(123)
    assert v.authorized(456)
    assert not v.authorized(789)
    v.ALLOWED_IDS = original


if __name__ == "__main__":
    test_single_links()
    test_ranges()
    test_sizes()
    test_filename_safety()
    test_authorization_defaults_to_private_list()
    print("Voroa stable core tests: PASS")
