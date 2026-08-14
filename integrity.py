# -*- coding: utf-8 -*-
"""下载完成强完整性校验与修复中心。

提供：
- 逐文件 PIL 解码校验（损坏图片检测）
- SHA-256 清单比对（文件被修改/半成品检测）
- 缺失页检测（按 info.json 的 num_pages 与 NN.NN 命名规则）
- 校验报告 + 调用现有下载器进行修复

独立于 GUI，可单独 import 使用，也可被 unified_gui 集成。
"""
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
INTEGRITY_FILE = '.integrity.json'


def _page_number_from_name(name):
    """从 0001.jpg 提取页码；不符合规则返回 None。"""
    import re
    m = re.match(r'^(\d{1,6})\.(?:jpe?g|png|webp|gif)$', name.lower())
    return int(m.group(1)) if m else None


def sha256_file(path, chunk_size=1 << 16):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            block = f.read(chunk_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def image_is_valid(path):
    """PIL 严格校验：能打开且 verify 通过，并确保像素数据完整。"""
    try:
        with Image.open(path) as image:
            image.verify()
        # verify() 后不能再用同一对象；重新打开做 load 保证像素数据可读
        with Image.open(path) as image:
            image.load()
        return True
    except Exception:
        return False


@dataclass
class GalleryVerdict:
    gallery_id: str
    dir_path: str = ''
    total_pages: int = 0
    found_pages: int = 0
    missing: list = field(default_factory=list)
    corrupt: list = field(default_factory=list)
    mismatch: list = field(default_factory=list)
    zero_byte: list = field(default_factory=list)
    files: int = 0
    error: str = ''

    @property
    def ok(self):
        return (not self.missing and not self.corrupt and not self.mismatch
                and not self.zero_byte and not self.error)


class IntegrityVerifier:
    """完整性校验与修复中心。"""

    def __init__(self, output_dir, manifest_name=INTEGRITY_FILE):
        self.output_dir = Path(output_dir)
        self.manifest_name = manifest_name
        self._lock = threading.RLock()
        self._manifests = {}
        self._loaded = set()

    # ---------- manifest ----------
    def _gallery_manifest_path(self, gallery_id):
        return self.output_dir / (self.manifest_name + '.' + str(gallery_id))

    def load_manifest(self, gallery_id):
        with self._lock:
            if gallery_id in self._loaded:
                return self._manifests.get(gallery_id, {})
            path = self._gallery_manifest_path(gallery_id)
            data = {}
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding='utf-8'))
                    if not isinstance(data, dict):
                        data = {}
                except Exception:
                    data = {}
            self._manifests[gallery_id] = data
            self._loaded.add(gallery_id)
            return data

    def save_manifest(self, gallery_id, data):
        data = data or {}
        with self._lock:
            self._manifests[gallery_id] = data
            self._loaded.add(gallery_id)
            path = self._gallery_manifest_path(gallery_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile_mkstemp_near(path)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                Path(temp_name).replace(path)
            finally:
                try:
                    Path(temp_name).unlink(missing_ok=True)
                except OSError:
                    pass

    def update_manifest(self, gallery_id, dir_path):
        """记录目录内所有图片的 SHA-256 清单（键为相对目录的相对路径）。"""
        dir_path = Path(dir_path)
        files = {}
        if dir_path.exists():
            for p in sorted(dir_path.rglob('*')):
                if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES and image_is_valid(p):
                    try:
                        files[str(p.relative_to(dir_path))] = sha256_file(p)
                    except (OSError, ValueError):
                        continue
        manifest = {
            'updated': time.strftime('%Y-%m-%d %H:%M:%S'),
            'files': files,
        }
        self.save_manifest(gallery_id, manifest)
        return manifest

    # ---------- 校验 ----------
    def find_dir(self, gallery_id):
        if not self.output_dir.exists():
            return None
        prefix = f'{str(gallery_id)}_'
        for d in self.output_dir.iterdir():
            if d.is_dir() and d.name.startswith(prefix):
                return d
        return None

    def _expected_pages(self, dir_path):
        info_path = dir_path / 'info.json'
        if info_path.exists():
            try:
                info = json.loads(info_path.read_text(encoding='utf-8'))
                num = int(info.get('num_pages') or 0)
                if num > 0:
                    return num
            except Exception:
                pass
        return None

    def verify_gallery(self, gallery_id) -> GalleryVerdict:
        verdict = GalleryVerdict(str(gallery_id))
        dir_path = self.find_dir(gallery_id)
        if dir_path is None:
            verdict.error = '目录不存在'
            return verdict
        verdict.dir_path = str(dir_path)
        verdict.total_pages = self._expected_pages(dir_path) or 0

        manifest = self.load_manifest(gallery_id).get('files', {})

        page_set = set()
        corrupt = []
        mismatch = []
        zero_byte = []
        for p in sorted(dir_path.rglob('*')):
            if not p.is_file() or p.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            verdict.files += 1
            relative = str(p.relative_to(dir_path))
            num = _page_number_from_name(p.name) if p.parent == dir_path else None
            if num is not None:
                page_set.add(num)
            if p.stat().st_size == 0:
                zero_byte.append(relative)
                continue
            if not image_is_valid(p):
                corrupt.append(relative)
                continue
            stored = manifest.get(relative)
            if stored is not None:
                try:
                    if sha256_file(p) != stored:
                        mismatch.append(relative)
                except OSError:
                    corrupt.append(relative)

        verdict.corrupt = corrupt
        verdict.mismatch = mismatch
        verdict.zero_byte = zero_byte
        if verdict.total_pages:
            verdict.missing = sorted(n for n in range(1, verdict.total_pages + 1)
                                     if n not in page_set)
            verdict.found_pages = len(page_set)
        return verdict

    def verify_all(self, gallery_ids=None):
        """校验输出目录全部（或指定）画廊，返回 [(verdict, status)]。"""
        if gallery_ids is None:
            if not self.output_dir.exists():
                return []
            gallery_ids = []
            for d in self.output_dir.iterdir():
                if d.is_dir() and '_' in d.name:
                    gid = d.name.split('_', 1)[0]
                    if gid.isdigit() and gid not in gallery_ids:
                        gallery_ids.append(gid)
        results = []
        for gid in gallery_ids:
            verdict = self.verify_gallery(gid)
            results.append((verdict, 'ok' if verdict.ok else
                            ('缺失' if verdict.missing else
                             '损坏' if verdict.corrupt or verdict.mismatch else '异常')))
        return results

    def scan_tree(self, base_dir):
        """递归扫描目录树（JM 目录结构），返回 {绝对路径: 问题}。

        问题取值：'zero_byte'（空文件）/ 'corrupt'（损坏/不可解码）。
        用于 JM 等非 "id_标题" 命名规则的下载目录。
        """
        base_dir = Path(base_dir)
        issues = {}
        if not base_dir.exists():
            return issues
        for p in base_dir.rglob('*'):
            if not p.is_file() or p.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            try:
                if p.stat().st_size == 0:
                    issues[str(p)] = 'zero_byte'
                elif not image_is_valid(p):
                    issues[str(p)] = 'corrupt'
            except OSError:
                issues[str(p)] = 'corrupt'
        return issues

    # ---------- 修复 ----------
    def repair_gallery(self, crawler, gallery_id, callback=None):
        """校验后调用下载器修复（重新下载缺失/损坏页），修复后刷新清单。"""
        before = self.verify_gallery(gallery_id)
        if before.ok:
            return True, before
        try:
            ok = crawler.download_gallery(gallery_id, callback=callback)
        except Exception as exc:
            ok = False
            before.error = str(exc)
        after = self.verify_gallery(gallery_id)
        if after.dir_path:
            self.update_manifest(gallery_id, after.dir_path)
        return ok and after.ok, after

    def repair_all(self, crawler, gallery_ids=None, on_progress=None):
        """修复全部问题画廊，返回 [(gid, ok, verdict)]。"""
        issues = [verdict.gallery_id for verdict, _status in self.verify_all(gallery_ids)
                  if not verdict.ok and verdict.dir_path]
        results = []
        for idx, gid in enumerate(issues, 1):
            if on_progress:
                on_progress(idx, len(issues), gid)
            ok, verdict = self.repair_gallery(crawler, gid)
            results.append((gid, ok, verdict))
        return results


def tempfile_mkstemp_near(path):
    import tempfile
    return tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp',
                            dir=str(path.parent))
