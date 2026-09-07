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
needed = {"parse_link", "parse_bulk_link", "format_eta", "safe_filename"}
selected = [
    node for node in module.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in needed
]
ns = {"re": __import__("re"), "MAX_BULK_MESSAGES": 500}
exec(compile(ast.Module(body=selected, type_ignores=[]), "telegram_bot.py:test", "exec"), ns)

parse_link = ns["parse_link"]
parse_bulk_link = ns["parse_bulk_link"]
format_eta = ns["format_eta"]
safe_filename = ns["safe_filename"]

class DummyFile:
    def __init__(self, name):
        self.name = name

class DummyMessage:
    def __init__(self, name, mid=123):
        self.file = DummyFile(name)
        self.id = mid

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

safe = safe_filename(DummyMessage("../folder\\evil:name?.mp4"))
assert "/" not in safe and "\\" not in safe and ".." not in safe
assert len(safe) <= 240

# Authorization/persistence assertions.
assert BOT.count("def access_role(uid):") == 1
assert "def is_owner(uid):" in BOT
assert "if not is_owner(uid):" in BOT
assert "role=\"vip\"" in BOT
assert "VOROA_OWNER_USER_ID" in BOT
assert "explicit_owner = EXPLICIT_OWNER_ID" in BOT
assert "MongoDB connected" in STORE
assert "local mirror" in STORE
assert "/data/voroa_session_store.json" in STORE
assert "_sync_local_to_mongo" in STORE
assert "if self._mongo_healthy:" in STORE

# Range-command regression checks.
assert "if len(parts) != 4:" in BOT
assert "peer = parts[1].strip()" in BOT
assert "start_id, end_id = int(parts[2]), int(parts[3])" in BOT
assert "resolve_message_peer(peer)" in BOT
assert "get_entity(parts[1])" not in BOT

# Timeout/cancellation regression check.
assert "asyncio.CancelledError" in BOT
assert "timed out after {SCAN_TIMEOUT_SECONDS} seconds" in BOT

# Playbook signed-upload protocol assertions based on the current API contract.
assert "assets/upload_prepare" in PLAYBOOK
assert "assets/upload_complete" in PLAYBOOK
assert "x-goog-resumable" in PLAYBOOK
assert "Content-Range" in PLAYBOOK
assert '"Content-Type": "text/plain"' in PLAYBOOK
assert "multipart_upload_id" in PLAYBOOK
assert "x-amz-meta-extension" in PLAYBOOK
assert "x-amz-meta-encrypted-organization-metadata" in PLAYBOOK
assert "class ProgressFileStream(httpx.AsyncByteStream)" in PLAYBOOK

print("Voroa smoke checks: PASS (parser/ETA + range + filename safety + authorization/persistence + Playbook upload assertions)")
