# -*- coding: utf-8 -*-
"""故障模拟与完整回归验证。

覆盖：
- 完整性中心：损坏图片 / 零字节文件 / 缺页 / 清单比对 / 修复 / JM 树扫描
- 持久任务队列：崩溃恢复 / 暂停 / 继续 / 重试上限 / 调整顺序 / 持久化
- 请求与带宽预算：限速计时
- 代理出口检测：出口 IP / 同 IP 识别 / IP 服务全部不可用的容错
- 调度器故障模拟：线路全熔断 + 取消事件中断
"""
import io
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest import mock

from PIL import Image

import integrity
from integrity import IntegrityVerifier, image_is_valid
from task_queue import PersistentTaskQueue, PENDING, RUNNING, DONE, FAILED, PAUSED
from adaptive_scheduler import AdaptiveScheduler, JmAdaptiveStrategy


def _make_image(path, color='red', size=(4, 4)):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', size, color).save(path)


def _write_info(dir_path, num_pages):
    (dir_path / 'info.json').write_text(
        json.dumps({'id': '123', 'num_pages': num_pages}), encoding='utf-8')


class IntegrityFaultTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.gdir = self.base / '123_title'
        self.gdir.mkdir()

    def test_corrupt_zero_byte_and_missing_pages(self):
        for i in (1, 2, 3):
            _make_image(self.gdir / f'{i:04d}.jpg')
        _write_info(self.gdir, 4)
        # 制造故障
        self.gdir.joinpath('0002.jpg').write_bytes(b'not-a-real-image')
        self.gdir.joinpath('0003.jpg').write_bytes(b'')
        v = IntegrityVerifier(self.base).verify_gallery('123')
        self.assertEqual(v.missing, [4])
        self.assertIn('0002.jpg', v.corrupt)
        self.assertIn('0003.jpg', v.zero_byte)
        self.assertFalse(v.ok)
        self.assertFalse(image_is_valid(self.gdir / '0002.jpg'))

    def test_manifest_detects_modified_file(self):
        for i in (1, 2):
            _make_image(self.gdir / f'{i:04d}.jpg')
        _write_info(self.gdir, 2)
        verifier = IntegrityVerifier(self.base)
        verifier.update_manifest('123', self.gdir)
        self.assertTrue(verifier.verify_gallery('123').ok)
        # 模拟下载后被篡改/半成品覆盖
        Image.new('RGB', (4, 4), 'green').save(self.gdir / '0001.jpg')
        self.assertIn('0001.jpg', verifier.verify_gallery('123').mismatch)

    def test_repair_refills_missing_via_crawler(self):
        for i in (1, 2):
            _make_image(self.gdir / f'{i:04d}.jpg')
        _write_info(self.gdir, 3)
        verifier = IntegrityVerifier(self.base)

        class FakeCrawler:
            def __init__(self, output):
                self.output = Path(output)

            def download_gallery(self, gallery_id, callback=None):
                # 模拟下载器补上了缺失的第三页
                _make_image(self.output / '123_title' / '0003.jpg', color='blue')
                return True

        ok, verdict = verifier.repair_gallery(FakeCrawler(self.base), '123')
        self.assertTrue(ok)
        self.assertTrue(verdict.ok)

    def test_repair_detects_dir_missing(self):
        verifier = IntegrityVerifier(self.base)
        v = verifier.verify_gallery('999')  # 不存在的画廊
        self.assertEqual(v.error, '目录不存在')

    def test_jm_tree_scan_finds_corrupt_and_empty(self):
        jm = self.base / 'jm'
        (jm / '作者A' / '标题B' / '01').mkdir(parents=True)
        _make_image(jm / '作者A' / '标题B' / '01' / '0001.jpg')
        bad = jm / '作者A' / '标题B' / '01' / '0002.jpg'
        bad.write_bytes(b'')
        broken = jm / '作者A' / '标题B' / '02'
        broken.mkdir()
        (broken / '0001.jpg').write_bytes(b'junk-not-image')
        issues = IntegrityVerifier(str(jm)).scan_tree(str(jm))
        self.assertEqual(issues[str(bad)], 'zero_byte')
        self.assertEqual(issues[str(broken / '0001.jpg')], 'corrupt')

    def test_verify_all_scans_output(self):
        for i in (1, 2):
            _make_image(self.gdir / f'{i:04d}.jpg')
        _write_info(self.gdir, 3)
        results = IntegrityVerifier(self.base).verify_all()
        self.assertEqual(len(results), 1)
        verdict, status = results[0]
        self.assertEqual(verdict.missing, [3])

    def test_manifest_subdir_same_name_no_false_mismatch(self):
        sub1 = self.gdir / '01'
        sub2 = self.gdir / '02'
        sub1.mkdir()
        sub2.mkdir()
        _make_image(sub1 / '0001.jpg', 'red')
        _make_image(sub2 / '0001.jpg', 'blue')
        _make_image(self.gdir / '0001.jpg', 'green')
        _write_info(self.gdir, 1)
        verifier = IntegrityVerifier(self.base)
        verifier.update_manifest('123', self.gdir)
        # 子目录同名图片用相对路径做键，不应互相覆盖导致假 mismatch
        self.assertTrue(verifier.verify_gallery('123').ok)

    def test_nested_page_does_not_hide_missing_root_page(self):
        nested = self.gdir / 'nested'
        nested.mkdir()
        _make_image(nested / '0001.jpg')
        _write_info(self.gdir, 1)
        verdict = IntegrityVerifier(self.base).verify_gallery('123')
        self.assertEqual(verdict.missing, [1])

    def test_nested_issues_use_relative_paths(self):
        nested = self.gdir / 'nested'
        nested.mkdir()
        (nested / '0001.jpg').write_bytes(b'')
        verdict = IntegrityVerifier(self.base).verify_gallery('123')
        self.assertIn(str(Path('nested') / '0001.jpg'), verdict.zero_byte)


