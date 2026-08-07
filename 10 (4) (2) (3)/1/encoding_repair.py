import json
import os
import re
import tempfile
from pathlib import Path

from utils import (
    DOWNLOAD_INDEX_FILE, load_app_state, repair_mojibake,
    sanitize_filename, update_app_state,
)


IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}


def _write_json_atomic(path, data):
    path = Path(path)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        Path(temp_name).replace(path)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def _status_suffix(name):
    match = re.search(r'( \[(?:完整|缺失\d+页)\](?: \(\d+\))?)$', name)
    return match.group(1) if match else ''


def _recover_title(info):
    old_title = str(info.get('title', ''))
    full_title = repair_mojibake(str(info.get('full_title', '')))
    repaired_title = repair_mojibake(old_title)
    if full_title and full_title != info.get('full_title'):
        prefix = re.match(r'^(\[[^\]]+\]\s*)', old_title)
        if prefix and not full_title.startswith(prefix.group(1)):
            full_title = prefix.group(1) + full_title
        return full_title
    return repaired_title


def repair_existing_download_names(output_dir):
    output_dir = Path(output_dir)
    index_path = output_dir / DOWNLOAD_INDEX_FILE
    try:
        index = json.loads(index_path.read_text(encoding='utf-8')) if index_path.exists() else {}
    except Exception:
        index = {}
    changes = []

    for directory in list(output_dir.iterdir()) if output_dir.exists() else []:
        info_path = directory / 'info.json'
        if not directory.is_dir() or not info_path.exists():
            continue
        try:
            info = json.loads(info_path.read_text(encoding='utf-8'))
        except Exception:
            continue
        gallery_id = str(info.get('id', ''))
        old_title = str(info.get('title', ''))
        new_title = sanitize_filename(_recover_title(info))
        if not gallery_id or not new_title or new_title == old_title:
            continue

        before_count = sum(1 for path in directory.rglob('*')
                           if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES)
        suffix = _status_suffix(directory.name)
        new_name = f'{gallery_id}_{new_title}{suffix}'
        new_path = directory.parent / new_name
        if new_path.exists() and new_path != directory:
            raise FileExistsError(f'目标目录已存在: {new_path}')

        temp_path = directory.parent / f'.encoding-repair-{gallery_id}.tmp'
        directory.rename(temp_path)
        temp_path.rename(new_path)

        info['title'] = new_title
        if info.get('full_title'):
            info['full_title'] = repair_mojibake(info['full_title'])
        if info.get('title_jp'):
            info['title_jp'] = repair_mojibake(info['title_jp'])
        _write_json_atomic(new_path / 'info.json', info)

        text_path = new_path / '画廊信息.txt'
        if text_path.exists():
            text = text_path.read_text(encoding='utf-8', errors='replace')
            text = text.replace(old_title, new_title)
            text = repair_mojibake(text)
            text_path.write_text(text, encoding='utf-8')

        after_count = sum(1 for path in new_path.rglob('*')
                          if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES)
        if after_count != before_count:
            raise RuntimeError(f'{gallery_id} 迁移前后图片数量不一致')

        entry = index.setdefault(gallery_id, {})
        entry['title'] = new_title
        entry['path'] = str(new_path)
        entry['gallery_id'] = gallery_id
        changes.append({
            'id': gallery_id, 'old_path': str(directory), 'new_path': str(new_path),
            'title': new_title, 'images': after_count,
        })

    if changes:
        _write_json_atomic(index_path, index)

        def update_history(state):
            by_id = {change['id']: change for change in changes}
            for record in state['history']:
                change = by_id.get(str(record.get('id', '')))
                if change:
                    record['title'] = change['title']
                    record['path'] = change['new_path']

        update_app_state(update_history)
    return changes


if __name__ == '__main__':
    for change in repair_existing_download_names(Path(__file__).parent / 'downloads'):
        print(f'{change["id"]}: {change["title"]} ({change["images"]} images)')
