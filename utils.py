# -*- coding: utf-8 -*-
import sys
import re
import json
import unicodedata
import os
import tempfile
import threading
import logging
from contextlib import contextmanager
import time
from pathlib import Path

MAX_FILENAME_LEN = 180
TAG_OK = '[完整]'
TAG_FAIL_PREFIX = '[缺失'
TAG_FAIL_SUFFIX = '页]'
JM_COLLECTION_FILE = Path(__file__).parent / '新建文本文档.txt'
DOWNLOAD_INDEX_FILE = '.download_index.json'
APP_STATE_FILE = '.downloader_state.json'
_INDEX_LOCKS = {}
_INDEX_LOCKS_GUARD = threading.Lock()
_APP_STATE_LOCK = threading.RLock()
logger = logging.getLogger(__name__)


def _index_lock(output_dir):
    key = str(Path(output_dir).resolve())
    with _INDEX_LOCKS_GUARD:
        return _INDEX_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _process_index_lock(output_dir):
    """Serialize index updates across processes on Windows."""
    lock_path = Path(output_dir) / (DOWNLOAD_INDEX_FILE + '.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, 'a+b')
    try:
        try:
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b'0')
                handle.flush()
            acquired = False
            while not acquired:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    time.sleep(0.05)
        except ImportError:
            # POSIX callers still get the in-process lock below.
            pass
        yield
    finally:
        try:
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except (ImportError, OSError):
            pass
        handle.close()

ERROR_CN = {
    'ConnectionError': '网络连接失败，请检查网络或代理',
    'Timeout': '请求超时，请检查网络或更换代理',
    'ProxyError': '代理连接失败，请检查代理地址',
    'SSLError': 'SSL证书验证失败',
    'TooManyRedirects': '重定向次数过多',
    'ChunkedEncodingError': '数据传输中断',
    'ContentDecodingError': '内容解码失败',
    'ConnectionRefused': '连接被拒绝',
    'NameResolutionError': '域名解析失败',
}

HTTP_CN = {
    403: '访问被拒绝 (403)，可能需要更换代理',
    404: '页面不存在 (404)，资源可能已被删除',
    429: '请求过于频繁 (429)，请稍后再试',
    500: '服务器内部错误 (500)',
    502: '网关错误 (502)',
    503: '服务不可用 (503)',
    521: '拒绝连接 (521)，可能触发了反爬',
    522: '连接超时 (522)',
    523: '源站不可达 (523)',
    524: '超时 (524)',
}


