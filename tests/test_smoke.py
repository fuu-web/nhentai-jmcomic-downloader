import tempfile
import threading
import unittest
import io
import json
from pathlib import Path

from PIL import Image

from unified_gui import NHentaiCrawler, WeeklyPanel
from jmcomic.jm_client_interface import JmImageResp
from jmcomic.jm_client_impl import AbstractJmClient
from adaptive_scheduler import (
    AdaptiveScheduler, ChallengeDetected, detect_challenge_response,
    parse_proxy_pool,
)
from utils import (
    append_download_history, classify_failure, load_app_state,
    load_download_index, repair_mojibake, sanitize_filename,
    update_app_state, update_download_index,
)


class SmokeTests(unittest.TestCase):
    def test_index_concurrent_updates(self):
        output_dir = tempfile.mkdtemp()
        threads = [
            threading.Thread(
                target=update_download_index,
                args=(output_dir, str(i)),
                kwargs={'status': 'complete'},
            )
            for i in range(5)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(load_download_index(output_dir)), 5)

    def test_existing_image_validation(self):
        output_dir = Path(tempfile.mkdtemp())
        image_path = output_dir / '0001.jpg'
        Image.new('RGB', (2, 2), 'red').save(image_path)
        crawler = NHentaiCrawler(stealth_mode=False, workers=1)
        self.assertEqual(crawler.scan_existing(output_dir, 1, 'jpg'), [])
        image_path.write_bytes(b'not-an-image')
        self.assertEqual(crawler.scan_existing(output_dir, 1, 'jpg'), [1])

    def test_stop_survives_reset(self):
        crawler = NHentaiCrawler(stealth_mode=False, workers=1)
        crawler.stop()
        crawler.reset()
        self.assertTrue(crawler._stop_event.is_set())

    def test_failure_history_is_structured(self):
        state_file = Path(tempfile.mkdtemp()) / 'state.json'
        append_download_history('NHentai', '1', 'test', 'failed',
                                error='ProxyError: timed out', state_file=state_file)
        record = load_app_state(state_file)['history'][0]
        self.assertEqual(record['failure']['code'], 'proxy')
        self.assertEqual(classify_failure('HTTP 429')['code'], 'rate_limited')

    def test_jm_image_is_written_atomically_and_validated(self):
        raw = io.BytesIO()
        Image.new('RGB', (3, 3), 'blue').save(raw, 'JPEG')

        class Response:
            status_code = 200
            content = raw.getvalue()
            url = 'https://example.test/x.jpg'

        output = Path(tempfile.mkdtemp()) / 'x.jpg'
        JmImageResp(Response()).transfer_to(output, None, False)
        with Image.open(output) as image:
            image.verify()
        self.assertFalse(output.with_name('x.part.jpg').exists())

    def test_weekly_cover_cache_validation(self):
        panel = WeeklyPanel.__new__(WeeklyPanel)
        panel._cover_cache_dir = Path(tempfile.mkdtemp())
        cover = panel._cover_cache_dir / '123.jpg'
        Image.new('RGB', (10, 10), 'green').save(cover)
        self.assertTrue(panel._cached_cover_is_valid('123'))
        cover.write_bytes(b'broken')
        self.assertFalse(panel._cached_cover_is_valid('123'))
        self.assertFalse(cover.exists())

    def test_weekly_data_cache_round_trip(self):
        panel = WeeklyPanel.__new__(WeeklyPanel)
        panel._weekly_cache_dir = Path(tempfile.mkdtemp())
        panel._weekly_cache_lock = threading.Lock()
        expected = {'list': [{'id': '123', 'name': 'cached'}], 'total': 1}
        panel._save_weekly_json('test.json', expected)
        self.assertEqual(panel._load_weekly_json('test.json'), expected)

    def test_weekly_advanced_mapping_is_json_safe(self):
        class MappingLike(dict):
            pass

        source = MappingLike(list=[MappingLike(id='123')], total=1)
        plain = WeeklyPanel._to_plain_data(source)
        self.assertEqual(plain, {'list': [{'id': '123'}], 'total': 1})
        json.dumps(plain)

    def test_app_state_schema_is_repaired(self):
        state_file = Path(tempfile.mkdtemp()) / 'state.json'
        state_file.write_text(json.dumps({
            'profiles': [], 'active_profile': 123,
            'history': [None, {'id': 'valid'}], 'capsule': [],
        }), encoding='utf-8')
        state = load_app_state(state_file)
        self.assertEqual(state['profiles'], {})
        self.assertEqual(state['active_profile'], '')
        self.assertEqual(state['history'], [{'id': 'valid'}])
        self.assertEqual(state['capsule'], {
            'dock_side': 'right', 'y': 120, 'listen_clipboard': True,
        })

    def test_capsule_state_fields_are_repaired(self):
        state_file = Path(tempfile.mkdtemp()) / 'state.json'
        state_file.write_text(json.dumps({'capsule': {
            'dock_side': 'middle', 'y': 'bad', 'listen_clipboard': 'false',
        }}), encoding='utf-8')
        state = load_app_state(state_file)
        self.assertEqual(state['capsule'], {
            'dock_side': 'right', 'y': 120, 'listen_clipboard': False,
        })

    def test_capsule_extract_ids_is_ordered_and_unique(self):
        import unified_gui
        capsule = unified_gui.FloatingCapsule.__new__(unified_gui.FloatingCapsule)
        self.assertEqual(
            capsule.extract_ids('https://nhentai.net/g/123/ 123 456 123'),
            ['123', '456'])

    def test_app_state_transaction_preserves_concurrent_history(self):
        state_file = Path(tempfile.mkdtemp()) / 'state.json'
        barrier = threading.Barrier(3)

        def add_history(item_id):
            barrier.wait()
            append_download_history('test', item_id, item_id, 'complete',
                                    state_file=state_file)

        threads = [threading.Thread(target=add_history, args=(str(i),)) for i in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        update_app_state(lambda state: state['profiles'].update(default={}), state_file)
        state = load_app_state(state_file)
        self.assertEqual(len(state['history']), 2)
        self.assertIn('default', state['profiles'])

    def test_history_task_id_updates_instead_of_duplicating(self):
        state_file = Path(tempfile.mkdtemp()) / 'state.json'
        append_download_history('NHentai', '1', 'test', 'failed',
                                error='first', state_file=state_file, task_id='7')
        append_download_history('NHentai', '1', 'test', 'complete',
                                state_file=state_file, task_id='7')
        history = load_app_state(state_file)['history']
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['status'], 'complete')

    def test_history_delete_removes_only_selected_record(self):
        import unified_gui
        state_file = Path(tempfile.mkdtemp()) / 'state.json'
        first = append_download_history('NHentai', '1', 'first', 'complete', state_file=state_file)
        append_download_history('NHentai', '2', 'second', 'failed', state_file=state_file)
        panel = unified_gui.HistoryPanel.__new__(unified_gui.HistoryPanel)
        panel._selected_record = lambda: first
        panel.refresh = unittest.mock.Mock()
        panel.gui = type('Gui', (), {'status_var': type('Var', (), {'set': lambda *_: None})()})()
        with unittest.mock.patch.object(unified_gui, 'update_app_state') as update, \
                unittest.mock.patch.object(unified_gui.messagebox, 'askyesno', return_value=True):
            update.side_effect = lambda mutator: mutator({'history': [first, {
                'site': 'NHentai', 'id': '2', 'title': 'second', 'status': 'failed',
            }]})
            panel.delete_selected()
        self.assertTrue(update.called)
        panel.refresh.assert_called_once()

    def test_history_export_json_and_csv(self):
        import unified_gui
        panel = unified_gui.HistoryPanel.__new__(unified_gui.HistoryPanel)
        panel._records = [{
            'time': '2026-01-01 00:00:00', 'site': 'NHentai', 'id': '1',
            'title': 'test', 'status': 'failed', 'total': 10, 'missing': 2,
            'path': 'x', 'failure': {'code': 'timeout', 'reason': '超时', 'detail': 'timed out'},
        }]
        panel.gui = type('Gui', (), {'status_var': type('Var', (), {'set': lambda *_: None})()})()
        base = Path(tempfile.mkdtemp())
        for suffix in ('.json', '.csv'):
            output = base / f'history{suffix}'
            with unittest.mock.patch.object(unified_gui.filedialog, 'asksaveasfilename', return_value=str(output)), \
                    unittest.mock.patch.object(unified_gui.messagebox, 'showinfo'):
                panel.export_current()
            self.assertTrue(output.exists())
            self.assertGreater(output.stat().st_size, 0)

    def test_dynamic_browser_fetch_receives_proxy(self):
        import nhentai_engine
        crawler = NHentaiCrawler.__new__(NHentaiCrawler)
        crawler.proxy = 'http://proxy.test:8080'
        crawler._adapt_response = lambda page, _url: page
        with unittest.mock.patch.object(nhentai_engine, 'DynamicFetcher') as fetcher:
            fetcher.fetch.return_value = object()
            crawler._fetch_with_browser('https://example.test')
        self.assertEqual(fetcher.fetch.call_args.kwargs['proxy'], crawler.proxy)

    def test_cli_dynamic_browser_fetch_receives_proxy(self):
        import nhentai_crawler
        crawler = nhentai_crawler.NHentaiCrawler.__new__(nhentai_crawler.NHentaiCrawler)
        crawler.proxy = 'socks5://proxy.test:1080'
        crawler.config = {'timeout': 30}
        crawler._adapt_page_response = lambda page, _url: page
        with unittest.mock.patch.object(nhentai_crawler, 'DynamicFetcher') as fetcher:
            fetcher.fetch.return_value = object()
            crawler._fetch_with_browser('https://example.test', 0)
        self.assertEqual(fetcher.fetch.call_args.kwargs['proxy'], crawler.proxy)

    def test_real_browser_rejects_unsupported_authenticated_proxy(self):
        import nhentai_engine
        manager = nhentai_engine.AntiCrawlManager()
        with unittest.mock.patch.object(nhentai_engine, 'NODRIVER_AVAIL', True), \
                unittest.mock.patch.object(nhentai_engine.uc, 'start') as start:
            ok, error = manager._try_browser(
                'https://example.test/image.jpg',
                Path(tempfile.mkdtemp()) / 'image.jpg', '1',
                'http://user:pass@proxy.test:8080', 10)
        self.assertFalse(ok)
        self.assertIn('无法安全应用', error)
        start.assert_not_called()

    def test_gui_close_waits_for_download_threads(self):
        import unified_gui

        class FakeThread:
            alive = True

            def is_alive(self):
                return self.alive

        class FakeRoot:
            def __init__(self):
                self.destroyed = False
                self.after_callback = None

            def after(self, _delay, callback):
                self.after_callback = callback
                return 'after-id'

            def after_cancel(self, _after_id):
                pass

            def title(self, _title):
                pass

            def destroy(self):
                self.destroyed = True

        thread = FakeThread()
        gui = unified_gui.UnifiedGUI.__new__(unified_gui.UnifiedGUI)
        gui.root = FakeRoot()
        gui._closing = False
        gui._cf_monitor_id = None
        gui._ui_pump_id = None
        gui.capsule = None
        gui.nhentai_tab = type('NhTab', (), {
            'crawler': None, '_stop_requested': False, 'download_thread': thread,
        })()
        gui.jm_tab = type('JmTab', (), {
            '_jm_stop_event': threading.Event(), '_jm_thread': None,
        })()

        gui.on_close()
        self.assertFalse(gui.root.destroyed)
        self.assertIsNotNone(gui.root.after_callback)
        thread.alive = False
        gui.root.after_callback()
        self.assertTrue(gui.root.destroyed)

    def test_weekly_cache_filename_uses_explicit_snapshot(self):
        panel = WeeklyPanel.__new__(WeeklyPanel)
        panel._selected_category = 'new/category'
        panel._current_type = 'new type'
        self.assertEqual(panel._weekly_result_filename('old/category', 'old type'),
                         'old_category_old_type.json')

    def test_cancelled_gallery_emits_cancelled_event(self):
        output = Path(tempfile.mkdtemp())
        crawler = NHentaiCrawler(output_dir=output, stealth_mode=False, workers=1)
        crawler.get_gallery_info = lambda _gid: ({
            'id': '1', 'title': 'test', 'title_jp': '', 'media_id': '1',
            'num_pages': 1, 'ext': 'jpg', 'parodies': [], 'tags': [],
            'artists': [], 'groups': [], 'languages': [], 'categories': [],
        }, None)

        def cancel_page(*_args, **_kwargs):
            crawler.stop()
            return False

        crawler.download_single_page = cancel_page
        events = []
        result = crawler.download_gallery('1', lambda event, _gid, data=None: events.append(event))
        self.assertFalse(result)
        self.assertIn('cancelled', events)
        self.assertNotIn('complete', events)

    def test_gallery_byte_count_is_not_cumulative(self):
        output = Path(tempfile.mkdtemp())
        crawler = NHentaiCrawler(output_dir=output, stealth_mode=False, workers=1)

        def gallery_info(gid):
            return ({
                'id': gid, 'title': f'test-{gid}', 'title_jp': '', 'media_id': gid,
                'num_pages': 1, 'ext': 'jpg', 'parodies': [], 'tags': [],
                'artists': [], 'groups': [], 'languages': [], 'categories': [],
            }, None)

        crawler.get_gallery_info = gallery_info

        def download_page(_gid, page, gallery_dir, *_args, **_kwargs):
            image_path = gallery_dir / f'{page:04d}.jpg'
            Image.new('RGB', (2, 2), 'red').save(image_path)
            with crawler.bytes_lock:
                crawler.total_downloaded_bytes += 100
            return True

        crawler.download_single_page = download_page
        totals = []

        def callback(event, _gid, data=None):
            if event == 'complete':
                totals.append(data.get('total_bytes'))

        self.assertTrue(crawler.download_gallery('1', callback))
        self.assertTrue(crawler.download_gallery('2', callback))
        self.assertEqual(totals, [100, 100])

    def test_jm_domain_retry_uses_next_domain(self):
        client = AbstractJmClient.__new__(AbstractJmClient)
        client.retry_times = 1
        client.domain_list = ['bad.test', 'good.test']
        client.domain_retry_strategy = None
        client.of_api_url = lambda path, domain: f'https://{domain}{path}'
        client.update_request_with_specify_domain = lambda *_args: None
        client.raise_if_resp_should_retry = lambda response, _is_image: response
        client.before_retry = lambda *_args: None
        client.log_topic = lambda: 'test'
        calls = []

        def request(url, **_kwargs):
            calls.append(url)
            if 'bad.test' in url:
                raise OSError('unavailable')
            return 'ok'

        self.assertEqual(client.request_with_retry(request, '/week'), 'ok')
        self.assertEqual(calls[-1], 'https://good.test/week')
        self.assertEqual(len(calls), 3)

    def test_proxy_pool_parsing(self):
        self.assertEqual(
            parse_proxy_pool('http://a:1; socks5://b:2\ndirect'),
            ['http://a:1', 'socks5://b:2', None],
        )

    def test_adaptive_scheduler_reduces_and_recovers_concurrency(self):
        scheduler = AdaptiveScheduler(
            max_concurrency=8, min_concurrency=1,
            failure_threshold=2, cooldown=10, recovery_successes=2,
            proxies=['http://proxy:1'],
        )
        initial = scheduler.current_limit
        lease = scheduler.acquire(['cdn.test'])
        lease.finish(status=429)
        self.assertLess(scheduler.current_limit, initial)
        self.assertEqual(scheduler.snapshot()['open_routes'], 1)

        scheduler._states[('cdn.test', 'http://proxy:1')].blocked_until = 0
        for _ in range(2):
            lease = scheduler.acquire(['cdn.test'])
            lease.finish(status=200)
        self.assertGreater(scheduler.current_limit, 1)

    def test_adaptive_scheduler_switches_away_from_failed_proxy(self):
        scheduler = AdaptiveScheduler(
            max_concurrency=3, failure_threshold=1, cooldown=30,
            proxies=['http://bad:1', 'http://good:2'],
        )
        first = scheduler.acquire(['api.test'])
        self.assertEqual(first.proxy, 'http://bad:1')
        first.finish(error=ConnectionError('proxy failed'))
        second = scheduler.acquire(['api.test'])
        self.assertEqual(second.proxy, 'http://good:2')
        second.finish(status=200)

    def test_http_200_challenge_page_is_detected_and_pauses_site(self):
        class Response:
            status_code = 200
            headers = {'Content-Type': 'text/html; charset=utf-8'}
            text = '<title>Just a moment...</title>Checking your browser'

        marker = detect_challenge_response(Response())
        self.assertTrue(marker)
        scheduler = AdaptiveScheduler(max_concurrency=6, proxies=[None])
        lease = scheduler.acquire(['site.test'])
        lease.finish(status=200, error=ChallengeDetected(marker))
        snapshot = scheduler.snapshot()
        self.assertGreater(snapshot['site_cooldown'], 0)
        self.assertEqual(snapshot['limit'], 1)

    def test_browser_priority_is_optional_and_reversible(self):
        scheduler = AdaptiveScheduler(
            max_concurrency=8, initial_concurrency=4, proxies=[None])
        scheduler.configure(browser_priority=True)
        self.assertTrue(scheduler.snapshot()['browser_priority'])
        self.assertEqual(scheduler.current_limit, 2)
        scheduler.configure(browser_priority=False)
        self.assertFalse(scheduler.snapshot()['browser_priority'])
        self.assertEqual(scheduler.current_limit, 4)

    def test_mojibake_repair_preserves_cjk(self):
        for original in ('金瓶梅【作者：沐浴橙汁儿】', '日本語タイトル', '한국어 제목'):
            broken = original.encode('utf-8').decode('latin-1')
            self.assertEqual(repair_mojibake(broken), original)
            self.assertEqual(repair_mojibake(original), original)
            self.assertEqual(sanitize_filename(original), original)

    def test_windows_reserved_filename_with_extension_is_safe(self):
        self.assertEqual(sanitize_filename('CON.txt'), '_CON.txt')
        self.assertEqual(sanitize_filename('nul.jpg'), '_nul.jpg')
        self.assertFalse(sanitize_filename('A' * 179 + '.tail').endswith(('.', ' ')))


if __name__ == '__main__':
    unittest.main()
