from pathlib import Path
p=Path('telegram_bot.py')
s=p.read_text(encoding='utf-8')
s=s.replace('@dp.message(Command("login"))\nasync def qr_login_flow(message: Message, uid: int):','async def qr_login_flow(message: Message, uid: int):',1)
p.write_text(s,encoding='utf-8')
print('fixed')
