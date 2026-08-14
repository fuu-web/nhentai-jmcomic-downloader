# -*- coding: utf-8 -*-
"""NHentai 下载核心引擎（从 unified_gui 拆分）。

包含：画室锁、Scrapling 反爬可用性探测、AntiCrawlManager（多级反爬升级链）、
NHentaiCrawler（页面/图片下载，含请求速率与带宽预算）。

unified_gui 通过 `from nhentai_engine import ...` 复用本模块，保持原入口兼容。
"""
import os
import re
import time
import json
import random
import tempfile
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from PIL import Image

from utils import (
    sanitize_filename, make_tagged_name, repair_mojibake,
    translate_http_status, classify_failure,
)
from adaptive_scheduler import (
    AdaptiveScheduler, ChallengeDetected, detect_challenge_response,
    endpoint_from_url, parse_proxy_pool,
)

# 画室级下载互斥锁
_GALLERY_LOCKS = {}
_GALLERY_LOCKS_GUARD = threading.Lock()


def _gallery_lock(output_dir, gallery_id):
    key = (str(Path(output_dir).resolve()), str(gallery_id))
    with _GALLERY_LOCKS_GUARD:
        return _GALLERY_LOCKS.setdefault(key, threading.RLock())


def _write_text_atomic(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=path.name + '.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        Path(temp_name).replace(path)
    finally:
        try:
            Path(temp_name).unlink(missing_ok=True)
        except OSError:
            pass


def _write_json_atomic(path, data):
    _write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2))


# ==================== Scrapling 反爬增强 ====================
SCRAPLING_AVAILABLE = False
StealthySession = None
FetcherSession = None
DynamicFetcher = None
try:
    from scrapling.fetchers import StealthySession, FetcherSession, DynamicFetcher
    SCRAPLING_AVAILABLE = True
except ImportError:
    pass


# ==================== 反反爬虫管理器 ====================
# 检测各反爬库是否可用
CURLCFFI_AVAIL = False
NODRIVER_AVAIL = False
SELENIUM_AVAIL = False
try:
    from curl_cffi import requests as curl_requests
    CURLCFFI_AVAIL = True
except ImportError:
    pass
try:
    import nodriver as uc
    NODRIVER_AVAIL = True
except ImportError:
    pass
if not NODRIVER_AVAIL:
    try:
        import undetected_chromedriver as uc
        SELENIUM_AVAIL = True
    except ImportError:
        pass


