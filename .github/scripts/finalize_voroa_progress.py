from pathlib import Path

p = Path('playbook_client.py')
s = p.read_text(encoding='utf-8')

if 'class ProgressFileStream(httpx.AsyncByteStream):' not in s:
    marker = '\n\nclass PlaybookClient:'
    stream = '''\n\nclass ProgressFileStream(httpx.AsyncByteStream):\n    def __init__(self, path: Path, total: int, callback=None, chunk_size: int = 1024 * 1024) -> None:\n        self.path = path\n        self.total = total\n        self.callback = callback\n        self.chunk_size = chunk_size\n\n    async def __aiter__(self):\n        sent = 0\n        with self.path.open("rb") as stream:\n            while True:\n                chunk = stream.read(self.chunk_size)\n                if not chunk:\n                    break\n                yield chunk\n                sent += len(chunk)\n                if self.callback:\n                    self.callback(sent, self.total)\n'''
    if marker not in s:
        raise SystemExit('PlaybookClient marker not found')
    s = s.replace(marker, stream + marker, 1)

old_gcs = '''                with path.open("rb") as stream:\n                    upload = await client.put(session_url, headers={"Content-Type": media_type}, content=stream)\n                upload.raise_for_status()\n'''
new_gcs = '''                upload_headers = {"Content-Type": media_type, "Content-Length": str(size)}\n                upload = await client.put(\n                    session_url,\n                    headers=upload_headers,\n                    content=ProgressFileStream(path, size, progress_callback),\n                )\n                upload.raise_for_status()\n'''
if old_gcs in s:
    s = s.replace(old_gcs, new_gcs, 1)

old_b2 = '''                with path.open("rb") as stream:\n                    for part in data["parts"]:\n                        part_number = int(part["part_number"])\n                        part_size = int(data["part_size"])\n                        stream.seek((part_number - 1) * part_size)\n                        chunk = stream.read(part_size)\n                        response = await client.put(part["url"], content=chunk)\n                        response.raise_for_status()\n'''
new_b2 = '''                with path.open("rb") as stream:\n                    uploaded = 0\n                    for part in data["parts"]:\n                        part_number = int(part["part_number"])\n                        part_size = int(data["part_size"])\n                        stream.seek((part_number - 1) * part_size)\n                        chunk = stream.read(part_size)\n                        response = await client.put(part["url"], content=chunk)\n                        response.raise_for_status()\n                        uploaded += len(chunk)\n                        if progress_callback:\n                            progress_callback(uploaded, size)\n'''
if old_b2 in s:
    s = s.replace(old_b2, new_b2, 1)

old_else = '''                with path.open("rb") as stream:\n                    upload = await client.put(upload_url, headers=headers, content=stream)\n                upload.raise_for_status()\n'''
new_else = '''                upload_headers = dict(headers)\n                upload_headers["Content-Length"] = str(size)\n                upload = await client.put(\n                    upload_url,\n                    headers=upload_headers,\n                    content=ProgressFileStream(path, size, progress_callback),\n                )\n                upload.raise_for_status()\n'''
if old_else in s:
    s = s.replace(old_else, new_else, 1)

# completion callback remains useful when the upload reaches 100%.
if 'progress_callback=None' not in s:
    s = s.replace(
        '    async def upload_file(self, file_path: str | Path, title: str | None = None) -> str:\n',
        '    async def upload_file(self, file_path: str | Path, title: str | None = None, progress_callback=None) -> str:\n',
        1,
    )
p.write_text(s, encoding='utf-8')
assert 'class ProgressFileStream(httpx.AsyncByteStream):' in s
assert 'content=ProgressFileStream(path, size, progress_callback)' in s
assert 'progress_callback=None' in s
print('True Playbook upload progress applied.')