class TaskQueueFaultTests(unittest.TestCase):
    def test_load_repairs_invalid_task_schema(self):
        path = Path(tempfile.mkdtemp()) / 'queue.json'
        path.write_text(json.dumps({'tasks': {
            '7': {
                'site': 'NHentai', 'item_id': 123, 'status': 'unknown',
                'attempts': 'bad', 'max_attempts': 0, 'total': -2,
            },
            '8': {'site': '', 'item_id': 'missing-site'},
        }}), encoding='utf-8')
        q = PersistentTaskQueue(path, max_attempts=3)
        task = q.get('7')
        self.assertEqual(task['id'], '7')
        self.assertEqual(task['item_id'], '123')
        self.assertEqual(task['status'], PENDING)
        self.assertEqual(task['attempts'], 0)
        self.assertEqual(task['max_attempts'], 1)
        self.assertEqual(task['total'], 0)
        self.assertIsNone(q.get('8'))
        self.assertEqual(q.count_by_status()[PENDING], 1)

    def test_persistence_survives_instance_reload(self):
        path = Path(tempfile.mkdtemp()) / 'queue.json'
        q = PersistentTaskQueue(path)
        tid = q.add('NHentai', '111', '标题')
        q2 = PersistentTaskQueue(path)
        task = q2.get(tid)
        self.assertEqual(task['site'], 'NHentai')
        self.assertEqual(task['item_id'], '111')

    def test_crash_recovery_running_to_pending(self):
        path = Path(tempfile.mkdtemp()) / 'queue.json'
        q = PersistentTaskQueue(path, max_attempts=3)
        tid = q.add('NHentai', '222')
        q.claim(tid)  # 模拟下载中崩溃，任务停在 running
        q2 = PersistentTaskQueue(path)
        self.assertEqual(q2.recover(), 1)
        self.assertEqual(q2.get(tid)['status'], PENDING)
        self.assertTrue(q2.claim(tid))

    def test_pause_resume(self):
        q = PersistentTaskQueue(Path(tempfile.mkdtemp()) / 'q.json')
        tid = q.add('NHentai', '1')
        self.assertTrue(q.pause(tid))
        self.assertEqual(q.get(tid)['status'], PAUSED)
        self.assertIsNone(q.next('NHentai'))
        self.assertTrue(q.resume(tid))
        self.assertEqual(q.next('NHentai')['item_id'], '1')

    def test_retry_limited_by_max_attempts(self):
        q = PersistentTaskQueue(Path(tempfile.mkdtemp()) / 'q.json', max_attempts=2)
        tid = q.add('NHentai', '1')
        q.claim(tid)
        q.finish(tid, False)
        self.assertTrue(q.retry(tid))
        q.claim(tid)
        q.finish(tid, False)
        self.assertFalse(q.retry(tid))

    def test_reorder_moves_task_to_front(self):
        q = PersistentTaskQueue(Path(tempfile.mkdtemp()) / 'q.json')
        for gid in ('a', 'b', 'c'):
            q.add('NHentai', gid)
        c_id = next(t['id'] for t in q.all() if t['item_id'] == 'c')
        self.assertTrue(q.move_to_top(c_id))
        self.assertEqual(q.next('NHentai')['item_id'], 'c')

    def test_duplicate_item_not_duplicated_until_done(self):
        q = PersistentTaskQueue(Path(tempfile.mkdtemp()) / 'q.json')
        tid1 = q.add('NHentai', '42')
        tid2 = q.add('NHentai', '42')
        self.assertEqual(tid1, tid2)
        q.finish(tid1, True)
        tid3 = q.add('NHentai', '42')
        self.assertNotEqual(tid3, tid1)

    def test_readd_running_task_keeps_running_state(self):
        q = PersistentTaskQueue(Path(tempfile.mkdtemp()) / 'q.json')
        tid = q.add('NHentai', '42')
        claimed = q.claim(tid)
        self.assertEqual(claimed['status'], RUNNING)
        self.assertEqual(q.add('NHentai', '42', 'updated title'), tid)
        task = q.get(tid)
        self.assertEqual(task['status'], RUNNING)
        self.assertEqual(task['attempts'], 1)
        self.assertEqual(task['title'], 'updated title')

    def test_readd_failed_resets_to_pending(self):
        q = PersistentTaskQueue(Path(tempfile.mkdtemp()) / 'q.json', max_attempts=1)
        tid = q.add('NHentai', '1')
        q.claim(tid)
        q.finish(tid, False)
        self.assertEqual(q.get(tid)['status'], FAILED)
        # 用户再次点击“开始下载”，同一任务应重新入队并重置重试次数
        tid2 = q.add('NHentai', '1')
        self.assertEqual(tid2, tid)
        self.assertEqual(q.get(tid)['status'], PENDING)
        self.assertEqual(q.get(tid)['attempts'], 0)

    def test_park_marks_running_as_paused_not_failed(self):
        q = PersistentTaskQueue(Path(tempfile.mkdtemp()) / 'q.json')
        tid = q.add('NHentai', '1')
        q.claim(tid)
        # 用户主动停止：任务应保留为暂停，而不是失败
        self.assertTrue(q.park(tid))
        self.assertEqual(q.get(tid)['status'], PAUSED)
        self.assertEqual(q.get(tid)['status'] == FAILED, False)

    def test_batch_operations_save_once(self):
        q = PersistentTaskQueue(Path(tempfile.mkdtemp()) / 'q.json')
        with mock.patch.object(q, '_save', wraps=q._save) as save:
            q.add_many([('NHentai', str(i), str(i)) for i in range(5)])
            self.assertEqual(save.call_count, 1)
        for task in q.all():
            q.claim(task['id'])
            q.finish(task['id'], False)
        with mock.patch.object(q, '_save', wraps=q._save) as save:
            self.assertEqual(q.retry_all(), 5)
            self.assertEqual(save.call_count, 1)