class AntiCrawlManager:
    LEVEL_CFI = 1       # curl_cffi TLS指纹伪装
    LEVEL_STEALTH = 2   # UA轮换 + Sec-Fetch 隐身
    LEVEL_CDN = 3       # 切换CDN子域
    LEVEL_BROWSER = 4   # undetected-chromedriver / nodriver
    LEVEL_PROXY = 5     # 通过FlareSolverr或外部代理

    LEVEL_NAMES = {
        0: '空闲',
        1: 'TLS指纹',
        2: 'HTTP隐身',
        3: 'CDN轮换',
        4: '真实浏览器',
        5: '代理旁路',
    }

    def __init__(self):
        self.active = False
        self.current_level = 0
        self.trigger_count = 0
        self.fail_count = 0
        self.success_count = 0
        self.last_ua_idx = 0
        self._ua_pool = [
            ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36', 'Windows'),
            ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36', 'Windows'),
            ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36', 'macOS'),
            ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15', 'macOS'),
            ('Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0', 'Windows'),
            ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36', 'Linux'),
            ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1', 'iOS'),
        ]
        self._cdn_suffixes = ['i', 'i3', 'i5', 'i7', 't', 't3', 't5', 't7']
        self._status_callback = None
        self._browser = None
        self._strategy_lock = threading.Lock()
        self._state_lock = threading.RLock()

    def reset(self):
        with self._state_lock:
            self.active = False
            self.current_level = 0
            self.fail_count = 0
        self._update_status()

    def _update_status(self):
        if self._status_callback:
            self._status_callback(self.active, self.current_level)

    def on_failure(self):
        with self._state_lock:
            self.fail_count += 1
            if self.fail_count >= 3:
                self.active = True
                self.trigger_count += 1
                self.current_level = 0
                self._update_status()
            return self.active

    # ---- Level 1: curl_cffi TLS fingerprint ----
    def _try_curl_cffi(self, url, save_path, gallery_id, proxy, timeout):
        if not CURLCFFI_AVAIL:
            return False, 'curl_cffi未安装'
        self.current_level = self.LEVEL_CFI
        self._update_status()
        # 逐个 TLS 指纹尝试，避免固定指纹被识别
        last = ''
        for _ in range(len(CURL_FINGERPRINTS)):
            fp = CURL_FINGERPRINTS[self.last_ua_idx % len(CURL_FINGERPRINTS)]
            self.last_ua_idx += 1
            ua, platform = self._ua_pool[self.last_ua_idx % len(self._ua_pool)]
            self.last_ua_idx += 1
            resp = None
            try:
                resp = curl_requests.get(url,
                                         headers={
                                             'User-Agent': ua,
                                             'Referer': f'https://nhentai.net/g/{gallery_id}/',
                                             'Accept': 'image/avif,image/webp,image/apng,*/*',
                                         },
                                         impersonate=fp,
                                         proxy=proxy,
                                         timeout=timeout,
                                         stream=True)
                if resp.status_code == 200:
                    self._save_stream_atomically(resp, save_path)
                    self.success_count += 1
                    return True, None
                last = f'TLS:HTTP{resp.status_code}'
            except Exception as e:
                last = f'TLS:{e}'
            finally:
                if resp is not None:
                    resp.close()
        return False, last

    # ---- Level 2: Stealth HTTP (like puppeteer-stealth/playwright-stealth) ----
    def _try_stealth_http(self, url, save_path, gallery_id, proxy, timeout):
        self.current_level = self.LEVEL_STEALTH
        self._update_status()
        try:
            ua, platform = self._ua_pool[self.last_ua_idx % len(self._ua_pool)]
            self.last_ua_idx += 2
            with requests.Session() as sess:
                sess.headers.update({
                    'User-Agent': ua,
                    'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.9,zh-CN;q=0.8',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'Referer': f'https://nhentai.net/g/{gallery_id}/',
                    'Sec-Fetch-Dest': 'image',
                    'Sec-Fetch-Mode': 'no-cors',
                    'Sec-Fetch-Site': 'cross-site',
                    'Sec-Ch-Ua': '"Chromium";v="120", "Google Chrome";v="120", "Not-A.Brand";v="99"',
                    'Sec-Ch-Ua-Mobile': '?0',
                    'Sec-Ch-Ua-Platform': f'"{platform}"',
                    'DNT': '1',
                    'Upgrade-Insecure-Requests': '1',
                })
                if proxy:
                    sess.proxies = {'http': proxy, 'https': proxy}
                adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=20)
                sess.mount('https://', adapter)
                resp = sess.get(url, timeout=timeout, stream=True)
                if resp.status_code == 200:
                    self._validate_response_type(resp)
                    self._save_stream_atomically(resp, save_path)
                    self.success_count += 1
                    return True, None
                return False, f'隐身:HTTP{resp.status_code}'
        except Exception as e:
            return False, f'隐身:{e}'

    # ---- Level 3: CDN switching (like domain rotation) ----
    def _try_cdn_rotate(self, url, save_path, gallery_id, proxy, timeout):
        self.current_level = self.LEVEL_CDN
        self._update_status()
        m = re.match(r'https?://(i\d*)\.[^/]+/(.+)', url)
        if not m:
            return False, 'URL格式错误'
        current_cdn = m.group(1)
        path = m.group(2)
        for suffix in self._cdn_suffixes:
            if suffix == current_cdn:
                continue
            new_url = f'https://{suffix}.nhentai.net/{path}'
            ua, _ = self._ua_pool[self.last_ua_idx % len(self._ua_pool)]
            self.last_ua_idx += 1
            with requests.Session() as sess:
                sess.headers.update({
                    'User-Agent': ua,
                    'Referer': f'https://nhentai.net/g/{gallery_id}/',
                    'Accept': 'image/avif,image/webp,*/*',
                })
                if proxy:
                    sess.proxies = {'http': proxy, 'https': proxy}
                try:
                    resp = sess.get(new_url, timeout=timeout, stream=True)
                    if resp.status_code == 200:
                        self._save_stream_atomically(resp, save_path)
                        self.success_count += 1
                        return True, None
                except Exception:
                    continue
        return False, '所有CDN子域失败'

    # ---- Level 4: Real Browser (undetected-chromedriver / nodriver) ----
    def _try_browser(self, url, save_path, gallery_id, proxy, timeout):
        self.current_level = self.LEVEL_BROWSER
        self._update_status()
        if not NODRIVER_AVAIL and not SELENIUM_AVAIL:
            return False, '无浏览器驱动(nodriver/undetected-chromedriver)'

        def _download_via_nodriver():
            import asyncio
            async def _do():
                browser_args = [f'--proxy-server={proxy}'] if proxy else None
                if proxy and '@' in proxy:
                    return False, '浏览器代理包含认证信息，当前驱动无法安全应用'
                driver = await uc.start(headless=True, browser_args=browser_args)
                try:
                    page = await driver.get(url)
                    await asyncio.sleep(2)
                    raw = await page.evaluate('document.body.innerText')
                    if 'Access denied' in str(raw) or '403' in str(raw):
                        return False, '浏览器:被拒绝'
                    # 箭头函数内没有 arguments，需用普通函数取参数再喂给异步函数
                    content = await page.evaluate("""
                        (async (u) => {
                            const r = await fetch(u);
                            const b = await r.arrayBuffer();
                            return Array.from(new Uint8Array(b));
                        })(arguments[0])
                    """, url)
                    self._save_bytes_atomically(bytes(content), save_path)
                    self.success_count += 1
                    return True, None
                except Exception as e:
                    return False, f'浏览器:{e}'
                finally:
                    await driver.close()
            return asyncio.run(_do())

        def _download_via_uc():
            try:
                opts = uc.ChromeOptions()
                opts.add_argument('--headless')
                opts.add_argument('--no-sandbox')
                opts.add_argument('--disable-gpu')
                if proxy:
                    if '@' in proxy:
                        return False, '浏览器代理包含认证信息，当前驱动无法安全应用'
                    opts.add_argument(f'--proxy-server={proxy}')
                driver = uc.Chrome(options=opts)
                try:
                    driver.get(f'https://nhentai.net/g/{gallery_id}/')
                    time.sleep(2)
                    content = driver.execute_script("""
                        return fetch(arguments[0])
                            .then(r => r.arrayBuffer())
                            .then(b => Array.from(new Uint8Array(b)));
                    """, url)
                    self._save_bytes_atomically(bytes(content), save_path)
                    self.success_count += 1
                    return True, None
                except Exception as e:
                    return False, f'UC浏览器:{e}'
                finally:
                    driver.quit()
            except Exception as e:
                return False, f'UC驱动:{e}'

        if NODRIVER_AVAIL:
            return _download_via_nodriver()
        elif SELENIUM_AVAIL:
            return _download_via_uc()
        return False, '无可用浏览器'

    # ---- Level 5: External Bypass ----
    def _try_external_bypass(self, url, save_path, gallery_id, proxy, timeout):
        self.current_level = self.LEVEL_PROXY
        self._update_status()
        delay = random.uniform(5, 15)
        time.sleep(delay)
        ua, platform = self._ua_pool[self.last_ua_idx % len(self._ua_pool)]
        self.last_ua_idx += 3
        with requests.Session() as sess:
            sess.headers.update({
                'User-Agent': ua,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Referer': f'https://nhentai.net/g/{gallery_id}/',
                'Origin': 'https://nhentai.net',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Sec-Ch-Ua': '"Chromium";v="120", "Google Chrome";v="120", "Not-A.Brand";v="99"',
            })
            if proxy:
                sess.proxies = {'http': proxy, 'https': proxy}
            try:
                resp = sess.get(url, timeout=timeout, stream=True)
                if resp.status_code == 200:
                    self._save_stream_atomically(resp, save_path)
                    self.success_count += 1
                    return True, None
                return False, f'旁路:HTTP{resp.status_code}'
            except Exception as e:
                return False, f'旁路:{e}'

    def _strategies_for(self, failure_code=None):
        """按失败原因选择反爬升级链：什么类型的墙走什么类型的招。"""
        tls = [(self._try_curl_cffi, 'curl_cffi TLS指纹')]
        stealth = [(self._try_stealth_http, 'HTTP隐身')]
        cdn = [(self._try_cdn_rotate, 'CDN轮换')]
        browser = [(self._try_browser, '真实浏览器')]
        bypass = [(self._try_external_bypass, '代理旁路')]

        if failure_code == 'rate_limited':
            # 限流交给调度器冷却，不升级反爬链
            return []
        if failure_code == 'blocked':
            # Cloudflare/403：优先换 TLS 指纹和浏览器
            return tls + stealth + browser + bypass
        if failure_code in ('connection', 'timeout', 'dns', 'tls'):
            # 连接层问题：先换 CDN 子域，再换指纹
            return cdn + tls + stealth
        if failure_code == 'incomplete':
            return cdn + tls
        return tls + stealth + cdn + browser + bypass

    def download_with_anti_crawl(self, url, save_path, gallery_id, proxy=None,
                                 timeout=30, failure_code=None):
        strategies = self._strategies_for(failure_code)
        if not strategies:
            return False, '限流/冷却中，等待下一轮'
        if not self._strategy_lock.acquire(timeout=timeout):
            return False, '反爬升级链繁忙，等待下一轮补页'
        try:
            last_error = None
            for i, (strategy, name) in enumerate(strategies):
                try:
                    result, err = strategy(url, save_path, gallery_id, proxy, timeout)
                    if result:
                        if self.active:
                            self.current_level = 0
                            self.fail_count = 0
                            self.active = False
                            self._update_status()
                        return True, None
                    last_error = f'{name}:{err}'
                except Exception as e:
                    last_error = f'{name}:{e}'
                time.sleep(random.uniform(0.5, 2.0) * (i + 1))
            return False, f'全部{len(strategies)}级失败: {last_error}'
        finally:
            self._strategy_lock.release()

    @staticmethod
    def _save_bytes_atomically(content, save_path):
        save_path = Path(save_path)
        partial_path = save_path.with_name(save_path.name + '.part')
        try:
            with open(partial_path, 'wb') as f:
                f.write(content)
            if partial_path.stat().st_size == 0:
                raise IOError('响应为空')
            with Image.open(partial_path) as image:
                image.verify()
            partial_path.replace(save_path)
        finally:
            partial_path.unlink(missing_ok=True)

    def _save_stream_atomically(self, response, save_path):
        save_path = Path(save_path)
        partial_path = save_path.with_name(save_path.name + '.part')
        try:
            self._validate_response_type(response)
            with open(partial_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=65536):
                    f.write(chunk)
            if partial_path.stat().st_size == 0:
                raise IOError('响应为空')
            self._validate_image_file(partial_path)
            partial_path.replace(save_path)
        finally:
            partial_path.unlink(missing_ok=True)

    @staticmethod
    def _validate_image_file(path):
        path = Path(path)
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image.load()

    @staticmethod
    def _validate_response_type(response):
        content_type = response.headers.get('Content-Type', '').lower()
        if content_type and not content_type.startswith('image/'):
            raise ValueError(f'响应不是图片: {content_type}')

    def get_status_text(self):
        with self._state_lock:
            if self.active:
                level_name = self.LEVEL_NAMES.get(self.current_level, '未知')
                return f'L{self.current_level} {level_name}', '#d73a49'
            if self.fail_count > 0:
                return f'警告({self.fail_count})', '#e36209'
            return '就绪', '#22863a'

    def get_defense_info(self):
        lines = [
            f'curl_cffi  : {"V" if CURLCFFI_AVAIL else "x"}',
            f'nodriver   : {"V" if NODRIVER_AVAIL else "x"}',
            f'uc-driver : {"V" if SELENIUM_AVAIL else "x"}',
            f'Scrapling  : {"V" if SCRAPLING_AVAILABLE else "x"}',
        ]
        return '  |  '.join(lines)

    def cleanup(self):
        if self._browser:
            try:
                import asyncio
                asyncio.get_event_loop().run_until_complete(self._browser.close())
            except Exception:
                pass
            self._browser = None


