import os
import runpy

# Voroa currently uses the shorter environment variable names. The application
# keeps the explicit TELEGRAM_* names, so map the deployment aliases here.
aliases = {
    "TELEGRAM_API_ID": "API_ID",
    "TELEGRAM_API_HASH": "API_HASH",
    "TELEGRAM_BOT_TOKEN": "BOT_TOKEN",
}
for target, source in aliases.items():
    if not os.getenv(target) and os.getenv(source):
        os.environ[target] = os.environ[source]

runpy.run_path("telegram_chat_exporter.py", run_name="__main__")