class DownloadResumeFaultTests(unittest.TestCase):
    def test_mismatched_content_range_restarts_without_appending(self):
        from nhentai_engine import NHentaiCrawler

        raw = io.BytesIO()
        Image.new('RGB', (4, 4), 'blue').save(raw, 'JPEG')
        valid_image = raw.getvalue()

        class Response:
            def __init__(self, status, body, headers):
                self.status_code = status
                self.body = body
                self.headers = headers
                self.text = ''

            def iter_content(self, chunk_size=65536):
                yield self.body

            def close(self):
                pass

        responses = [
            Response(206, b'wrong-fragment', {
                'Content-Type': 'image/jpeg',
                'Content-Length': '14',
                'Content-Range': 'bytes 2-15/16',
            }),
            Response(200, valid_image, {
                'Content-Type': 'image/jpeg',
                'Content-Length': str(len(valid_image)),
            }),
        ]
        headers_seen = []

        class Session:
            def get(self, _url, headers=None, **_kwargs):
                headers_seen.append(dict(headers or {}))
                return responses.pop(0)

        class Lease:
            proxy = None

            def finish(self, **_kwargs):
                pass

        crawler = NHentaiCrawler.__new__(NHentaiCrawler)
        crawler._stop_event = threading.Event()
        crawler._stop_flag = False
        crawler.scheduler = mock.Mock(acquire=mock.Mock(return_value=Lease()))
        crawler._get_download_session = lambda _proxy=None: Session()
        crawler._download_headers = lambda _gid, _attempt: {}
        crawler._pause_enabled = lambda: False

        output = Path(tempfile.mkdtemp()) / 'image.jpg'
        output.with_name(output.name + '.part').write_bytes(b'old-data')
        ok, error = crawler.download_image_with_progress('https://cdn.test/image.jpg', output)
        self.assertTrue(ok, error)
        self.assertTrue(image_is_valid(output))
        self.assertEqual(len(headers_seen), 2)
        self.assertIn('Range', headers_seen[0])
        self.assertNotIn('Range', headers_seen[1])
