# -*- coding: utf-8 -*-
import os
import sys
import re
import json
import unicodedata
from pathlib import Path

MAX_FILENAME_LEN = 180
TAG_OK = '[完整]'
TAG_FAIL_PREFIX = '[缺失'
TAG_FAIL_SUFFIX = '页]'
JM_COLLECTION_FILE = Path(__file__).parent / '新建文本文档.txt'

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


def load_collection_ids():
    ids = []
    if JM_COLLECTION_FILE.exists():
        with open(JM_COLLECTION_FILE, 'r', encoding='utf-8') as f:
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
        if JM_COLLECTION_FILE.exists():
            print(f'[警告] 合集文件 "{JM_COLLECTION_FILE.name}" 为空，使用内置预设ID', file=sys.stderr)
        else:
            print(f'[警告] 合集文件 "{JM_COLLECTION_FILE.name}" 不存在，使用内置预设ID', file=sys.stderr)
        ids = ['641734', '644868', '644485', '640276', '637978', '633629',
               '629937', '629936', '629935', '629933', '629934', '568638',
               '530743', '492966', '483197', '403166', '311324', '325247']
    return list(dict.fromkeys(ids))


def get_collection_desc():
    ids = load_collection_ids()
    return f'共 {len(ids)} 本'


def translate_error(error_str):
    for key, cn in ERROR_CN.items():
        if key.lower() in str(error_str).lower():
            return cn
    return str(error_str)


def translate_http_status(code):
    return HTTP_CN.get(code, f'HTTP错误 {code}')


def sanitize_filename(name):
    if not name:
        return 'unknown'
    name = unicodedata.normalize('NFKC', name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    name = name.replace('\n', ' ').replace('\r', ' ')
    name = name.strip('. ')
    reserved = {'CON', 'PRN', 'AUX', 'NUL', 'COM1', 'COM2', 'COM3', 'COM4',
                'COM5', 'COM6', 'COM7', 'COM8', 'COM9', 'LPT1', 'LPT2', 'LPT3',
                'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9'}
    if name.upper() in reserved:
        name = '_' + name
    return name[:MAX_FILENAME_LEN]


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
    for d in output_path.iterdir():
        if d.is_dir() and d.name.startswith(f'{gallery_id}_'):
            if TAG_OK in d.name:
                return 'complete', d.name, d
            elif TAG_FAIL_PREFIX in d.name:
                return 'partial', d.name, d
            return 'downloaded', d.name, d
    return 'none', gallery_id, None


def get_cached_title(gallery_id):
    for d in Path('./downloads').glob(f'{gallery_id}_*'):
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
