from __future__ import annotations

import os

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "ci_test_hash")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ci_test_token")

import voroa_stable as v


def test_single_links() -> None:
    assert v.parse_single_link("https://t.me/c/123456789/42") == ("-100123456789", 42)
    assert v.parse_single_link("https://t.me/examplechannel/99") == ("@examplechannel", 99)


def test_ranges() -> None:
    assert v.parse_range("https://t.me/c/123456789/100-110") == ("-100123456789", 100, 110)
    assert v.parse_range("https://t.me/examplechannel/100 to 105") == ("@examplechannel", 100, 105)


def test_sizes() -> None:
    assert v.human_size(1024) == "1.0 KB"
    assert v.human_size(1024 * 1024) == "1.0 MB"
    assert v.human_size(1024 * 1024 * 1024) == "1.0 GB"


def test_filename_safety() -> None:
    class File:
        name = 'bad:/name*.mp4'

    class Message:
        id = 17
        file = File()

    assert "/" not in v.filename(Message())
    assert ":" not in v.filename(Message())


def test_authorization() -> None:
    original = set(v.ALLOWED_IDS)
    original_owner = v.OWNER_ID
    try:
        v.ALLOWED_IDS = {123}
        v.OWNER_ID = 456
        assert v.authorized(123)
        assert v.authorized(456)
        assert not v.authorized(789)
    finally:
        v.ALLOWED_IDS = original
        v.OWNER_ID = original_owner


def test_confirm_ui_contract() -> None:
    keyboard = v.confirm_keyboard(123)
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert "confirm:123" in callbacks
    assert "job_cancel:123" in callbacks


def test_cancel_ui_contract() -> None:
    keyboard = v.inline_cancel(123)
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert callbacks == ["transfer_cancel:123"]


if __name__ == "__main__":
    test_single_links()
    test_ranges()
    test_sizes()
    test_filename_safety()
    test_authorization()
    test_confirm_ui_contract()
    test_cancel_ui_contract()
    print("Voroa smoke checks: PASS")