def load_collection_ids(collection_file=None):
    if collection_file is None:
        collection_file = JM_COLLECTION_FILE
    else:
        collection_file = Path(collection_file)

    ids = []
    if collection_file.exists():
        with open(collection_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if line.isdigit():
                    ids.append(line)
                else:
                    match = re.search(r'/g/(\d+)', line)
                    if match:
                        ids.append(match.group(1))
    if not ids:
        if collection_file.exists():
            print(f'[警告] 合集文件 "{collection_file.name}" 为空，使用内置预设ID', file=sys.stderr)
        else:
            print(f'[警告] 合集文件 "{collection_file.name}" 不存在，使用内置预设ID', file=sys.stderr)
        ids = ['641734', '644868', '644485', '640276', '637978', '633629',
               '629937', '629936', '629935', '629933', '629934', '568638',
               '530743', '492966', '483197', '403166', '311324', '325247']
    return list(dict.fromkeys(ids))


def get_collection_desc(collection_file=None):
    ids = load_collection_ids(collection_file)
    return f'共 {len(ids)} 本'


def translate_error(error_str):
    for key, cn in ERROR_CN.items():
        if key.lower() in str(error_str).lower():
            return cn
    return str(error_str)


def translate_http_status(code):
    return HTTP_CN.get(code, f'HTTP错误 {code}')


def decode_utf8_response(response):
    """Decode known UTF-8 site responses without trusting HTTP's Latin-1 default."""
    content = getattr(response, 'content', None)
    if content:
        try:
            return content.decode('utf-8-sig')
        except UnicodeDecodeError:
            pass
    return str(getattr(response, 'text', '') or '')


def repair_mojibake(text):
    """Repair strict UTF-8-as-Latin-1 mojibake, leaving valid CJK text untouched."""
    if not isinstance(text, str) or not text:
        return text
    suspicious = any(marker in text for marker in (
        'Ã', 'Â', 'â', 'å', 'æ', 'ç', 'é', 'ð', '\x80', '\x81', '\x82',
        '\x83', '\x84', '\x85', '\x86', '\x87', '\x88', '\x89', '\x8a',
        '\x8b', '\x8c', '\x8d', '\x8e', '\x8f', '\x90', '\x91', '\x92',
        '\x93', '\x94', '\x95', '\x96', '\x97', '\x98', '\x99', '\x9a',
        '\x9b', '\x9c', '\x9d', '\x9e', '\x9f'))
    if not suspicious:
        return text
    try:
        repaired = text.encode('latin-1').decode('utf-8')
        if repaired.encode('utf-8').decode('latin-1') == text:
            return repaired
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return text


def classify_failure(error):
    """Return a stable failure code and a useful Chinese explanation."""
    text = str(error or '')
    lower = text.lower()
    rules = [
        ('cancelled', ('用户取消', 'cancelled', 'canceled'), '任务已由用户停止'),
        ('not_found', ('404', 'not found', '不存在'), '资源不存在或已被删除'),
        ('rate_limited', ('429', 'too many requests', '频繁'), '请求过于频繁，服务端正在限流'),
        ('blocked', ('403', 'cloudflare', 'captcha', 'turnstile', 'restricted access', '反爬'),
         '访问被站点防护拦截，建议降低并发或更换出口'),
        ('proxy', ('proxy', '代理'), '代理连接失败或代理出口不可用'),
        ('dns', ('nameresolution', 'name resolution', 'getaddrinfo', '域名解析'), '域名解析失败'),
        ('tls', ('ssl', 'certificate', 'tls'), 'TLS 或证书握手失败'),
        ('timeout', ('timeout', 'timed out', '超时'), '连接或读取超时，网络质量不稳定'),
        ('connection', ('connection', 'connectionreset', 'remote end closed', '连接', '网络'),
         '网络连接中断或被远端关闭'),
        ('incomplete', ('incomplete', 'missing', '缺失', 'content-length', '数据传输中断', '响应为空', '图片损坏'),
         '图片数据不完整或校验失败'),
        ('disk', ('no space', 'disk full', '磁盘空间'), '磁盘空间不足'),
        ('permission', ('permission', 'access is denied', '拒绝访问'), '文件写入权限不足'),
        ('server', ('500', '502', '503', '520', '521', '522', '523', '524'), '远端服务或网关异常'),
    ]
    for code, needles, reason in rules:
        if any(needle in lower for needle in needles):
            return {'code': code, 'reason': reason, 'detail': text}
    return {'code': 'unknown', 'reason': translate_error(text) or '未知错误', 'detail': text}


def _normalize_app_state(data):
    if not isinstance(data, dict):
        data = {}
    profiles = data.get('profiles')
    history = data.get('history')
    active_profile = data.get('active_profile')
    capsule = data.get('capsule')
    data['profiles'] = profiles if isinstance(profiles, dict) else {}
    data['history'] = [record for record in history if isinstance(record, dict)] \
        if isinstance(history, list) else []
    data['active_profile'] = active_profile if isinstance(active_profile, str) else ''
    capsule = capsule if isinstance(capsule, dict) else {}
    dock_side = capsule.get('dock_side')
    try:
        capsule_y = int(capsule.get('y', 120) or 120)
    except (TypeError, ValueError):
        capsule_y = 120
    listen_clipboard = capsule.get('listen_clipboard', True)
    if not isinstance(listen_clipboard, bool):
        if isinstance(listen_clipboard, str):
            listen_clipboard = listen_clipboard.strip().lower() not in ('0', 'false', 'no', 'off')
        else:
            listen_clipboard = bool(listen_clipboard)
    data['capsule'] = {
        'dock_side': dock_side if dock_side in ('left', 'right') else 'right',
        'y': max(0, min(capsule_y, 100000)),
        'listen_clipboard': listen_clipboard,
    }
    return data


def _load_app_state_unlocked(path):
    if not path.exists():
        return _normalize_app_state({})
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return _normalize_app_state(json.load(f))
    except Exception:
        logger.warning('应用状态损坏或无法读取: %s', path)
        return _normalize_app_state({})


def _save_app_state_unlocked(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(_normalize_app_state(state), f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        Path(temp_name).replace(path)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def load_app_state(state_file=None):
    with _APP_STATE_LOCK:
        path = Path(state_file or (Path(__file__).parent / APP_STATE_FILE))
        return _load_app_state_unlocked(path)


def save_app_state(state, state_file=None):
    with _APP_STATE_LOCK:
        path = Path(state_file or (Path(__file__).parent / APP_STATE_FILE))
        _save_app_state_unlocked(path, state)


def update_app_state(mutator, state_file=None):
    """Atomically load, mutate and save application state."""
    with _APP_STATE_LOCK:
        path = Path(state_file or (Path(__file__).parent / APP_STATE_FILE))
        state = _load_app_state_unlocked(path)
        result = mutator(state)
        _save_app_state_unlocked(path, state)
        return result


def append_download_history(site, item_id, title, status, total=0, missing=0,
                            path='', error=None, state_file=None, task_id=None):
    def mutate(state):
        failure = classify_failure(error) if error else None
        record = {
            'history_id': f'{time.time_ns()}-{threading.get_ident()}',
            'time': time.strftime('%Y-%m-%d %H:%M:%S'),
            'site': site,
            'id': str(item_id),
            'title': title or str(item_id),
            'status': status,
            'total': int(total or 0),
            'missing': int(missing or 0),
            'path': str(path or ''),
            'failure': failure,
        }
        if task_id is not None:
            record['task_id'] = str(task_id)
            for index in range(len(state['history']) - 1, -1, -1):
                existing = state['history'][index]
                if existing.get('site') == site and existing.get('task_id') == str(task_id):
                    state['history'][index] = record
                    break
            else:
                state['history'].append(record)
        else:
            state['history'].append(record)
        state['history'] = state['history'][-2000:]
        return record
    return update_app_state(mutate, state_file)


def sanitize_filename(name):
    if not name:
        return 'unknown'
    name = unicodedata.normalize('NFC', repair_mojibake(name))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    name = name.replace('\n', ' ').replace('\r', ' ')
    name = name.strip('. ')
    reserved = {'CON', 'PRN', 'AUX', 'NUL', 'COM1', 'COM2', 'COM3', 'COM4',
                'COM5', 'COM6', 'COM7', 'COM8', 'COM9', 'LPT1', 'LPT2', 'LPT3',
                'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9'}
    stem = name.split('.', 1)[0].upper()
    if stem in reserved:
        name = '_' + name
    name = name[:MAX_FILENAME_LEN].rstrip('. ')
    return name or 'unknown'


def strip_status_tag(name):
    name = re.sub(r'\[完整\]\s*$', '', name)
    name = re.sub(r'\[缺失\d+页\]\s*$', '', name)
    return name.rstrip()


def make_tagged_name(base_name, num_pages, missing_count):
    clean = strip_status_tag(base_name)
    if missing_count == 0:
        return f'{clean} {TAG_OK}'
    else:
        return f'{clean} {TAG_FAIL_PREFIX}{missing_count}{TAG_FAIL_SUFFIX}'


def format_size(bytes_val):
    if bytes_val < 1024:
        return f'{bytes_val} B'
    elif bytes_val < 1024 * 1024:
        return f'{bytes_val / 1024:.1f} KB'
    elif bytes_val < 1024 * 1024 * 1024:
        return f'{bytes_val / (1024 * 1024):.2f} MB'
    else:
        return f'{bytes_val / (1024 * 1024 * 1024):.2f} GB'


def format_time(seconds):
    if seconds < 0 or seconds > 3600 * 24:
        return '计算中...'
    if seconds < 60:
        return f'{int(seconds)}秒'
    elif seconds < 3600:
        return f'{int(seconds // 60)}分{int(seconds % 60)}秒'
    else:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f'{h}小时{m}分'


def format_speed(bytes_per_sec):
    if bytes_per_sec < 1024:
        return f'{bytes_per_sec:.0f} B/s'
    elif bytes_per_sec < 1024 * 1024:
        return f'{bytes_per_sec / 1024:.1f} KB/s'
    else:
        return f'{bytes_per_sec / (1024 * 1024):.2f} MB/s'


def parse_gallery_status(gallery_id, output_dir='./downloads'):
    output_path = Path(output_dir)
    if not output_path.exists():
        return 'none', gallery_id, None

    index = load_download_index(output_path)
    entry = index.get(str(gallery_id), {})
    indexed_path = entry.get('path')
    if indexed_path:
        dir_path = Path(indexed_path)
        if dir_path.exists() and dir_path.is_dir():
            name = dir_path.name
            if TAG_OK in name:
                return 'complete', name, dir_path
            if TAG_FAIL_PREFIX in name:
                return 'partial', name, dir_path
            status = entry.get('status')
            if status in {'complete', 'partial', 'downloaded', 'none'}:
                return status, name, dir_path

    for d in output_path.iterdir():
        if d.is_dir() and d.name.startswith(f'{gallery_id}_'):
            if TAG_OK in d.name:
                return 'complete', d.name, d
            elif TAG_FAIL_PREFIX in d.name:
                return 'partial', d.name, d
            return 'downloaded', d.name, d
    return 'none', gallery_id, None


def load_download_index(output_dir):
    output_path = Path(output_dir)
    index_file = output_path / DOWNLOAD_INDEX_FILE
    if not index_file.exists():
        return {}
    try:
        with open(index_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        logger.warning('下载索引损坏或无法读取: %s', index_file)
        try:
            backup = index_file.with_suffix(index_file.suffix + '.corrupt')
            if not backup.exists():
                index_file.replace(backup)
        except OSError:
            logger.warning('无法备份损坏的下载索引: %s', index_file)
    return {}


def save_download_index(output_dir, index):
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    index_file = output_path / DOWNLOAD_INDEX_FILE
    lock = _index_lock(output_path)
    with lock, _process_index_lock(output_path):
        fd, temp_name = tempfile.mkstemp(prefix='.download_index.', suffix='.tmp',
                                          dir=str(output_path))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(index, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            Path(temp_name).replace(index_file)
        finally:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass


def update_download_index(output_dir, gallery_id, **entry):
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    with _index_lock(output_path), _process_index_lock(output_path):
        index = load_download_index(output_path)
        key = str(gallery_id)
        current = index.get(key, {})
        current.update(entry)
        current['gallery_id'] = key
        index[key] = current
        fd, temp_name = tempfile.mkstemp(prefix='.download_index.', suffix='.tmp',
                                          dir=str(output_path))
        index_file = output_path / DOWNLOAD_INDEX_FILE
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(index, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            Path(temp_name).replace(index_file)
        finally:
            Path(temp_name).unlink(missing_ok=True)


def get_cached_title(gallery_id, output_dir='./downloads'):
    index = load_download_index(output_dir)
    entry = index.get(str(gallery_id), {})
    title = entry.get('title')
    if title:
        return title

    for d in Path(output_dir).glob(f'{gallery_id}_*'):
        if d.is_dir():
            cache_file = d / 'info.json'
            if cache_file.exists():
                try:
                    with open(cache_file, 'r', encoding='utf-8') as f:
                        info = json.load(f)
                    return info.get('title', gallery_id)
                except Exception:
                    pass
    return gallery_id
