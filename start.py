import asyncio
import os
import runpy

# Voroa uses the shorter environment variable names. Map them to the names
# expected by the Python application.
aliases = {
    "TELEGRAM_API_ID": "API_ID",
    "TELEGRAM_API_HASH": "API_HASH",
    "TELEGRAM_BOT_TOKEN": "BOT_TOKEN",
}
for target, source in aliases.items():
    if not os.getenv(target) and os.getenv(source):
        os.environ[target] = os.environ[source]

for name in (
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USER_IDS",
    "TELEGRAM_SESSION_DB_URI",
    "PLAYBOOK_API_TOKEN",
    "PLAYBOOK_ORG_SLUG",
):
    print(f"[Voroa] {name}: {'set' if os.getenv(name) else 'MISSING'}", flush=True)

# The standalone Telegram application contains the complete bot UI and
# persistent reply-keyboard menu.
app = runpy.run_path("telegram_bot.py", run_name="hj_groups_app")
main = app["main"]

if __name__ == "__main__":
    asyncio.run(main())
