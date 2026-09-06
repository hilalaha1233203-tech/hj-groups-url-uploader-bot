"""Run locally once to create a Telethon StringSession for hosted deployment.

Never commit the printed session string or share it in chat. Treat it like a
password: anyone holding it may be able to operate the Telegram account.
"""

import os

from telethon import TelegramClient
from telethon.sessions import StringSession

api_id = int(os.environ["TELEGRAM_API_ID"])
api_hash = os.environ["TELEGRAM_API_HASH"]
phone = os.environ["TELEGRAM_PHONE_NUMBER"]

client = TelegramClient(StringSession(), api_id, api_hash)
client.start(phone=phone)
print("\nTELEGRAM_SESSION_STRING=\n")
print(client.session.save())
print("\nStore this value only in your hosting provider's secret environment variable.")
client.disconnect()
