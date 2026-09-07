from pathlib import Path
p = Path('playbook_client.py')
s = p.read_text(encoding='utf-8')
old = '    async def upload_file(self, file_path: str | Path, title: str | None = None) -> str:\n'
new = '    async def upload_file(self, file_path: str | Path, title: str | None = None, progress_callback=None) -> str:\n'
if old in s:
    s = s.replace(old, new, 1)
if 'if progress_callback:\n            progress_callback(size, size)' not in s:
    s = s.replace('        return str(token)\n\n    async def get_asset', '        if progress_callback:\n            progress_callback(size, size)\n        return str(token)\n\n    async def get_asset', 1)
p.write_text(s, encoding='utf-8')
assert 'progress_callback=None' in s
print('Playbook progress callback compatibility applied.')