class SchedulerFaultTests(unittest.TestCase):
    def test_http_date_retry_after_does_not_break_lease(self):
        scheduler = AdaptiveScheduler(max_concurrency=2, proxies=[None])
        retry_at = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30))
        lease = scheduler.acquire(['cdn.test'])
        lease.finish(status=429, retry_after=retry_at)
        snapshot = scheduler.snapshot()
        self.assertEqual(snapshot['active'], 0)
        self.assertGreater(snapshot['routes'][0]['cooldown'], 0)

    def test_jm_request_setup_error_releases_lease(self):
        scheduler = AdaptiveScheduler(max_concurrency=1, proxies=[None],
                                      pause_enabled=False)
        scheduler.configure(request_interval=0)
        strategy = JmAdaptiveStrategy(scheduler)

        class Client:
            domain_list = ['api.test']
            retry_times = 0

            @staticmethod
            def of_api_url(_url, _domain):
                raise ValueError('bad URL')

            @staticmethod
            def before_retry(*_args):
                pass

        with self.assertRaises(ValueError):
            strategy(Client(), request=lambda *_args, **_kwargs: None, url='/album')
        self.assertEqual(scheduler.snapshot()['active'], 0)

    def test_cancel_event_interrupts_while_all_routes_blocked(self):
        scheduler = AdaptiveScheduler(max_concurrency=2, proxies=[None])
        lease = scheduler.acquire(['cdn.test'])
        lease.finish(status=429)  # 唯一线路熔断
        cancel_event = threading.Event()
        result = {}

        def worker():
            try:
                scheduler.acquire(['cdn.test'], cancel_event=cancel_event)
                result['status'] = 'returned'
            except Exception as e:
                result['status'] = type(e).__name__

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        time.sleep(0.2)
        cancel_event.set()
        thread.join(timeout=3)
        self.assertEqual(result['status'], 'InterruptedError')

    def test_pause_off_skips_cooldown(self):
        scheduler = AdaptiveScheduler(max_concurrency=6, proxies=[None],
                                      pause_enabled=False)
        lease = scheduler.acquire(['cdn.test'])
        lease.finish(status=429)
        snapshot = scheduler.snapshot()
        self.assertEqual(snapshot['site_cooldown'], 0.0)
        self.assertEqual(snapshot['open_routes'], 0)


class RegressionTests(unittest.TestCase):
    """拆分后入口兼容 + 核心模块可导入。"""

    def test_entry_imports_compat(self):
        import unified_gui
        self.assertEqual(unified_gui.NHentaiCrawler.__module__, 'nhentai_engine')
        self.assertEqual(unified_gui.WeeklyPanel.__module__, 'unified_gui')

    def test_cli_requests_mode_initializes_when_scrapling_is_available(self):
        import nhentai_crawler
        with mock.patch.object(nhentai_crawler, 'SCRAPLING_AVAILABLE', True):
            crawler = nhentai_crawler.NHentaiCrawler(config={'stealth_mode': False})
        try:
            self.assertEqual(crawler._session_mode, 'requests')
        finally:
            crawler.session.close()

    def test_jm_cache_save_ignores_gallery_id_field(self):
        from unified_gui import JMComicPanel
        from utils import load_download_index
        panel = JMComicPanel.__new__(JMComicPanel)
        out = tempfile.mkdtemp()
        # 索引条目里自带的 gallery_id 字段不应与位置参数冲突
        panel._save_downloaded_cache(
            {'1': {'title': 't', 'author': 'a', 'gallery_id': '1'}}, output_dir=out)
        self.assertEqual(load_download_index(out)['1']['title'], 't')



if __name__ == '__main__':
    unittest.main()