# ==================== NHentai 爬虫类 ====================

# 请求头指纹池：UA + Sec-Ch-Ua 组合，按“代理/尝试次数”轮换，
# 让每次重试不被识别为同一台设备的固定指纹。
HEADER_FINGERPRINTS = [
    ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
     '"Chromium";v="120", "Google Chrome";v="120", "Not-A.Brand";v="99"'),
    ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
     '"Chromium";v="131", "Google Chrome";v="131", "Not_A Brand";v="24"'),
    ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
     '"AppleWebKit";v="605", "Version";v="17", "Safari";v="605"'),
    ('Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0',
     '"Firefox";v="128", "Gecko";v="130", "Firefox";v="128"'),
    ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0',
     '"Chromium";v="122", "Microsoft Edge";v="122", "Not.A.Brand";v="99"'),
]

# TLS 指纹池（curl_cffi impersonate 参数）
CURL_FINGERPRINTS = ['chrome120', 'chrome131', 'safari17_0', 'firefox128', 'edge122']

# 画廊页面镜像域（按顺序回退；官方域名在最前）
MIRROR_DOMAINS = ['nhentai.net', 'nhentai.to', 'nhentai.xxx', 'nhentai.com']


class NHentaiCrawler:
    def __init__(self, proxy=None, output_dir='./downloads', quality='high',
                 max_rounds=5, stealth_mode=True, use_browser_fallback=True,
                 workers=12, speed_mode='极速', scheduler=None,
                 browser_priority=False, pause_enabled=True,
                 mirror_domains=None):
        self.proxy_pool = parse_proxy_pool(proxy)
        self.proxy = next((item for item in self.proxy_pool if item), None)
        self.output_dir = Path(output_dir)
        self.quality = quality
        self.max_rounds = max_rounds
        self.stealth_mode = stealth_mode and SCRAPLING_AVAILABLE
        self.use_browser_fallback = use_browser_fallback
        self.workers = workers
        self.speed_mode = speed_mode
        self.mirror_domains = list(mirror_domains) if mirror_domains else list(MIRROR_DOMAINS)
        self._apply_speed_mode()
        self.scheduler = scheduler or AdaptiveScheduler(
            max_concurrency=self.workers, min_concurrency=1,
            failure_threshold=3, cooldown=20,
            recovery_successes=8, proxies=self.proxy_pool,
            initial_concurrency=2 if self.speed_mode == '保守' else 4,
            pause_enabled=pause_enabled)
        self.scheduler.configure(
            browser_priority=browser_priority,
            request_interval=0.15 if self.speed_mode == '保守' else 0.05,
            pause_enabled=pause_enabled)

        self.session = None
        self.stealth_session = None
        self._session_mode = "requests"
        self._browser_required_cache = {}
        self.download_session = None
        self._download_sessions = threading.local()
        self._page_sessions = threading.local()
        self._page_fetch_lock = threading.Lock()
        self._owned_sessions = set()
        self._owned_sessions_lock = threading.Lock()

        self.errors = []
        self._stop_flag = False
        self._stop_event = threading.Event()
        self.cloudflare_hits = 0
        self.bytes_lock = threading.Lock()
        self.total_downloaded_bytes = 0

        self.setup_session()
        self.setup_download_session()
        self.anti_crawl = AntiCrawlManager()

    def _apply_speed_mode(self):
        mode = self.speed_mode
        if mode == '保守':
            self.delay_min, self.delay_max = 0.3, 1.0
            if self.workers > 6:
                self.workers = 6
        elif mode == '极速':
            self.delay_min, self.delay_max = 0.01, 0.05
        elif mode == '狂暴':
            self.delay_min, self.delay_max = 0.0, 0.0
            if self.workers < 32:
                self.workers = 32
        else:
            self.delay_min, self.delay_max = 0.05, 0.2

    def _pause_enabled(self):
        """当前是否启用“等待”（调度冷却 + 反爬升级链）。"""
        try:
            return bool(getattr(self.scheduler, 'pause_enabled', True))
        except Exception:
            return True

    def setup_session(self):
        if not self.stealth_mode:
            self._setup_requests_session()
            return
        try:
            kwargs = {'headless': True, 'solve_cloudflare': True, 'timeout': 30}
            if self.proxy:
                kwargs['proxy'] = self.proxy
            self.stealth_session = StealthySession(**kwargs)
            self._session_mode = "stealth"
            return
        except Exception:
            pass
        try:
            if self.proxy:
                self.session = FetcherSession(proxy=self.proxy, timeout=30)
            else:
                self.session = FetcherSession(timeout=30)
            self._session_mode = "fetcher"
            return
        except Exception:
            pass
        self._setup_requests_session()

    def _setup_requests_session(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://nhentai.net/',
        })
        if self.proxy:
            self.session.proxies = {'http': self.proxy, 'https': self.proxy}
        self._session_mode = "requests"

    def setup_download_session(self):
        # requests.Session 不保证线程安全，每个下载线程各自持有一个会话。
        self.download_session = None

    @staticmethod
    def _validate_image_file(path):
        with Image.open(Path(path)) as image:
            image.verify()
        with Image.open(Path(path)) as image:
            image.load()

    @staticmethod
    def _validate_response_type(response):
        content_type = response.headers.get('Content-Type', '').lower()
        if content_type and not content_type.startswith('image/'):
            raise ValueError(f'响应不是图片: {content_type}')

    def _get_download_session(self, proxy=None):
        sessions = getattr(self._download_sessions, 'sessions', None)
        if sessions is None:
            sessions = {}
            self._download_sessions.sessions = sessions
        key = proxy or 'DIRECT'
        session = sessions.get(key)
        if session is None:
            session = requests.Session()
            session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
                'Referer': 'https://nhentai.net/',
            })
            if proxy:
                session.proxies = {'http': proxy, 'https': proxy}
            retry = requests.adapters.Retry(
                total=0, connect=0, read=0, status=0,
                backoff_factor=0,
                status_forcelist=(),
                allowed_methods=frozenset(('GET', 'HEAD')),
                respect_retry_after_header=True,
            )
            adapter = requests.adapters.HTTPAdapter(
                max_retries=retry, pool_connections=max(10, self.workers),
                pool_maxsize=max(20, self.workers * 2), pool_block=True)
            session.mount('http://', adapter)
            session.mount('https://', adapter)
            sessions[key] = session
            with self._owned_sessions_lock:
                self._owned_sessions.add(session)
        return session

    def _get_page_session(self, proxy=None):
        sessions = getattr(self._page_sessions, 'sessions', None)
        if sessions is None:
            sessions = {}
            self._page_sessions.sessions = sessions
        key = proxy or 'DIRECT'
        session = sessions.get(key)
        if session is None:
            session = requests.Session()
            session.headers.update(self.session.headers if isinstance(self.session, requests.Session) else {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36',
                'Referer': 'https://nhentai.net/',
            })
            if proxy:
                session.proxies = {'http': proxy, 'https': proxy}
            sessions[key] = session
            with self._owned_sessions_lock:
                self._owned_sessions.add(session)
        return session

    def stop(self):
        self._stop_event.set()
        self._stop_flag = True

    def reset(self):
        self.errors = []
        self.total_downloaded_bytes = 0
        self.cloudflare_hits = 0
        if hasattr(self, 'anti_crawl'):
            self.anti_crawl.reset()

    def get_page(self, url, retry=3, force_browser=False):
        if self.use_browser_fallback and url in self._browser_required_cache:
            force_browser = self._browser_required_cache[url]
        for attempt in range(retry):
            if self._stop_event.is_set() or self._stop_flag:
                return None
            try:
                if force_browser and SCRAPLING_AVAILABLE and DynamicFetcher is not None:
                    with self._page_fetch_lock:
                        return self._fetch_with_browser(url)
                elif self._session_mode == "stealth" and self.stealth_session:
                    with self._page_fetch_lock:
                        return self._fetch_with_stealth(url)
                elif self._session_mode == "fetcher" and self.session:
                    with self._page_fetch_lock:
                        return self._fetch_with_fetcher(url)
                else:
                    return self._fetch_with_requests(url)
            except Exception as e:
                error_msg = str(e).lower()
                if self.use_browser_fallback and any(kw in error_msg for kw in
                                                     ['cloudflare', 'captcha', 'turnstile', 'cf-ray']):
                    self._browser_required_cache[url] = True
                    if len(self._browser_required_cache) > 200:
                        self._browser_required_cache.clear()
                    self.cloudflare_hits += 1
                    if not force_browser:
                        force_browser = True
                        continue
                if attempt < retry - 1 and not self._stop_event.is_set():
                    self._stop_event.wait(random.uniform(1, 3))
        return None

    def _fetch_with_stealth(self, url):
        response = self.stealth_session.fetch(url)
        return self._adapt_response(response, url)

    def _fetch_with_fetcher(self, url):
        page = self.session.get(url)
        return self._adapt_response(page, url)

    def _fetch_with_browser(self, url):
        kwargs = {
            'headless': True,
            'wait_until': 'networkidle',
            'timeout': 30000,
        }
        if self.proxy:
            kwargs['proxy'] = self.proxy
        page = DynamicFetcher.fetch(url, **kwargs)
        return self._adapt_response(page, url)

    def _fetch_with_requests(self, url):
        lease = self.scheduler.acquire([endpoint_from_url(url)], cancel_event=self._stop_event)
        try:
            response = self._get_page_session(lease.proxy).get(url, timeout=30)
            status = response.status_code
            challenge = detect_challenge_response(response)
            if challenge:
                response.close()
                raise ChallengeDetected(challenge)
            response.raise_for_status()
            response.encoding = 'utf-8'
            lease.finish(status=status)
            return response
        except Exception as error:
            lease.finish(error=error)
            raise

    def _adapt_response(self, scrapling_obj, url):
        class AdaptedResponse:
            def __init__(self, obj, url):
                self.url = url
                self.status_code = getattr(obj, 'status_code', getattr(obj, 'status', 200))
                if hasattr(obj, 'html'):
                    self.text = obj.html
                elif hasattr(obj, 'text'):
                    self.text = repair_mojibake(obj.text)
                else:
                    self.text = str(obj)

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise Exception(f"HTTP {self.status_code}")
        return AdaptedResponse(scrapling_obj, url)

    def get_gallery_info(self, gallery_id):
        response = None
        # 主域失败时按序回退到镜像域
        for domain in self.mirror_domains:
            url = f'https://{domain}/g/{gallery_id}/'
            response = self.get_page(url)
            if response:
                break
        if not response:
            return None, f'无法访问画廊 {gallery_id}'

        soup = BeautifulSoup(response.text, 'html.parser')

        gallery_data = {}
        for el in soup.select('script[type="application/json"]'):
            if not el.string or 'media_id' not in el.string:
                continue
            try:
                wrapper = json.loads(el.string)
                body = json.loads(wrapper.get('body', '{}'))
                if body.get('id') == int(gallery_id):
                    gallery_data = body
                    break
            except Exception:
                continue

        if not gallery_data:
            title_elem = soup.find('h1', class_='title')
            title = sanitize_filename(title_elem.get_text(strip=True)) if title_elem else f'gallery_{gallery_id}'
            thumb_divs = soup.select('.thumb-container img')
            num_pages = len(thumb_divs)
            if num_pages == 0:
                return None, f'画廊 {gallery_id} 未找到图片'
            first_src = thumb_divs[0].get('data-src', '') or thumb_divs[0].get('src', '')
            media_match = re.search(r'/galleries/(\d+)/', first_src)
            if not media_match:
                return None, f'画廊 {gallery_id} 缺少有效的 media_id'
            return {
                'id': gallery_id, 'title': title, 'title_jp': '',
                'media_id': media_match.group(1), 'num_pages': num_pages, 'ext': 'webp',
                'parodies': [], 'tags': [], 'artists': [],
                'groups': [], 'languages': [], 'categories': [],
                'upload_date': '', 'favorites': 0,
            }, None

        title_obj = gallery_data.get('title', {})
        title_en = title_obj.get('english', '') or title_obj.get('pretty', f'gallery_{gallery_id}')
        title_jp = title_obj.get('japanese', '')

        num_pages = gallery_data.get('num_pages', 0)
        media_id = str(gallery_data.get('media_id', ''))
        if not media_id.isdigit():
            return None, f'画廊 {gallery_id} 缺少有效的 media_id'

        cover = gallery_data.get('cover', {})
        cover_path = cover.get('path', '')
        ext = 'webp'
        if cover_path.endswith('.png'):
            ext = 'png'
        elif cover_path.endswith('.jpg') or cover_path.endswith('.jpeg'):
            ext = 'jpg'

        favorites = gallery_data.get('num_favorites', 0)
        upload_ts = gallery_data.get('upload_date', 0)
        if upload_ts > 0:
            upload_date = datetime.fromtimestamp(upload_ts).strftime('%Y-%m-%d %H:%M:%S')
        else:
            upload_date = ''

        parodies, tags, artists, groups, languages, categories = [], [], [], [], [], []
        for tag in gallery_data.get('tags', []):
            tag_type = tag.get('type', '')
            tag_name = tag.get('name', '')
            if tag_type == 'parody':
                parodies.append(tag_name)
            elif tag_type == 'tag':
                tags.append(tag_name)
            elif tag_type == 'artist':
                artists.append(tag_name)
            elif tag_type == 'group':
                groups.append(tag_name)
            elif tag_type == 'language':
                languages.append(tag_name)
            elif tag_type == 'category':
                categories.append(tag_name)

        return {
            'id': gallery_id, 'title': sanitize_filename(title_en),
            'title_jp': title_jp,
            'media_id': media_id, 'num_pages': num_pages, 'ext': ext,
            'parodies': parodies, 'tags': tags, 'artists': artists,
            'groups': groups, 'languages': languages, 'categories': categories,
            'upload_date': upload_date, 'favorites': favorites,
            'cover_url': f'https://t.nhentai.net/{cover_path}' if cover_path else '',
            'full_title': title_obj.get('pretty', ''),
        }, None

    def get_gallery_info_enhanced(self, gallery_id):
        return self.get_gallery_info(gallery_id)

    def get_real_image_url(self, gallery_id, page_num):
        for domain in self.mirror_domains:
            url = f'https://{domain}/g/{gallery_id}/{page_num}/'
            try:
                response = self.get_page(url)
                if not response:
                    continue
                match = re.search(r'(https?://i\d*\.nhentai\.net/galleries/\d+/\d+\.\w+)', response.text)
                if match:
                    return match.group(1)
            except Exception:
                continue
        return None

    def scan_existing(self, gallery_dir, num_pages, ext):
        missing = []
        for page_num in range(1, num_pages + 1):
            found = False
            for try_ext in ('webp', 'jpg', 'png', 'jpeg'):
                img_path = gallery_dir / f'{page_num:04d}.{try_ext}'
                if img_path.exists() and img_path.stat().st_size > 0:
                    try:
                        self._validate_image_file(img_path)
                        found = True
                        break
                    except Exception:
                        img_path.unlink(missing_ok=True)
            if not found:
                missing.append(page_num)
        return missing

    def find_existing_dir(self, gallery_id):
        if not self.output_dir.exists():
            return None
        prefix = f'{gallery_id}_'
        for d in self.output_dir.iterdir():
            if d.is_dir() and d.name.startswith(prefix):
                return d
        return None

    def rename_with_tag(self, gallery_dir, num_pages, missing_count):
        tagged = make_tagged_name(gallery_dir.name, num_pages, missing_count)
        if tagged != gallery_dir.name:
            new_path = gallery_dir.parent / tagged
            suffix = 1
            while new_path.exists() and new_path != gallery_dir:
                new_path = gallery_dir.parent / f'{tagged} ({suffix})'
                suffix += 1
            tmp_path = gallery_dir.parent / f'.{tagged}.{threading.get_ident()}.tmp'
            gallery_dir.rename(tmp_path)
            tmp_path.rename(new_path)
            return new_path
        return gallery_dir

    def save_metadata_txt(self, gallery_dir, info):
        lines = [
            f'画廊ID: {info["id"]}',
            f'英文标题: {info["title"]}',
        ]
        if info.get('title_jp'):
            lines.append(f'日文标题: {info["title_jp"]}')
        lines.extend([
            f'Media ID: {info["media_id"]}', f'页数: {info["num_pages"]}',
            f'图片格式: {info["ext"]}', f'链接: https://nhentai.net/g/{info["id"]}/'
        ])
        if info.get('upload_date'):
            lines.append(f'上传时间: {info["upload_date"]}')
        if info.get('favorites'):
            lines.append(f'收藏数: {info["favorites"]}')
        lines.append('')
        for category, items in [
            ('作品来源', info.get('parodies', [])), ('艺术家', info.get('artists', [])),
            ('团体', info.get('groups', [])), ('语言', info.get('languages', [])),
            ('分类', info.get('categories', [])), ('标签', info.get('tags', []))
        ]:
            if items:
                lines.append(f'━━ {category} ━━')
                for item in items:
                    lines.append(f'  {item}')
        txt_path = gallery_dir / '画廊信息.txt'
        _write_text_atomic(txt_path, '\n'.join(lines))

    def _download_headers(self, gallery_id, variant=0):
        """按尝试次数轮换请求头指纹，避免重试被识别为同一设备。"""
        ua, sec_ch_ua = HEADER_FINGERPRINTS[variant % len(HEADER_FINGERPRINTS)]
        return {
            'Referer': f'https://nhentai.net/g/{gallery_id or ""}/',
            'User-Agent': ua,
            'Sec-Ch-Ua': sec_ch_ua,
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Fetch-Dest': 'image',
            'Sec-Fetch-Mode': 'no-cors',
            'Sec-Fetch-Site': 'cross-site',
            'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
        }

    def download_image_with_progress(self, url, save_path, gallery_id=None, callback=None):
        save_path = Path(save_path)
        partial_path = save_path.with_name(save_path.name + '.part')
        last_error = ''
        for attempt in range(5):
            if self._stop_event.is_set() or self._stop_flag:
                return False, '用户取消'
            response = None
            lease = None
            try:
                transfer_complete = False
                existing = partial_path.stat().st_size if partial_path.exists() else 0
                headers = self._download_headers(gallery_id, attempt)
                if existing:
                    headers['Range'] = f'bytes={existing}-'
                lease = self.scheduler.acquire([endpoint_from_url(url)], cancel_event=self._stop_event)
                response = self._get_download_session(lease.proxy).get(
                    url, headers=headers, timeout=(12, 60), stream=True)
                code = response.status_code
                challenge = detect_challenge_response(response)
                if challenge:
                    raise ChallengeDetected(challenge)
                if code == 404:
                    lease.finish(status=code)
                    return False, '404 图片不存在'
                if code == 416 and existing:
                    partial_path.unlink(missing_ok=True)
                    lease.finish(status=code)
                    last_error = '断点位置无效，已重新开始下载'
                    continue
                if code not in (200, 206):
                    lease.finish(status=code, retry_after=response.headers.get('Retry-After'))
                    last_error = translate_http_status(code)
                    if attempt < 4:
                        self._stop_event.wait(min(12, (2 ** attempt) + random.random()))
                        continue
                    if gallery_id and hasattr(self, 'anti_crawl') and self._pause_enabled():
                        self.anti_crawl.on_failure()
                        code = classify_failure(last_error)['code']
                        return self._download_with_anti_crawl(url, save_path, gallery_id, callback,
                                                              failure_code=code)
                    return False, last_error

                self._validate_response_type(response)
                if code == 200 and existing:
                    partial_path.unlink(missing_ok=True)
                    existing = 0
                content_size = int(response.headers.get('Content-Length', 0) or 0)
                content_range = response.headers.get('Content-Range', '')
                range_match = re.fullmatch(r'bytes\s+(\d+)-(\d+)/(\d+|\*)', content_range.strip()) \
                    if content_range else None
                if code == 206:
                    if not range_match or int(range_match.group(1)) != existing:
                        partial_path.unlink(missing_ok=True)
                        lease.finish(status=code)
                        last_error = '服务器返回的断点范围无效，已重新开始下载'
                        continue
                if code == 206:
                    start, end = int(range_match.group(1)), int(range_match.group(2))
                    if end < start or (content_size and end - start + 1 != content_size):
                        partial_path.unlink(missing_ok=True)
                        lease.finish(status=code)
                        last_error = '服务器返回的断点长度无效，已重新开始下载'
                        continue
                    total_size = int(range_match.group(3)) if range_match.group(3) != '*' else end + 1
                else:
                    total_size = content_size
                downloaded = existing
                start_time = time.time()
                last_cb = start_time
                with open(partial_path, 'ab' if existing else 'wb') as f:
                    for chunk in response.iter_content(chunk_size=65536):
                        if self._stop_event.is_set() or self._stop_flag:
                            return False, '用户取消'
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        now = time.time()
                        if now - last_cb >= 0.1 and callback:
                            elapsed = now - start_time
                            speed = downloaded / elapsed if elapsed > 0 else 0
                            remaining = (total_size - downloaded) / speed if speed > 0 else 0
                            callback(downloaded, total_size, speed, remaining)
                            last_cb = now
                    f.flush()
                    os.fsync(f.fileno())
                actual_size = partial_path.stat().st_size
                if actual_size == 0:
                    raise IOError('响应为空')
                if total_size and actual_size != total_size:
                    raise IOError(f'数据传输中断: 预期 {total_size} 字节，实际 {actual_size} 字节')
                transfer_complete = True
                self._validate_image_file(partial_path)
                partial_path.replace(save_path)
                lease.finish(status=code)
                if callback:
                    callback(actual_size, actual_size, 0, 0)
                return True, None
            except Exception as e:
                if lease is not None:
                    lease.finish(error=e)
                last_error = str(e)
                if transfer_complete or 'cannot identify image' in last_error.lower() or 'truncated' in last_error.lower():
                    partial_path.unlink(missing_ok=True)
                if attempt < 4:
                    self._stop_event.wait(min(15, (2 ** attempt) + random.uniform(0.2, 1.0)))
                    continue
                if gallery_id and hasattr(self, 'anti_crawl') and self._pause_enabled():
                    self.anti_crawl.on_failure()
                    code = classify_failure(last_error)['code']
                    return self._download_with_anti_crawl(url, save_path, gallery_id, callback,
                                                          failure_code=code)
            finally:
                if response is not None:
                    response.close()
        return False, classify_failure(last_error)['reason']

    def _download_with_anti_crawl(self, url, save_path, gallery_id, callback=None,
                                  failure_code=None):
        if not hasattr(self, 'anti_crawl'):
            return False, '反爬管理器未初始化'
        try:
            lease = self.scheduler.acquire([endpoint_from_url(url)], cancel_event=self._stop_event)
        except InterruptedError:
            return False, '用户取消'
        result = self.anti_crawl.download_with_anti_crawl(
            url, save_path, gallery_id, lease.proxy, timeout=30, failure_code=failure_code)
        success, error = result if isinstance(result, tuple) else (bool(result), None)
        if success:
            lease.finish(status=200)
        else:
            lease.finish(error=RuntimeError(error or '反爬升级失败'))
        if success and save_path.exists() and callback:
            size = save_path.stat().st_size
            callback(size, size, 0, 0)
        return success, error

    def download_single_page(self, gallery_id, page_num, gallery_dir, media_id, ext, callback=None):
        def prog_cb(dl, total, spd, rem):
            if callback:
                callback('file_progress', gallery_id, {
                    'page': page_num, 'downloaded': dl, 'total': total,
                    'speed': spd, 'remaining': rem
                })

        def download(url, path):
            if path.exists() and path.stat().st_size > 0:
                try:
                    self._validate_image_file(path)
                    return True, None
                except Exception:
                    path.unlink(missing_ok=True)
            return self.download_image_with_progress(
                url, path, gallery_id, callback=prog_cb if callback else None)

        # 直接构造图片 URL，失败才解析页面真实地址，避免每页多一次页面请求
        image_path = gallery_dir / f'{page_num:04d}.{ext}'
        success, err = download(
            f'https://i.nhentai.net/galleries/{media_id}/{page_num}.{ext}', image_path)
        final_path = image_path
        if not success:
            real_url = self.get_real_image_url(gallery_id, page_num)
            if real_url:
                real_ext = real_url.split('.')[-1].lower()
                if real_ext not in ('jpg', 'jpeg', 'png', 'webp', 'gif'):
                    real_ext = ext
                real_path = gallery_dir / f'{page_num:04d}.{real_ext}'
                final_path = real_path
                success, err = download(real_url, real_path)

        if not success:
            self.errors.append({'gallery_id': gallery_id, 'type': 'page', 'page': page_num,
                                'error': err, 'failure': classify_failure(err)})
        if callback:
            callback('thread_log', gallery_id, {
                'page': page_num, 'success': success,
                'thread': threading.current_thread().name, 'error': err,
                'size': final_path.stat().st_size if success else 0
            })
        return success

    def download_gallery(self, gallery_id, callback=None):
        lock = _gallery_lock(self.output_dir, gallery_id)
        if not lock.acquire(blocking=False):
            if callback:
                callback('error', gallery_id, '该画廊正在被其他任务下载')
            return False
        try:
            return self._download_gallery_locked(gallery_id, callback)
        finally:
            lock.release()

    def _download_gallery_locked(self, gallery_id, callback=None):
        if self._stop_event.is_set():
            return False
        with self.bytes_lock:
            start_bytes = self.total_downloaded_bytes
        info, error = self.get_gallery_info(gallery_id)
        if error:
            self.errors.append({'gallery_id': gallery_id, 'type': 'info', 'page': 0, 'error': error})
            if callback:
                callback('error', gallery_id, error)
            return False
        if callback:
            callback('gallery_info', gallery_id, info)

        base_name = f"{gallery_id}_{info['title']}"
        existing_dir = self.find_existing_dir(gallery_id)
        gallery_dir = existing_dir if existing_dir else self.output_dir / base_name
        gallery_dir.mkdir(parents=True, exist_ok=True)

        info['last_update'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        _write_json_atomic(gallery_dir / 'info.json', info)
        self.save_metadata_txt(gallery_dir, info)

        num_pages = info['num_pages']
        ext = info['ext']
        media_id = info['media_id']

        missing = self.scan_existing(gallery_dir, num_pages, ext)
        already_done = num_pages - len(missing)

        if len(missing) == 0:
            gallery_dir = self.rename_with_tag(gallery_dir, num_pages, 0)
            if callback:
                callback('complete', gallery_id, {
                    'title': info['title'],
                    'downloaded': num_pages, 'total': num_pages,
                    'missing': 0, 'skipped': True, 'dir_name': gallery_dir.name,
                    'files': self._list_gallery_files(gallery_dir),
                    'path': str(gallery_dir),
                })
            return True

        if callback:
            callback('start', gallery_id, {
                'total': num_pages, 'already': already_done,
                'missing': len(missing), 'dir_name': gallery_dir.name,
            })

        round_num = 0
        current_missing = list(missing)

        while current_missing and round_num < self.max_rounds:
            if self._stop_event.is_set() or self._stop_flag:
                break
            round_num += 1
            if round_num > 1 and callback:
                callback('retry', gallery_id, {
                    'round': round_num, 'max_rounds': self.max_rounds,
                    'remaining': len(current_missing),
                })
                time.sleep(random.uniform(2, 4))

            still_missing = []
            with ThreadPoolExecutor(max_workers=self.workers) as executor:
                future_to_page = {}
                for page_num in current_missing:
                    future = executor.submit(
                        self.download_single_page,
                        gallery_id, page_num, gallery_dir, media_id, ext, callback
                    )
                    future_to_page[future] = page_num

                for future in as_completed(future_to_page):
                    page_num = future_to_page[future]
                    try:
                        success = future.result()
                        if not success:
                            still_missing.append(page_num)
                    except Exception as e:
                        still_missing.append(page_num)
                        self.errors.append({'gallery_id': gallery_id, 'type': 'page',
                                            'page': page_num, 'error': str(e)})
                        if callback:
                            callback('page_error', gallery_id, {'page': page_num, 'error': str(e)})

            current_missing = self.scan_existing(gallery_dir, num_pages, ext)
            if current_missing and round_num < self.max_rounds:
                time.sleep(random.uniform(1, 2))

        final_missing = self.scan_existing(gallery_dir, num_pages, ext)
        downloaded = num_pages - len(final_missing)

        if self._stop_event.is_set() or self._stop_flag:
            if callback:
                callback('cancelled', gallery_id, {
                    'title': info['title'], 'downloaded': downloaded,
                    'total': num_pages, 'missing': len(final_missing),
                    'dir_name': gallery_dir.name, 'path': str(gallery_dir),
                    'files': self._list_gallery_files(gallery_dir),
                })
            return False

        if len(final_missing) > 0:
            report_path = gallery_dir / '缺失页报告.txt'
            report_lines = [
                f'画廊: {gallery_id} - {info["title"]}',
                f'总页数: {num_pages}',
                f'已下载: {downloaded}',
                f'缺失页: {len(final_missing)}',
            ]
            for p in final_missing:
                record = next((e for e in reversed(self.errors) if e.get('page') == p), {})
                failure = record.get('failure') or classify_failure(record.get('error', ''))
                report_lines.append(
                    f'  第{p}页 [{failure["code"]}] {failure["reason"]}: {failure["detail"]}')
            _write_text_atomic(report_path, '\n'.join(report_lines) + '\n')
        else:
            (gallery_dir / '缺失页报告.txt').unlink(missing_ok=True)

        gallery_dir = self.rename_with_tag(gallery_dir, num_pages, len(final_missing))
        with self.bytes_lock:
            gallery_bytes = max(0, self.total_downloaded_bytes - start_bytes)
        if callback:
            callback('complete', gallery_id, {
                'title': info['title'],
                'downloaded': downloaded, 'total': num_pages,
                'missing': len(final_missing), 'skipped': False,
                'dir_name': gallery_dir.name,
                'files': self._list_gallery_files(gallery_dir),
                'total_bytes': gallery_bytes,
                'path': str(gallery_dir),
            })
        return downloaded == num_pages

    def _list_gallery_files(self, gallery_dir):
        files = []
        if gallery_dir.exists():
            for f in sorted(gallery_dir.iterdir()):
                if f.is_file():
                    files.append({'name': f.name, 'size': f.stat().st_size})
        return files

    def test_proxy_speed(self):
        start = time.time()
        lease = None
        try:
            lease = self.scheduler.acquire(['nhentai.net'], cancel_event=self._stop_event)
            with self._get_download_session(lease.proxy).get('https://nhentai.net', timeout=10) as r:
                latency = (time.time() - start) * 1000
                lease.finish(status=r.status_code)
                if r.status_code == 200:
                    return True, latency
                return False, f"HTTP {r.status_code}"
        except Exception as e:
            if lease is not None:
                lease.finish(error=e)
            return False, str(e)

    def close(self):
        with self._owned_sessions_lock:
            sessions = list(self._owned_sessions)
            self._owned_sessions.clear()
        sessions.extend((self.session, self.download_session))
        for session in sessions:
            if hasattr(session, 'close'):
                try:
                    session.close()
                except Exception:
                    pass
        if self.stealth_session and hasattr(self.stealth_session, 'close'):
            try:
                self.stealth_session.close()
            except Exception:
                pass
        self.anti_crawl.cleanup()
