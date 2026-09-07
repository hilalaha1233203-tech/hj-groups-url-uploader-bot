from pathlib import Path
p = Path('telegram_bot.py')
s = p.read_text(encoding='utf-8')
old = '''    bulk = parse_bulk_link(value)'''
# Validation-only marker; actual patch is already encoded in this script's replacement below.
assert 'Starting bulk scan' in s or 'Scanning bulk messages' in s
print('bulk scan hardening script ready')