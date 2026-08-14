# -*- coding: utf-8 -*-
"""持久任务队列：存盘、恢复、暂停、重试。

- 任务状态：pending / running / done / failed / paused
- 每次变更原子写盘，进程崩溃/重启后可恢复（pending 与 running 均视为待恢复）
- 线程安全；与 utils 的下载索引相互独立
"""
import json
import os
import tempfile
import threading
import time
from pathlib import Path

TASK_QUEUE_FILE = '.task_queue.json'

PENDING, RUNNING, DONE, FAILED, PAUSED = 'pending', 'running', 'done', 'failed', 'paused'
VALID_STATUSES = {PENDING, RUNNING, DONE, FAILED, PAUSED}


def _now():
    return time.strftime('%Y-%m-%d %H:%M:%S')


class PersistentTaskQueue:
    def __init__(self, path=None, max_attempts=3):
        self.path = Path(path) if path else (Path(__file__).parent / TASK_QUEUE_FILE)
        self.max_attempts = max(1, int(max_attempts))
        self._lock = threading.RLock()
        self._tasks = {}
        self._seq = 0
        self._load()

    # ---------- 持久化 ----------
    def _normalize_task(self, tid, task):
        if not isinstance(task, dict):
            return None

        normalized = dict(task)
        normalized['id'] = str(tid)
        normalized['site'] = str(normalized.get('site') or '')
        normalized['item_id'] = str(normalized.get('item_id') or '')
        if not normalized['site'] or not normalized['item_id']:
            return None
        normalized['title'] = str(normalized.get('title') or normalized['item_id'])
        if normalized.get('status') not in VALID_STATUSES:
            normalized['status'] = PENDING
        try:
            normalized['attempts'] = max(0, int(normalized.get('attempts', 0)))
        except (TypeError, ValueError):
            normalized['attempts'] = 0
        try:
            normalized['max_attempts'] = max(
                1, int(normalized.get('max_attempts', self.max_attempts)))
        except (TypeError, ValueError):
            normalized['max_attempts'] = self.max_attempts
        for field in ('total', 'missing'):
            try:
                normalized[field] = max(0, int(normalized.get(field, 0)))
            except (TypeError, ValueError):
                normalized[field] = 0
        normalized['error'] = str(normalized.get('error') or '')
        normalized['created'] = str(normalized.get('created') or _now())
        normalized['updated'] = str(normalized.get('updated') or normalized['created'])
        return normalized

    def _load(self):
        with self._lock:
            if not self.path.exists():
                return
            try:
                data = json.loads(self.path.read_text(encoding='utf-8'))
                tasks = data.get('tasks', {}) if isinstance(data, dict) else {}
                for tid, task in tasks.items():
                    normalized = self._normalize_task(tid, task)
                    if normalized is None:
                        continue
                    self._tasks[str(tid)] = normalized
                    try:
                        self._seq = max(self._seq, int(tid))
                    except (TypeError, ValueError):
                        pass
            except Exception:
                # 损坏的队列文件备份后重建
                try:
                    backup = self.path.with_suffix(self.path.suffix + '.corrupt')
                    if not backup.exists():
                        self.path.replace(backup)
                except OSError:
                    pass

    def _save(self):
        data = {'version': 1, 'tasks': self._tasks}
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp',
                                         dir=str(path.parent))
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

    # ---------- 基础访问 ----------
    def _next_id(self):
        self._seq += 1
        return str(self._seq)

    def all(self):
        with self._lock:
            return [dict(t) for t in self._tasks.values()]

    def get(self, task_id):
        with self._lock:
            task = self._tasks.get(str(task_id))
            return dict(task) if task else None

    def by_item(self, site, item_id):
        with self._lock:
            for task in self._tasks.values():
                if task.get('site') == site and str(task.get('item_id', '')) == str(item_id):
                    return dict(task)
            return None

    # ---------- 写入 ----------
    def _add_unlocked(self, site, item_id, title='', **meta):
        existing = None
        for tid, task in self._tasks.items():
            if (task.get('site') == site
                    and str(task.get('item_id', '')) == str(item_id)
                    and task.get('status') != DONE):
                existing = tid
                break
        if existing is not None:
            task = self._tasks[existing]
            task['title'] = title or task.get('title', '')
            task['updated'] = _now()
            if task.get('status') in (FAILED, PAUSED):
                task['status'] = PENDING
                task['attempts'] = 0
                task['error'] = ''
            return existing
        tid = self._next_id()
        task = {
            'id': tid,
            'site': site,
            'item_id': str(item_id),
            'title': title or str(item_id),
            'status': PENDING,
            'attempts': 0,
            'max_attempts': int(meta.pop('max_attempts', self.max_attempts)),
            'error': '',
            'total': 0,
            'missing': 0,
            'created': _now(),
            'updated': _now(),
        }
        task.update(meta)
        self._tasks[tid] = task
        return tid

    def add(self, site, item_id, title='', **meta):
        """新增一个任务；若同一 site+item_id 已存在（非 done）则复用。返回 task_id。"""
        with self._lock:
            tid = self._add_unlocked(site, item_id, title, **meta)
            self._save()
            return tid

    def add_many(self, items):
        """items: [(site, item_id, title?), ...]"""
        with self._lock:
            ids = []
            for item in items:
                title = item[2] if len(item) == 3 else ''
                ids.append(self._add_unlocked(item[0], item[1], title))
            if ids:
                self._save()
            return ids

    # ---------- 取任务 ----------
    def pending(self, site=None):
        with self._lock:
            return [dict(t) for t in self._tasks.values()
                    if t.get('status') == PENDING
                    and (site is None or t.get('site') == site)]

    def next(self, site=None):
        """按创建顺序取一个 pending 任务（不修改状态）。"""
        with self._lock:
            for t in self._tasks.values():
                if t.get('status') == PENDING and (site is None or t.get('site') == site):
                    return dict(t)
            return None

    def claim(self, task_id):
        """将 pending 任务置为 running。返回 task dict 或 None。"""
        with self._lock:
            task = self._tasks.get(str(task_id))
            if not task or task.get('status') != PENDING:
                return None
            task['status'] = RUNNING
            task['attempts'] = int(task.get('attempts', 0)) + 1
            task['error'] = ''
            task['updated'] = _now()
            self._save()
            return dict(task)

    def finish(self, task_id, ok, error='', total=0, missing=0):
        with self._lock:
            task = self._tasks.get(str(task_id))
            if not task:
                return None
            if ok:
                task['status'] = DONE
                task['error'] = ''
            else:
                task['status'] = FAILED
                task['error'] = str(error)
            task['total'] = int(total or task.get('total', 0))
            task['missing'] = int(missing or 0)
            task['updated'] = _now()
            self._save()
            return dict(task)

    def retry(self, task_id, max_attempts=None):
        """将 failed/paused 任务重新置为 pending（若次数未超限）。"""
        with self._lock:
            task = self._tasks.get(str(task_id))
            if not task:
                return False
            if max_attempts is not None:
                task['max_attempts'] = max(1, int(max_attempts))
            if int(task.get('attempts', 0)) >= int(task.get('max_attempts', self.max_attempts)):
                return False
            task['status'] = PENDING
            task['updated'] = _now()
            self._save()
            return True

    def retry_all(self):
        with self._lock:
            n = 0
            for task in self._tasks.values():
                if (task.get('status') == FAILED
                        and int(task.get('attempts', 0)) < int(task.get('max_attempts', self.max_attempts))):
                    task['status'] = PENDING
                    task['updated'] = _now()
                    n += 1
            if n:
                self._save()
            return n

    def pause(self, task_id):
        with self._lock:
            task = self._tasks.get(str(task_id))
            if not task or task.get('status') != PENDING:
                return False
            task['status'] = PAUSED
            task['updated'] = _now()
            self._save()
            return True

    def park(self, task_id):
        """将任务置为暂停（用于用户主动停止时保留现场，不记失败、不自动重试）。"""
        with self._lock:
            task = self._tasks.get(str(task_id))
            if not task or task.get('status') == DONE:
                return False
            task['status'] = PAUSED
            task['error'] = task.get('error') or '用户停止'
            task['updated'] = _now()
            self._save()
            return True

    def resume(self, task_id):
        with self._lock:
            task = self._tasks.get(str(task_id))
            if not task or task.get('status') != PAUSED:
                return False
            task['status'] = PENDING
            task['updated'] = _now()
            self._save()
            return True

    def pause_all(self):
        with self._lock:
            n = 0
            for task in self._tasks.values():
                if task.get('status') == PENDING:
                    task['status'] = PAUSED
                    task['updated'] = _now()
                    n += 1
            if n:
                self._save()
            return n

    def resume_all(self):
        with self._lock:
            n = 0
            for task in self._tasks.values():
                if task.get('status') == PAUSED:
                    task['status'] = PENDING
                    task['updated'] = _now()
                    n += 1
            if n:
                self._save()
            return n

    # ---------- 清理与统计 ----------
    def clear_finished(self):
        with self._lock:
            ids = [tid for tid, t in self._tasks.items() if t.get('status') == DONE]
            for tid in ids:
                del self._tasks[tid]
            if ids:
                self._save()
            return len(ids)

    def clear(self, statuses=None):
        with self._lock:
            statuses = set(statuses or ())
            if not statuses:
                ids = list(self._tasks)
            else:
                ids = [tid for tid, t in self._tasks.items()
                       if t.get('status') in statuses]
            for tid in ids:
                del self._tasks[tid]
            if ids:
                self._save()
            return len(ids)

    def count_by_status(self):
        with self._lock:
            counts = {PENDING: 0, RUNNING: 0, DONE: 0, FAILED: 0, PAUSED: 0}
            for t in self._tasks.values():
                counts[t.get('status', PENDING)] += 1
            return counts

    def recoverable(self):
        """重启后需要恢复的任务：pending + running（上次中断）。"""
        with self._lock:
            return [dict(t) for t in self._tasks.values()
                    if t.get('status') in (PENDING, RUNNING)]

    def recover(self):
        """程序重启后调用：把上次中断的 running 任务重置为 pending，返回数量。

        未完成的 pending 任务保持 pending；running（进行中被中断）改为 pending 以便续跑。
        """
        with self._lock:
            n = 0
            for t in self._tasks.values():
                if t.get('status') == RUNNING:
                    t['status'] = PENDING
                    n += 1
            if n:
                self._save()
            return n

    # ---------- 调整顺序 ----------
    def _ordered_ids(self):
        with self._lock:
            return [tid for tid, t in self._tasks.items()
                    if t.get('status') in (PENDING, PAUSED, FAILED)]

    def reorder(self, task_id, position):
        """调整任务在队列中的位置（按 pending/paused/failed 计数，0 为最前）。"""
        with self._lock:
            tid = str(task_id)
            if tid not in self._tasks:
                return False
            task = self._tasks.pop(tid)
            ordered = [tid2 for tid2, t in self._tasks.items()
                       if t.get('status') in (PENDING, PAUSED, FAILED)]
            position = max(0, min(position, len(ordered)))
            rebuilt = {}
            inserted = False
            for i, tid2 in enumerate(ordered):
                if i == position and not inserted:
                    rebuilt[tid] = task
                    inserted = True
                rebuilt[tid2] = self._tasks[tid2]
            if not inserted:
                rebuilt[tid] = task
            # 保留非活跃任务的相对顺序
            for tid2, t in self._tasks.items():
                if tid2 not in rebuilt:
                    rebuilt[tid2] = t
            self._tasks = rebuilt
            self._save()
            return True

    def move_to_top(self, task_id):
        return self.reorder(task_id, 0)

    def move_to_bottom(self, task_id):
        return self.reorder(task_id, self._ordered_ids().__len__())
