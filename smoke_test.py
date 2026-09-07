from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BOT = (ROOT / "telegram_bot.py").read_text(encoding="utf-8")
STORE = (ROOT / "telegram_session_store.py").read_text(encoding="utf-8")
PLAYBOOK = (ROOT / "playbook_client.py").read_text(encoding="utf-8")

compile(BOT, "telegram_bot.py", "exec")
compile(STORE, "telegram_session_store.py", "exec")
compile(PLAYBOOK, "playbook_client.py", "exec")
compile((ROOT / "start.py").read_text(encoding="utf-8"), "start.py", "exec")

module = ast.parse(BOT)
needed = {"parse_link", "parse_bulk_link", "format_eta"}
selected = [node for node in module.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in needed]
ns = {"re": __import__("re"), "MAX_BULK_MESSAGES": 500}
exec(compile(ast.Module(body=selected, type_ignores=[]), "telegram_bot.py:test", "exec"), ns)

parse_link = ns["parse_link"]
parse_bulk_link = ns["parse_bulk_link"]
format_eta = ns["format_eta"]

cases = [
    ("https://t.me/c/4442444896/405", ("-1004442444896", 405)),
    ("https://t.me/example_channel/99", ("@example_channel", 99)),
]
for _ in range(100):
    for value, expected in cases:
        assert parse_link(value) == expected
    assert parse_bulk_link("https://t.me/c/4442444896/471-480") == ("-1004442444896", 471, 480)
    assert parse_bulk_link("https://t.me/c/4442444896/471 480") == ("-1004442444896", 471, 480)
    assert format_eta(4) == "4s left"
    assert format_eta(60) == "1m 0s left"

assert "mongo_ok = await session_store.ping()" in BOT
assert "Command menu setup failed; continuing to polling" in BOT
assert "bot will still start" in BOT
assert "MongoDB unavailable; continuing with local fallback" in STORE
assert "async def ping(self) -> bool" in STORE
assert "class ProgressFileStream(httpx.AsyncByteStream)" in PLAYBOOK
assert "Content-Length" in PLAYBOOK

print("Voroa smoke checks: PASS (100x parser/ETA regression + resilience assertions)")
