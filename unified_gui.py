#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一下载器 - Nhentai + JM Comic 双引擎
可视化合集面板、封面预览、多线程下载
"""

import os
import re
import time
import json
import random
import threading
import io
import queue
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageTk
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

_GALLERY_LOCKS = {}
_GALLERY_LOCKS_GUARD = threading.Lock()


def _gallery_lock(output_dir, gallery_id):
    key = (str(Path(output_dir).resolve()), str(gallery_id))
    with _GALLERY_LOCKS_GUARD:
        return _GALLERY_LOCKS.setdefault(key, threading.RLock())

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

# ==================== JM Comic 导入 ====================
JM_AVAILABLE = False
JmOption = None
JmModuleConfig = None
download_album = None
download_photo = None
try:
    from jmcomic.api import *
    JmOption = None
    import jmcomic
    JmModuleConfig = jmcomic.JmModuleConfig
    JmOption = jmcomic.JmOption
    JM_AVAILABLE = True
except ImportError:
    pass

from utils import (
    MAX_FILENAME_LEN, TAG_OK, TAG_FAIL_PREFIX, TAG_FAIL_SUFFIX,
    JM_COLLECTION_FILE, ERROR_CN, HTTP_CN,
    load_collection_ids, get_collection_desc,
    translate_error, translate_http_status,
    decode_utf8_response, repair_mojibake,
    sanitize_filename, strip_status_tag, make_tagged_name,
    format_size, format_time, format_speed,
    parse_gallery_status, get_cached_title,
    load_download_index, update_download_index,
    classify_failure, load_app_state, save_app_state, update_app_state,
    append_download_history,
)
from adaptive_scheduler import (
    AdaptiveScheduler, ChallengeDetected, JmAdaptiveStrategy,
    detect_challenge_response, endpoint_from_url, parse_proxy_pool,
)


# ==================== JM 合集面板 ====================
class JMCollectionPanel(ttk.Frame):
    def __init__(self, parent, gui):
        super().__init__(parent)
        self.gui = gui
        self.columns = 5
        self._photos = []
        self._cover_gen = 0
        self._cover_cache_dir = Path(__file__).parent / '.cache' / 'collection_covers'
        self._cover_cache_dir.mkdir(parents=True, exist_ok=True)
        self.setup_ui()

    def setup_ui(self):
        header = ttk.Frame(self)
        header.pack(fill=tk.X, pady=(0, 4))

        ttk.Label(header, text='精选合集',
                  font=('Microsoft YaHei UI', 11, 'bold')).pack(side=tk.LEFT, padx=8, pady=5)
        self.stats_label = ttk.Label(header, text='', foreground='gray')
        self.stats_label.pack(side=tk.LEFT, padx=(4, 0))

        self.download_all_btn = ttk.Button(header, text='下载全部合集',
                                           command=self.download_all, width=12)
        self.download_all_btn.pack(side=tk.RIGHT, padx=4)
        self.refresh_btn = ttk.Button(header, text='刷新状态',
                                      command=self.build_collection, width=8)
        self.refresh_btn.pack(side=tk.RIGHT, padx=2)

        self.canvas = tk.Canvas(self, highlightthickness=0, bg='#f0f0f0')
        self.scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.inner_frame = ttk.Frame(self.canvas)
        self._canvas_window = self.canvas.create_window((0, 0), window=self.inner_frame, anchor='nw')
        self.inner_frame.bind('<Configure>', lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', self._on_canvas_configure)
        self._wheel_binds = []
        for w in (self.canvas, self.inner_frame):
            w.bind('<MouseWheel>', self._on_mousewheel)
            w.bind('<Enter>', lambda e, widget=w: widget.bind_all('<MouseWheel>', self._on_mousewheel))
            w.bind('<Leave>', lambda e: self.canvas.unbind_all('<MouseWheel>'))

        self.build_collection()

    def _on_canvas_configure(self, event):
        width = event.width
        self.canvas.itemconfigure(self._canvas_window, width=width)
        self.columns = max(2, width // 230)
        if getattr(self, '_resize_after', None):
            self.after_cancel(self._resize_after)
        self._resize_after = self.after(200, self.build_collection)

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), 'units')

    def build_collection(self):
        self._cover_gen += 1
        gen = self._cover_gen
        for widget in self.inner_frame.winfo_children():
            widget.destroy()
        self._photos.clear()

        collection_ids = load_collection_ids()
        output_dir = self.gui.nh_output_var.get()
        stats = {'complete': 0, 'partial': 0, 'downloaded': 0, 'none': 0}

        cover_items = []
        for idx, gid in enumerate(collection_ids):
            row = idx // self.columns
            col = idx % self.columns

            status, dir_name, dir_path = parse_gallery_status(gid, output_dir)
            stats[status] = stats.get(status, 0) + 1
            title = get_cached_title(gid, output_dir)

            if status == 'complete':
                status_text, status_fg = '完整', '#16733c'
            elif status == 'partial':
                status_text, status_fg = '缺页', '#b35c00'
            elif status == 'downloaded':
                status_text, status_fg = '已下载', '#0969da'
            else:
                status_text, status_fg = '未下载', '#777777'

            card = tk.Frame(self.inner_frame, bg='white', relief=tk.SOLID,
                            bd=1, width=214, height=354)
            card.grid(row=row, column=col, padx=7, pady=7, sticky='n')
            card.grid_propagate(False)

            cover_frame = tk.Frame(card, bg='#e9ecef', width=196, height=258)
            cover_frame.pack(padx=8, pady=(8, 5))
            cover_frame.pack_propagate(False)
            thumb = tk.Label(cover_frame, text='读取封面...', bg='#e9ecef', fg='#777777',
                             font=('Microsoft YaHei UI', 8))
            thumb.pack(fill=tk.BOTH, expand=True)

            display_title = title if title != gid else '---'
            tk.Label(card, text=display_title, bg='white', anchor=tk.W, justify=tk.LEFT,
                     font=('Microsoft YaHei UI', 9, 'bold'), fg='#24292f',
                     wraplength=194, height=2).pack(fill=tk.X, padx=9)

            meta = tk.Frame(card, bg='white')
            meta.pack(fill=tk.X, padx=9, pady=(2, 0))
            tk.Label(meta, text=f'#{gid}', bg='white', fg='#666666',
                     font=('Consolas', 8)).pack(side=tk.LEFT)
            tk.Label(meta, text=status_text, bg='white', fg=status_fg,
                     font=('Microsoft YaHei UI', 8, 'bold')).pack(side=tk.RIGHT)

            btn_frame = tk.Frame(card, bg='white')
            btn_frame.pack(fill=tk.X, padx=8, pady=(5, 7))

            dl_btn = tk.Label(btn_frame, text='下载', bg='#4a9eff', fg='white',
                              font=('Microsoft YaHei UI', 8), cursor='hand2', pady=3)
            dl_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)
            dl_btn.bind('<Button-1>', lambda e, gid=gid: self.download_single(gid))

            if dir_path and dir_path.exists():
                open_btn = tk.Label(btn_frame, text='打开', bg='#555555', fg='white',
                                    font=('Microsoft YaHei UI', 8), cursor='hand2',
                                    padx=12, pady=3)
                open_btn.pack(side=tk.RIGHT, padx=(5, 0))
                open_btn.bind('<Button-1>', lambda e, d=dir_path: os.startfile(str(d)) if d.exists() else None)

            cover_items.append({'gid': gid, 'thumb': thumb, 'dir_path': dir_path})

        for c in range(self.columns):
            self.inner_frame.columnconfigure(c, weight=1)

        self.stats_label.config(
            text=f'完整:{stats["complete"]}  缺页:{stats["partial"]}  未下载:{stats["none"]}',
            foreground='#333333')
        self._load_covers_async(cover_items, gen, output_dir)

    @staticmethod
    def _resize_cover(image, size=(196, 258)):
        image = image.convert('RGB')
        target_w, target_h = size
        ratio = max(target_w / image.width, target_h / image.height)
        resized = image.resize((max(1, int(image.width * ratio)),
                                max(1, int(image.height * ratio))), Image.Resampling.LANCZOS)
        left = max(0, (resized.width - target_w) // 2)
        top = max(0, (resized.height - target_h) // 2)
        return resized.crop((left, top, left + target_w, top + target_h))

    def _load_covers_async(self, items, gen, output_dir):
        proxy = self.gui.nh_proxy_var.get().strip() or None

        def load_one(item):
            gid = item['gid']
            candidates = []
            if item['dir_path'] and item['dir_path'].exists():
                for suffix in ('*.jpg', '*.jpeg', '*.png', '*.webp'):
                    candidates.extend(sorted(item['dir_path'].glob(suffix)))
                    if candidates:
                        break
            cache_path = self._cover_cache_dir / f'{gid}.jpg'
            if cache_path.exists():
                candidates.append(cache_path)
            for path in candidates:
                try:
                    with Image.open(path) as image:
                        return self._resize_cover(image)
                except Exception:
                    if path == cache_path:
                        path.unlink(missing_ok=True)

            crawler = NHentaiCrawler(proxy=proxy, output_dir=output_dir,
                                      stealth_mode=False, workers=1, speed_mode='保守')
            try:
                info, error = crawler.get_gallery_info(gid)
                if error or not info or not info.get('cover_url'):
                    return None
                response = crawler._get_download_session().get(
                    info['cover_url'], headers={'Referer': f'https://nhentai.net/g/{gid}/'},
                    timeout=(8, 25))
                try:
                    response.raise_for_status()
                    image = Image.open(io.BytesIO(response.content))
                    image.load()
                    full = image.convert('RGB')
                    temp = cache_path.with_name(cache_path.stem + '.part.jpg')
                    full.save(temp, 'JPEG', quality=88)
                    temp.replace(cache_path)
                    return self._resize_cover(full)
                finally:
                    response.close()
            except Exception:
                return None
            finally:
                crawler.close()

        def run():
            with ThreadPoolExecutor(max_workers=min(4, len(items))) as pool:
                futures = {pool.submit(load_one, item): item for item in items}
                for future in as_completed(futures):
                    if gen != self._cover_gen:
                        return
                    try:
                        image = future.result()
                    except Exception:
                        image = None
                    self.gui.post(self._apply_cover, futures[future], image, gen)

        if items:
            threading.Thread(target=run, daemon=True).start()

    def _apply_cover(self, item, image, gen):
        if gen != self._cover_gen:
            return
        try:
            if image is None:
                item['thumb'].config(text='封面暂不可用')
                return
            photo = ImageTk.PhotoImage(image)
            item['thumb'].config(image=photo, text='')
            self._photos.append(photo)
        except tk.TclError:
            pass

    def download_single(self, gallery_id):
        self.gui.notebook.select(0)
        self.gui.nhentai_tab.input_text.delete(1.0, tk.END)
        self.gui.nhentai_tab.input_text.insert(1.0, gallery_id)
        self.gui.nhentai_tab.start_download()

    def download_all(self):
        collection_ids = load_collection_ids()
        if messagebox.askyesno('确认', f'将下载合集全部 {len(collection_ids)} 个画廊，确认？'):
            all_ids = '\n'.join(collection_ids)
            self.gui.notebook.select(0)
            self.gui.nhentai_tab.input_text.delete(1.0, tk.END)
            self.gui.nhentai_tab.input_text.insert(1.0, all_ids)
            self.gui.nhentai_tab.start_download()


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
        try:
            ua, platform = self._ua_pool[self.last_ua_idx % len(self._ua_pool)]
            self.last_ua_idx += 1
            resp = curl_requests.get(url,
                                     headers={
                                         'Referer': f'https://nhentai.net/g/{gallery_id}/',
                                         'Accept': 'image/avif,image/webp,image/apng,*/*',
                                     },
                                     impersonate='chrome120',
                                     proxy=proxy,
                                     timeout=timeout,
                                     stream=True)
            if resp.status_code == 200:
                self._save_stream_atomically(resp, save_path)
                self.success_count += 1
                return True, None
            return False, f'TLS:HTTP{resp.status_code}'
        except Exception as e:
            return False, f'TLS:{e}'

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
        import re as _re
        m = _re.match(r'https?://(i\d*)\.[^/]+/(.+)', url)
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
                driver = await uc.start(headless=True)
                try:
                    page = await driver.get(url)
                    await asyncio.sleep(2)
                    raw = await page.evaluate('document.body.innerText')
                    if 'Access denied' in str(raw) or '403' in str(raw):
                        return False, '浏览器:被拒绝'
                    content = await page.evaluate("""
                        async () => {
                            const r = await fetch(arguments[0]);
                            const b = await r.arrayBuffer();
                            return Array.from(new Uint8Array(b));
                        }
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

    def download_with_anti_crawl(self, url, save_path, gallery_id, proxy=None, timeout=30):
        if not self._strategy_lock.acquire(timeout=timeout):
            return False, '反爬升级链繁忙，等待下一轮补页'
        strategies = [
            (self._try_curl_cffi, 'curl_cffi TLS指纹'),
            (self._try_stealth_http, 'HTTP隐身'),
            (self._try_cdn_rotate, 'CDN轮换'),
            (self._try_browser, '真实浏览器'),
            (self._try_external_bypass, '代理旁路'),
        ]
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
            return False, f'全部5级失败: {last_error}'
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
class NHentaiCrawler:
    def __init__(self, proxy=None, output_dir='./downloads', quality='high',
                 max_rounds=5, stealth_mode=True, use_browser_fallback=True,
                 workers=12, speed_mode='极速', scheduler=None,
                 browser_priority=False):
        self.proxy_pool = parse_proxy_pool(proxy)
        self.proxy = next((item for item in self.proxy_pool if item), None)
        self.output_dir = Path(output_dir)
        self.quality = quality
        self.max_rounds = max_rounds
        self.stealth_mode = stealth_mode and SCRAPLING_AVAILABLE
        self.use_browser_fallback = use_browser_fallback
        self.workers = workers
        self.speed_mode = speed_mode
        self._apply_speed_mode()
        self.scheduler = scheduler or AdaptiveScheduler(
            max_concurrency=self.workers, min_concurrency=1,
            failure_threshold=3, cooldown=20,
            recovery_successes=8, proxies=self.proxy_pool,
            initial_concurrency=2 if self.speed_mode == '保守' else 4)
        self.scheduler.configure(
            browser_priority=browser_priority,
            request_interval=0.15 if self.speed_mode == '保守' else 0.05)

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
        page = DynamicFetcher.fetch(url, headless=True, wait_until='networkidle', timeout=30000)
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
        url = f'https://nhentai.net/g/{gallery_id}/'
        response = self.get_page(url)
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
            from datetime import datetime as _dt
            upload_date = _dt.fromtimestamp(upload_ts).strftime('%Y-%m-%d %H:%M:%S')
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
        url = f'https://nhentai.net/g/{gallery_id}/{page_num}/'
        try:
            response = self.get_page(url)
            if not response:
                return None
            match = re.search(r'(https?://i\d*\.nhentai\.net/galleries/\d+/\d+\.\w+)', response.text)
            if match:
                return match.group(1)
        except Exception:
            pass
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
        with open(txt_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))

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
                headers = {'Referer': f'https://nhentai.net/g/{gallery_id}/'}
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
                if code not in (200, 206):
                    lease.finish(status=code, retry_after=response.headers.get('Retry-After'))
                    last_error = translate_http_status(code)
                    if attempt < 4:
                        self._stop_event.wait(min(12, (2 ** attempt) + random.random()))
                        continue
                    if gallery_id and hasattr(self, 'anti_crawl'):
                        self.anti_crawl.on_failure()
                        return self._download_with_anti_crawl(url, save_path, gallery_id, callback)
                    return False, last_error

                self._validate_response_type(response)
                if code == 200 and existing:
                    partial_path.unlink(missing_ok=True)
                    existing = 0
                content_size = int(response.headers.get('Content-Length', 0) or 0)
                total_size = existing + content_size if code == 206 else content_size
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
                if gallery_id and hasattr(self, 'anti_crawl'):
                    self.anti_crawl.on_failure()
                    return self._download_with_anti_crawl(url, save_path, gallery_id, callback)
            finally:
                if response is not None:
                    response.close()
        return False, classify_failure(last_error)['reason']

    def _download_with_anti_crawl(self, url, save_path, gallery_id, callback=None):
        if not hasattr(self, 'anti_crawl'):
            return False, '反爬管理器未初始化'
        try:
            lease = self.scheduler.acquire([endpoint_from_url(url)], cancel_event=self._stop_event)
        except InterruptedError:
            return False, '用户取消'
        result = self.anti_crawl.download_with_anti_crawl(
            url, save_path, gallery_id, lease.proxy, timeout=30)
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
        image_url = self.get_real_image_url(gallery_id, page_num)
        if not image_url:
            image_url = f'https://i.nhentai.net/galleries/{media_id}/{page_num}.{ext}'

        real_ext = image_url.split('.')[-1]
        image_path = gallery_dir / f'{page_num:04d}.{real_ext}'

        if image_path.exists() and image_path.stat().st_size > 0:
            try:
                self._validate_image_file(image_path)
                return True
            except Exception:
                image_path.unlink(missing_ok=True)

        def prog_cb(dl, total, spd, rem):
            if callback:
                callback('file_progress', gallery_id, {
                    'page': page_num, 'downloaded': dl, 'total': total,
                    'speed': spd, 'remaining': rem
                })

        success, err = self.download_image_with_progress(image_url, image_path, gallery_id, callback=prog_cb if callback else None)
        if not success:
            self.errors.append({'gallery_id': gallery_id, 'type': 'page', 'page': page_num,
                                'error': err, 'failure': classify_failure(err)})
        if callback:
            callback('thread_log', gallery_id, {
                'page': page_num, 'success': success,
                'thread': threading.current_thread().name, 'error': err,
                'size': image_path.stat().st_size if success else 0
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
        with open(gallery_dir / 'info.json', 'w', encoding='utf-8') as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
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
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write(f'画廊: {gallery_id} - {info["title"]}\n总页数: {num_pages}\n已下载: {downloaded}\n缺失页: {len(final_missing)}\n')
                for p in final_missing:
                    record = next((e for e in reversed(self.errors) if e.get('page') == p), {})
                    failure = record.get('failure') or classify_failure(record.get('error', ''))
                    f.write(f'  第{p}页 [{failure["code"]}] {failure["reason"]}: {failure["detail"]}\n')
        else:
            (gallery_dir / '缺失页报告.txt').unlink(missing_ok=True)

        gallery_dir = self.rename_with_tag(gallery_dir, num_pages, len(final_missing))
        if callback:
            callback('complete', gallery_id, {
                'title': info['title'],
                'downloaded': downloaded, 'total': num_pages,
                'missing': len(final_missing), 'skipped': False,
                'dir_name': gallery_dir.name,
                'files': self._list_gallery_files(gallery_dir),
                'total_bytes': self.total_downloaded_bytes,
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


# ==================== 悬浮胶囊类 ====================
class FloatingCapsule:
    def __init__(self, main_gui):
        self.main_gui = main_gui
        self.win = tk.Toplevel(main_gui.root)
        self.win.title('')
        self.win.overrideredirect(True)
        self.win.attributes('-topmost', True)
        self.win.attributes('-alpha', 0.92)
        self.drag_data = {'x': 0, 'y': 0}
        self.expanded = False
        self.queue = []
        self.downloading = False
        self.current_gid = ''
        self.processed_ids = set()
        self._stop_event = threading.Event()
        self._crawler = None
        self._clipboard_after_id = None
        self.setup_ui()
        self.setup_position()
        self.setup_bindings()
        self.win.protocol("WM_DELETE_WINDOW", self.on_close)
        self.start_clipboard_check()

    def setup_ui(self):
        self.container = tk.Frame(self.win, bg='#2d2d2d', bd=0,
                                  highlightthickness=2, highlightbackground='#555555')
        self.container.pack(fill=tk.BOTH, expand=True)
        self.bar_frame = tk.Frame(self.container, bg='#2d2d2d')
        self.bar_frame.pack(fill=tk.X, padx=4, pady=3)
        self.icon_label = tk.Label(self.bar_frame, text='?', bg='#2d2d2d', fg='white',
                                   font=('Segoe UI', 11), cursor='fleur')
        self.icon_label.pack(side=tk.LEFT, padx=(2, 4))
        self.status_label = tk.Label(self.bar_frame, text='粘贴ID下载', bg='#2d2d2d',
                                     fg='#aaaaaa', font=('Microsoft YaHei', 8))
        self.status_label.pack(side=tk.LEFT, padx=2)
        self.expand_btn = tk.Label(self.bar_frame, text='?', bg='#2d2d2d',
                                   fg='#888888', font=('Segoe UI', 8), cursor='hand2')
        self.expand_btn.pack(side=tk.RIGHT, padx=2)
        self.expand_btn.bind('<Button-1>', self.toggle_expand)
        self.count_label = tk.Label(self.bar_frame, text='0', bg='#4a9eff', fg='white',
                                    font=('Segoe UI', 7, 'bold'), width=2)
        self.count_label.pack(side=tk.RIGHT, padx=2)

        self.expand_frame = tk.Frame(self.container, bg='#2d2d2d')
        input_row = tk.Frame(self.expand_frame, bg='#2d2d2d')
        input_row.pack(fill=tk.X, padx=4, pady=(4, 2))
        self.entry = tk.Entry(input_row, bg='#3a3a3a', fg='white', insertbackground='white',
                              font=('Consolas', 10), relief=tk.FLAT,
                              highlightthickness=1, highlightbackground='#555555')
        self.entry.pack(fill=tk.X, ipady=3)
        self.entry.insert(0, '粘贴画廊ID或URL...')
        self.entry.config(fg='#777777')
        self.entry.bind('<FocusIn>', self.on_focus_in)
        self.entry.bind('<FocusOut>', self.on_focus_out)
        self.entry.bind('<Return>', self.on_submit)
        self.entry.bind('<Control-v>', self.on_paste)
        self.entry.bind('<Button-3>', self.on_right_click)

        list_frame = tk.Frame(self.expand_frame, bg='#2d2d2d')
        list_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))
        self.queue_listbox = tk.Listbox(list_frame, bg='#3a3a3a', fg='#cccccc',
                                        font=('Consolas', 8), selectbackground='#4a9eff',
                                        relief=tk.FLAT, height=5, highlightthickness=0)
        scrollbar = tk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.queue_listbox.yview)
        self.queue_listbox.configure(yscrollcommand=scrollbar.set)
        self.queue_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        btn_row = tk.Frame(self.expand_frame, bg='#2d2d2d')
        btn_row.pack(fill=tk.X, padx=4, pady=(0, 4))
        self.clear_btn = tk.Label(btn_row, text='清空', bg='#555555', fg='#cccccc',
                                  font=('Microsoft YaHei', 7), cursor='hand2', padx=8, pady=1)
        self.clear_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.clear_btn.bind('<Button-1>', self.clear_queue)
        self.open_btn = tk.Label(btn_row, text='主窗口', bg='#555555', fg='#cccccc',
                                 font=('Microsoft YaHei', 7), cursor='hand2', padx=8, pady=1)
        self.open_btn.pack(side=tk.LEFT)
        self.open_btn.bind('<Button-1>', self.open_main_window)

    def setup_position(self):
        self.win.update_idletasks()
        w, h = 200, 35
        x = self.win.winfo_screenwidth() - w - 20
        y = 50
        self.win.geometry(f'{w}x{h}+{x}+{y}')

    def setup_bindings(self):
        for widget in [self.container, self.bar_frame, self.icon_label, self.status_label]:
            widget.bind('<Button-1>', self.start_drag)
            widget.bind('<B1-Motion>', self.do_drag)
            widget.bind('<ButtonRelease-1>', self.stop_drag)
        self.icon_label.bind('<Double-Button-1>', lambda e: self.toggle_expand())

    def start_clipboard_check(self):
        self._clipboard_after_id = self.win.after(2000, self.check_clipboard)

    def check_clipboard(self):
        if not self.win.winfo_exists():
            return
        try:
            text = self.win.clipboard_get()
            if text:
                ids = self.extract_ids(text)
                new_ids = [g for g in ids if g not in self.queue and
                           g != self.current_gid and g not in self.processed_ids]
                if new_ids:
                    for gid in new_ids:
                        self.add_to_queue(gid)
                    self.status_label.config(text=f'检测到: {new_ids[0]}', fg='#4a9eff')
                    self.process_queue()
        except tk.TclError:
            pass
        self._clipboard_after_id = self.win.after(2000, self.check_clipboard)

    def on_close(self):
        self._stop_event.set()
        if self._crawler:
            self._crawler.stop()
        if self._clipboard_after_id:
            self.win.after_cancel(self._clipboard_after_id)
        self.win.destroy()
        self.main_gui.capsule = None
        try:
            self.main_gui.capsule_btn.config(text='开启胶囊')
        except tk.TclError:
            pass

    def start_drag(self, event):
        self.drag_data['x'] = event.x
        self.drag_data['y'] = event.y

    def do_drag(self, event):
        x = self.win.winfo_x() + event.x - self.drag_data['x']
        y = self.win.winfo_y() + event.y - self.drag_data['y']
        self.win.geometry(f'+{x}+{y}')

    def stop_drag(self, event):
        self.drag_data['x'] = 0
        self.drag_data['y'] = 0

    def toggle_expand(self, event=None):
        if self.expanded:
            self.expand_frame.pack_forget()
            self.win.geometry('200x35')
            self.expand_btn.config(text='?')
        else:
            self.expand_frame.pack(fill=tk.BOTH, expand=True, after=self.bar_frame)
            self.win.geometry('200x220')
            self.expand_btn.config(text='?')
        self.expanded = not self.expanded

    def on_focus_in(self, event):
        if self.entry.get() == '粘贴画廊ID或URL...':
            self.entry.delete(0, tk.END)
            self.entry.config(fg='white')

    def on_focus_out(self, event):
        if not self.entry.get().strip():
            self.entry.insert(0, '粘贴画廊ID或URL...')
            self.entry.config(fg='#777777')

    def on_paste(self, event):
        self.win.after(50, self.process_paste)
        return None

    def process_paste(self):
        text = self.entry.get().strip()
        if not text or text == '粘贴画廊ID或URL...':
            return
        ids = self.extract_ids(text)
        if ids:
            for gid in ids:
                self.add_to_queue(gid)
            self.entry.delete(0, tk.END)
            self.flush_clipboard()
            self.process_queue()

    def on_submit(self, event):
        text = self.entry.get().strip()
        if not text or text == '粘贴画廊ID或URL...':
            return
        ids = self.extract_ids(text)
        if ids:
            for gid in ids:
                self.add_to_queue(gid)
            self.entry.delete(0, tk.END)
            self.process_queue()

    def on_right_click(self, event):
        try:
            text = self.win.clipboard_get()
            ids = self.extract_ids(text)
            if ids:
                for gid in ids:
                    self.add_to_queue(gid)
                self.flush_clipboard()
                self.process_queue()
        except tk.TclError:
            pass

    def extract_ids(self, text):
        ids = []
        if text.isdigit():
            ids.append(text)
        else:
            ids.extend(re.findall(r'/g/(\d+)', text))
            for part in re.split(r'[\s,;]+', text):
                part = part.strip()
                if part.isdigit():
                    ids.append(part)
        return ids

    def add_to_queue(self, gallery_id):
        if gallery_id not in self.queue:
            self.queue.append(gallery_id)
            self.queue_listbox.insert(tk.END, gallery_id)
            self.update_count()

    def update_count(self):
        cnt = len(self.queue)
        self.count_label.config(text=str(cnt))
        self.count_label.config(bg='#ff6b6b' if cnt > 0 else '#4a9eff')

    def process_queue(self):
        if self.downloading or not self.queue:
            return
        if self.main_gui.nhentai_tab.is_downloading:
            self.status_label.config(text='主窗口下载中，已排队', fg='#e36209')
            self.win.after(500, self.process_queue)
            return
        self.downloading = True
        gid = self.queue.pop(0)
        self.current_gid = gid
        self.queue_listbox.delete(0)
        self.update_count()
        self.status_label.config(text=f'下载中: {gid}', fg='#4a9eff')
        self.main_gui.log(f'[胶囊] 开始下载: {gid}', 'header')
        config = {
            'proxy': self.main_gui.nh_proxy_var.get().strip() or None,
            'output_dir': self.main_gui.nh_output_var.get(),
            'quality': self.main_gui.nh_quality_var.get(),
            'max_rounds': self.main_gui.nh_retry_var.get(),
            'stealth_mode': self.main_gui.nh_stealth_var.get(),
            'workers': min(self.main_gui.nh_workers_var.get(), 4),
            'speed_mode': self.main_gui.nh_speed_mode_var.get(),
            'browser_priority': self.main_gui.nh_browser_priority_var.get(),
        }

        def do_download():
            try:
                crawler = NHentaiCrawler(
                    proxy=config['proxy'], output_dir=config['output_dir'],
                    quality=config['quality'], max_rounds=config['max_rounds'],
                    stealth_mode=config['stealth_mode'],
                    use_browser_fallback=True,
                    workers=config['workers'], speed_mode=config['speed_mode'],
                    browser_priority=config['browser_priority']
                )
                self._crawler = crawler

                def capsule_callback(evt, gid, data=None):
                    if evt == 'gallery_info':
                         self.main_gui.post(self.main_gui.log, f'[胶囊] [{gid}] 标题: {data["title"]}', 'info')
                    elif evt == 'complete':
                        m = data['missing']
                        if m == 0:
                            self.main_gui.post(self.main_gui.log, f'[胶囊] [{gid}] {TAG_OK}', 'success')
                        else:
                            self.main_gui.post(self.main_gui.log, f'[胶囊] [{gid}] {TAG_FAIL_PREFIX}{m}{TAG_FAIL_SUFFIX}', 'warning')
                        append_download_history(
                            'NHentai', gid, data.get('title', gid),
                            'complete' if m == 0 else 'partial', data.get('total', 0), m,
                            path=data.get('path', ''), error=f'仍缺失 {m} 页' if m else None)
                        self.main_gui.post(self.main_gui.refresh_collection)
                        self.main_gui.post(self.main_gui.refresh_history)
                    elif evt == 'error':
                        append_download_history('NHentai', gid, gid, 'failed', error=data)
                        self.main_gui.post(self.main_gui.log, f'[胶囊] [{gid}] {data}', 'error')
                        self.main_gui.post(self.main_gui.refresh_history)
                    elif evt == 'cancelled':
                        append_download_history(
                            'NHentai', gid, data.get('title', gid), 'cancelled',
                            data.get('total', 0), data.get('missing', 0),
                            path=data.get('path', ''), error='用户取消')
                        self.main_gui.post(self.main_gui.refresh_history)

                result = crawler.download_gallery(gid, callback=capsule_callback)
                self.main_gui.post(self.on_download_done, gid, result)
            except Exception as e:
                self.main_gui.post(self.main_gui.log, f'[胶囊] [{gid}] 异常: {translate_error(str(e))}', 'error')
                self.main_gui.post(self.on_download_done, gid, False)
            finally:
                if self._crawler:
                    self._crawler.close()

        threading.Thread(target=do_download, daemon=True).start()

    def on_download_done(self, gid, success):
        self._crawler = None
        self.downloading = False
        self.current_gid = ''
        self.processed_ids.add(gid)
        if success:
            self.status_label.config(text=f'完成: {gid}', fg='#22863a')
        else:
            self.status_label.config(text=f'失败: {gid}', fg='#ff6b6b')
        if self.queue:
            self.win.after(500, self.process_queue)
        else:
            self.main_gui.log('[胶囊] 队列已清空', 'info')
            self.win.after(3000, lambda: self.status_label.config(text='粘贴ID下载', fg='#aaaaaa'))

    def clear_queue(self, event=None):
        self.queue.clear()
        self.queue_listbox.delete(0, tk.END)
        self.update_count()
        self.status_label.config(text='已清空', fg='#aaaaaa')
        self.main_gui.log('[胶囊] 队列已清空', 'info')

    def open_main_window(self, event=None):
        self.main_gui.root.deiconify()
        self.main_gui.root.lift()

    def flush_clipboard(self):
        try:
            self.win.clipboard_clear()
        except tk.TclError:
            pass


# ==================== NHentai 面板 ====================
class NHentaiPanel(ttk.Frame):
    def __init__(self, parent, gui):
        super().__init__(parent)
        self.gui = gui
        self.crawler = None
        self.download_thread = None
        self.is_downloading = False
        self._stop_requested = False
        self.cover_visible = True
        self.setup_ui()

    def setup_ui(self):
        default_font = ('Microsoft YaHei UI', 9)
        style = ttk.Style()
        style.configure('Treeview', font=default_font, rowheight=24)
        style.configure('Treeview.Heading', font=('Microsoft YaHei UI', 9, 'bold'))

        # ===== 设置区 =====
        settings_frame = ttk.LabelFrame(self, text='下载设置', padding=8)
        settings_frame.pack(fill=tk.X, padx=5, pady=(5, 0))

        row1 = ttk.Frame(settings_frame)
        row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text='代理:').pack(side=tk.LEFT)
        self.gui.nh_proxy_var = tk.StringVar(value='http://127.0.0.1:7897')
        ttk.Entry(row1, textvariable=self.gui.nh_proxy_var, width=30).pack(side=tk.LEFT, padx=4)
        ttk.Label(row1, text='多个用逗号分隔；direct=直连备用',
                  foreground='#777777', font=('Microsoft YaHei UI', 8)).pack(side=tk.LEFT)
        ttk.Label(row1, text='输出目录:').pack(side=tk.LEFT, padx=(10, 0))
        self.gui.nh_output_var = tk.StringVar(value=str(Path.cwd() / 'downloads'))
        ttk.Entry(row1, textvariable=self.gui.nh_output_var, width=30).pack(side=tk.LEFT, padx=4)
        ttk.Button(row1, text='浏览', command=self.browse_output, width=6).pack(side=tk.LEFT)

        row2 = ttk.Frame(settings_frame)
        row2.pack(fill=tk.X, pady=2)
        ttk.Label(row2, text='质量:').pack(side=tk.LEFT)
        self.gui.nh_quality_var = tk.StringVar(value='high')
        ttk.Combobox(row2, textvariable=self.gui.nh_quality_var, values=['low', 'high'],
                     state='readonly', width=6).pack(side=tk.LEFT, padx=4)
        ttk.Label(row2, text='重试轮数:').pack(side=tk.LEFT, padx=(10, 0))
        self.gui.nh_retry_var = tk.IntVar(value=5)
        ttk.Spinbox(row2, from_=1, to=10, textvariable=self.gui.nh_retry_var, width=4).pack(side=tk.LEFT, padx=4)
        self.gui.nh_stealth_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2, text='隐身模式', variable=self.gui.nh_stealth_var).pack(side=tk.LEFT, padx=(10, 0))
        self.gui.nh_browser_priority_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2, text='浏览优先', variable=self.gui.nh_browser_priority_var,
                        command=self._toggle_browser_priority).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(row2, text='线程数:').pack(side=tk.LEFT, padx=(10, 0))
        self.gui.nh_workers_var = tk.IntVar(value=12)
        ttk.Spinbox(row2, from_=1, to=64, textvariable=self.gui.nh_workers_var, width=5).pack(side=tk.LEFT, padx=4)
        ttk.Label(row2, text='模式:').pack(side=tk.LEFT, padx=(10, 0))
        self.gui.nh_speed_mode_var = tk.StringVar(value='极速')
        ttk.Combobox(row2, textvariable=self.gui.nh_speed_mode_var, values=['保守', '极速', '狂暴'],
                     state='readonly', width=5).pack(side=tk.LEFT, padx=4)
        ttk.Button(row2, text='测速', command=self.test_proxy_speed, width=5).pack(side=tk.LEFT, padx=(10, 0))
        self.gui.cf_status_label = ttk.Label(row2, text='\u25cf', foreground='green', font=('Segoe UI', 12))
        self.gui.cf_status_label.pack(side=tk.LEFT, padx=(10, 0))
        self.gui.cf_text_label = ttk.Label(row2, text='CF:0', foreground='gray')
        self.gui.cf_text_label.pack(side=tk.LEFT)

        # ===== 反封禁状态栏 =====
        anti_frame = ttk.Frame(settings_frame)
        anti_frame.pack(fill=tk.X, pady=(3, 0))

        self.anti_stealth_lbl = ttk.Label(anti_frame, text='\u25cf 隐身',
                                          font=('Microsoft YaHei UI', 8), foreground='#22863a')
        self.anti_stealth_lbl.pack(side=tk.LEFT, padx=(0, 10))
        self.anti_ua_lbl = ttk.Label(anti_frame, text='\u25cf UA轮换',
                                     font=('Microsoft YaHei UI', 8), foreground='#22863a')
        self.anti_ua_lbl.pack(side=tk.LEFT, padx=(0, 10))
        self.anti_delay_lbl = ttk.Label(anti_frame, text='\u25cf 延迟',
                                        font=('Microsoft YaHei UI', 8), foreground='#22863a')
        self.anti_delay_lbl.pack(side=tk.LEFT, padx=(0, 10))
        self.anti_proxy_lbl = ttk.Label(anti_frame, text='\u25cb 代理',
                                        font=('Microsoft YaHei UI', 8), foreground='gray')
        self.anti_proxy_lbl.pack(side=tk.LEFT, padx=(0, 10))
        self.anti_tls_lbl = ttk.Label(anti_frame, text='\u25cf TLS伪装',
                                      font=('Microsoft YaHei UI', 8), foreground='#22863a')
        self.anti_tls_lbl.pack(side=tk.LEFT, padx=(0, 10))
        self.anti_req_lbl = ttk.Label(anti_frame, text='请求: 0',
                                      font=('Microsoft YaHei UI', 8), foreground='gray')
        self.anti_req_lbl.pack(side=tk.RIGHT)
        self.anti_block_lbl = ttk.Label(anti_frame, text='\u25cf 反爬: 就绪',
                                        font=('Microsoft YaHei UI', 8), foreground='#22863a')
        self.anti_block_lbl.pack(side=tk.RIGHT, padx=(0, 10))

        # ===== 主体分栏 =====
        main_pane = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main_pane.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 左侧
        left_frame = ttk.Frame(main_pane)
        main_pane.add(left_frame, weight=4)

        left_split = ttk.PanedWindow(left_frame, orient=tk.VERTICAL)
        left_split.pack(fill=tk.BOTH, expand=True)

        # 画廊封面预览
        cover_outer = ttk.Frame(left_split)
        left_split.add(cover_outer, weight=6)

        cover_header = ttk.Frame(cover_outer)
        cover_header.pack(fill=tk.X)
        ttk.Label(cover_header, text='画廊封面预览',
                  font=('Microsoft YaHei UI', 9, 'bold')).pack(side=tk.LEFT, padx=4, pady=2)
        self.cover_toggle_btn = ttk.Button(cover_header, text='隐藏封面',
                                           command=self.toggle_covers, width=8)
        self.cover_toggle_btn.pack(side=tk.RIGHT, padx=4, pady=2)

        self.cover_gallery = self._create_cover_gallery(cover_outer)
        self.cover_gallery.pack(fill=tk.BOTH, expand=True)

        # 文件列表
        file_frame = ttk.LabelFrame(left_split, text='画廊文件', padding=4)
        left_split.add(file_frame, weight=4)
        columns = ('name', 'size')
        self.file_tree = ttk.Treeview(file_frame, columns=columns, show='headings', height=10)
        self.file_tree.heading('name', text='文件名')
        self.file_tree.heading('size', text='大小')
        self.file_tree.column('name', width=260, minwidth=160)
        self.file_tree.column('size', width=80, minwidth=60, anchor=tk.E)
        tree_scroll = ttk.Scrollbar(file_frame, orient=tk.VERTICAL, command=self.file_tree.yview)
        self.file_tree.configure(yscrollcommand=tree_scroll.set)
        self.file_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # 右侧
        right_frame = ttk.Frame(main_pane)
        main_pane.add(right_frame, weight=6)

        right_split = ttk.PanedWindow(right_frame, orient=tk.VERTICAL)
        right_split.pack(fill=tk.BOTH, expand=True)

        # ID输入区
        input_frame = ttk.LabelFrame(right_split, text='画廊ID（每行一个，支持数字ID或URL）', padding=6)
        right_split.add(input_frame, weight=0)
        self.input_text = scrolledtext.ScrolledText(input_frame, height=4, wrap=tk.WORD)
        self.input_text.pack(fill=tk.X)

        btn_row = ttk.Frame(input_frame)
        btn_row.pack(fill=tk.X, pady=(4, 0))
        self.start_btn = ttk.Button(btn_row, text='开始下载', command=self.start_download,
                                    style='Accent.TButton')
        self.start_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.stop_btn = ttk.Button(btn_row, text='停止', command=self.stop_download, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.view_btn = ttk.Button(btn_row, text='查看信息', command=self.view_gallery_info)
        self.view_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_row, text='打开下载目录', command=self.open_output).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_row, text='清空日志', command=self.gui.clear_log).pack(side=tk.LEFT, padx=(0, 6))
        self.gui.capsule_btn = ttk.Button(btn_row, text='开启胶囊', command=self.gui.toggle_capsule)
        self.gui.capsule_btn.pack(side=tk.RIGHT)

        # 进度区
        transfer_frame = ttk.LabelFrame(right_split, text='文件传输', padding=8)
        right_split.add(transfer_frame, weight=0)

        self.transfer_title = ttk.Label(transfer_frame, text='等待开始...', font=('Microsoft YaHei', 9, 'bold'))
        self.transfer_title.pack(anchor=tk.W)
        self.transfer_detail = ttk.Label(transfer_frame, text='', foreground='#555555')
        self.transfer_detail.pack(anchor=tk.W)

        prog_row = ttk.Frame(transfer_frame)
        prog_row.pack(fill=tk.X, pady=(6, 0))
        self.file_progress = ttk.Progressbar(prog_row, mode='determinate', length=300)
        self.file_progress.pack(fill=tk.X, side=tk.LEFT, expand=True)
        self.file_percent = ttk.Label(prog_row, text='0%', width=6, anchor=tk.E)
        self.file_percent.pack(side=tk.RIGHT, padx=(6, 0))

        spd_row = ttk.Frame(transfer_frame)
        spd_row.pack(fill=tk.X, pady=(4, 0))
        self.speed_label = ttk.Label(spd_row, text='', foreground='#555555')
        self.speed_label.pack(side=tk.LEFT)
        self.time_label = ttk.Label(spd_row, text='', foreground='#555555')
        self.time_label.pack(side=tk.RIGHT)

        # 日志区
        log_frame = ttk.LabelFrame(right_split, text='下载日志', padding=4)
        right_split.add(log_frame, weight=2)
        self.log_text = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, state=tk.DISABLED,
                                                  font=('Consolas', 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.init_log_tags()

    def _create_cover_gallery(self, parent):
        canvas = tk.Canvas(parent, highlightthickness=0, bg='#f0f0f0')
        scrollbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack_forget()
        inner = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=inner, anchor='nw')
        inner.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        for w in (canvas, inner):
            w.bind('<Enter>', lambda e: w.bind_all('<MouseWheel>', lambda ev: canvas.yview_scroll(int(-1 * (ev.delta / 120)), 'units')))
            w.bind('<Leave>', lambda e: canvas.unbind_all('<MouseWheel>'))

        class CoverGalleryWrapper(ttk.Frame):
            def __init__(self, master, canvas_w, scrollbar_w, inner_w):
                super().__init__(master)
                self.canvas = canvas_w
                self.inner = inner_w
                self.scrollbar = scrollbar_w
                self._packed = True

            def pack(self, **kw):
                if not kw.get('pack_forget_called', False):
                    self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
                    self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
                super().pack(**kw)

            def pack_forget(self):
                self.canvas.pack_forget()
                self.scrollbar.pack_forget()
                super().pack_forget()

        return CoverGalleryWrapper(parent, canvas, scrollbar, inner)

    def init_log_tags(self):
        self.log_text.tag_configure('info', foreground='#333333')
        self.log_text.tag_configure('success', foreground='#22863a', font=('Consolas', 9, 'bold'))
        self.log_text.tag_configure('error', foreground='#d73a49')
        self.log_text.tag_configure('warning', foreground='#e36209')
        self.log_text.tag_configure('header', foreground='#0366d6', font=('Consolas', 9, 'bold'))
        self.log_text.tag_configure('thread', foreground='#6f42c1')

    def toggle_covers(self):
        if self.cover_visible:
            self.cover_gallery.pack_forget()
            self.cover_toggle_btn.config(text='显示封面')
        else:
            self.cover_gallery.pack(fill=tk.BOTH, expand=True)
            self.cover_toggle_btn.config(text='隐藏封面')
            self.refresh_covers()
        self.cover_visible = not self.cover_visible

    def refresh_covers(self):
        output_path = self.gui.nh_output_var.get()
        for widget in self.cover_gallery.inner.winfo_children():
            widget.destroy()
        self.cover_gallery._photos = []

        output_path = Path(output_path)
        if not output_path.exists():
            ttk.Label(self.cover_gallery.inner, text='输出目录不存在', foreground='gray').pack()
            return

        galleries = sorted([d for d in output_path.iterdir() if d.is_dir()], key=lambda d: d.name)
        if not galleries:
            ttk.Label(self.cover_gallery.inner, text='暂无已下载的画廊', foreground='gray').pack()
            return

        valid = []
        for gd in galleries:
            for ext in ('*.webp', '*.jpg', '*.jpeg', '*.png'):
                files = sorted(gd.glob(ext))
                if files:
                    valid.append((gd, files[0]))
                    break

        if not valid:
            ttk.Label(self.cover_gallery.inner, text='画廊目录中没有图片文件', foreground='gray').pack()
            return

        cols = 4
        for idx, (gd, first_img) in enumerate(valid):
            row, col = idx // cols, idx % cols
            try:
                with Image.open(first_img) as img:
                    ratio = min(120 / img.width, 170 / img.height)
                    nw, nh = int(img.width * ratio), int(img.height * ratio)
                    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
                    photo = ImageTk.PhotoImage(img)
                    self.cover_gallery._photos = getattr(self.cover_gallery, '_photos', [])
                    self.cover_gallery._photos.append(photo)
            except Exception:
                photo = None

            item = ttk.Frame(self.cover_gallery.inner)
            item.grid(row=row, column=col, padx=3, pady=3, sticky='n')
            if photo:
                ttk.Label(item, image=photo).pack()

            dn = strip_status_tag(gd.name)
            if len(dn) > 22:
                dn = dn[:20] + '...'
            is_complete = TAG_OK in gd.name
            ttk.Label(item, text=dn, font=('Microsoft YaHei UI', 8),
                      foreground='#22863a' if is_complete else '#e36209', wraplength=120).pack()

        for c in range(cols):
            self.cover_gallery.inner.columnconfigure(c, weight=1)

    def start_download(self):
        if self.is_downloading:
            self.gui.log('已有下载任务进行中', 'warning')
            return

        input_text = self.input_text.get(1.0, tk.END).strip()
        if not input_text:
            messagebox.showwarning('提示', '请输入画廊ID')
            return

        gallery_ids = []
        for line in input_text.split('\n'):
            line = line.strip()
            if not line:
                continue
            if line.isdigit():
                gallery_ids.append(line)
            else:
                match = re.search(r'/g/(\d+)', line)
                if match:
                    gallery_ids.append(match.group(1))
                else:
                    self.gui.log(f'无法识别的输入: {line}', 'warning')

        if not gallery_ids:
            messagebox.showwarning('提示', '未找到有效的画廊ID')
            return

        unique_ids = list(dict.fromkeys(gallery_ids))
        Path(self.gui.nh_output_var.get()).mkdir(parents=True, exist_ok=True)

        output_dir = self.gui.nh_output_var.get()
        self.crawler = NHentaiCrawler(
            proxy=self.gui.nh_proxy_var.get().strip() or None,
            output_dir=output_dir,
            quality=self.gui.nh_quality_var.get(),
            max_rounds=self.gui.nh_retry_var.get(),
            stealth_mode=self.gui.nh_stealth_var.get(),
            use_browser_fallback=True,
            workers=self.gui.nh_workers_var.get(),
            speed_mode=self.gui.nh_speed_mode_var.get(),
            browser_priority=self.gui.nh_browser_priority_var.get()
        )

        self.is_downloading = True
        self._stop_requested = False
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.file_tree.delete(*self.file_tree.get_children())
        self._nh_req_count = 0
        self._update_anti_status()
        self.gui.log(f'[NHentai] 开始批量处理 {len(unique_ids)} 个画廊', 'header')

        self.download_thread = threading.Thread(
            target=self.download_worker, args=(unique_ids, output_dir, self.crawler), daemon=True)
        self.download_thread.start()

    def download_worker(self, gallery_ids, output_dir, crawler):
        total = len(gallery_ids)
        success_count, fail_count = 0, 0

        for idx, gid in enumerate(gallery_ids):
            if self._stop_requested or crawler._stop_event.is_set():
                break

            self.gui.post(lambda i=idx, t=total: self.transfer_title.config(
                text=f'[{i + 1}/{t}] 准备中...'))
            self.gui.post(lambda: self.transfer_detail.config(text=''))

            def make_callback(idx, gid):
                def callback(evt, gid_inner=None, data=None):
                    g = gid_inner or gid
                    if evt == 'gallery_info':
                        self.gui.post(self.gui.log, f'[{g}] 标题: {data["title"]}', 'info')
                        self.gui.post(self.gui.log, f'[{g}] 共 {data["num_pages"]} 页', 'info')
                        self.gui.post(lambda: self.transfer_title.config(
                            text=f'[{idx + 1}/{total}] {data["title"][:30]}'))
                        self.gui.post(lambda: self.transfer_detail.config(
                            text=f'共 {data["num_pages"]} 页'))
                    elif evt == 'start':
                        self.gui.post(self.gui.log,
                                            f'[{g}] 开始下载，已有 {data["already"]} 页，需补 {data["missing"]} 页', 'info')
                    elif evt == 'file_progress':
                        d = data['downloaded']
                        t = data['total']
                        pct = (d / t * 100) if t > 0 else 0
                        self.gui.post(lambda p=pct: self.file_progress.configure(value=p))
                        self.gui.post(lambda p=pct: self.file_percent.config(text=f'{p:.0f}%'))
                        size_str = f'{format_size(d)}'
                        if t > 0:
                            size_str += f' / {format_size(t)}'
                        self.gui.post(lambda: self.speed_label.config(
                            text=f'{size_str} | {format_speed(data["speed"])}'))
                        self.gui.post(lambda: self.time_label.config(
                            text=f'剩余: {format_time(data["remaining"])}'))
                    elif evt == 'thread_log':
                        if data.get('success'):
                            if 'size' in data:
                                with crawler.bytes_lock:
                                    crawler.total_downloaded_bytes += data['size']
                            self.gui.post(self.gui.log, f'[{g}] 第{data["page"]}页 成功', 'thread')
                        else:
                            self.gui.post(self.gui.log,
                                                f'[{g}] 第{data["page"]}页 失败: {data.get("error", "")}', 'error')
                    elif evt == 'retry':
                        self.gui.post(self.gui.log,
                                            f'[{g}] 第 {data["round"]}/{data["max_rounds"]} 轮重试，剩余 {data["remaining"]} 页',
                                            'warning')
                    elif evt == 'error':
                        self.gui.post(self.gui.log, f'[{g}] {data}', 'error')
                        append_download_history('NHentai', g, g, 'failed', error=data)
                        self.gui.post(self.gui.refresh_history)
                    elif evt == 'complete':
                        d = data['downloaded']
                        t = data['total']
                        m = data['missing']
                        skipped = data.get('skipped', False)
                        if skipped:
                            self.gui.post(self.gui.log, f'[{g}] 已完整，跳过 ({d}/{t}) {TAG_OK}', 'success')
                        elif m == 0:
                            self.gui.post(self.gui.log, f'[{g}] 全部完成 ({d}/{t}) {TAG_OK}', 'success')
                        else:
                            self.gui.post(self.gui.log,
                                                f'[{g}] 完成 ({d}/{t}) {TAG_FAIL_PREFIX}{m}{TAG_FAIL_SUFFIX}', 'warning')
                        total_bytes = data.get('total_bytes', 0)
                        if total_bytes > 0:
                            self.gui.post(self.gui.log, f'[{g}] 本次下载: {format_size(total_bytes)}', 'info')
                        self.gui.post(self.gui.log, f'[{g}] 目录: {data["dir_name"]}', 'info')
                        update_download_index(output_dir, g,
                                             title=data.get('title', g),
                                             path=str(Path(output_dir) / data["dir_name"]),
                                             status='complete' if m == 0 else 'partial',
                                             missing=m,
                                              total=t)
                        append_download_history(
                            'NHentai', g, data.get('title', g),
                            'complete' if m == 0 else 'partial', total=t, missing=m,
                            path=data.get('path') or str(Path(output_dir) / data["dir_name"]),
                            error=f'仍缺失 {m} 页' if m else None)
                        self.gui.post(lambda f=data.get('files', []): self.update_file_tree(f))
                        self.gui.post(self.refresh_covers)
                        self.gui.post(self.gui.refresh_collection)
                        self.gui.post(self.gui.refresh_history)
                    elif evt == 'cancelled':
                        append_download_history(
                            'NHentai', g, data.get('title', g), 'cancelled',
                            data.get('total', 0), data.get('missing', 0),
                            path=data.get('path', ''), error='用户取消')
                        self.gui.post(self.gui.log,
                                            f'[{g}] 已停止，保留 {data.get("downloaded", 0)}/{data.get("total", 0)} 页',
                                            'warning')
                        self.gui.post(self.gui.refresh_history)
                return callback

            try:
                result = crawler.download_gallery(gid, callback=make_callback(idx, gid))
                if result:
                    success_count += 1
                elif not self._stop_requested:
                    fail_count += 1
            except Exception as e:
                fail_count += 1
                append_download_history('NHentai', gid, gid, 'failed', error=e)
                self.gui.post(self.gui.log, f'[异常] [{gid}] {translate_error(str(e))}', 'error')
                self.gui.post(self.gui.refresh_history)

        crawler.close()
        self.gui.post(self.download_finished, total, success_count, fail_count,
                      self._stop_requested)

    def download_finished(self, total, success, fail, stopped=False):
        self.is_downloading = False
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.gui.log('=' * 50, 'header')
        if stopped:
            self.gui.log(f'已停止，已完成: {success}   未处理: {total - success - fail}   失败: {fail}', 'warning')
        else:
            self.gui.log(f'总计: {total}   完整: {success} {TAG_OK}   失败: {fail}',
                         'success' if fail == 0 else 'warning')
        if self.crawler and self.crawler.errors:
            self.gui.log(f'共 {len(self.crawler.errors)} 个错误', 'error')
        self.transfer_title.config(text=('已停止' if stopped else '处理完成') + f'  完整:{success}  失败:{fail}')
        self.transfer_detail.config(text='')
        self.speed_label.config(text='')
        self.time_label.config(text='')
        if not stopped:
            self.file_progress['value'] = 100
            self.file_percent.config(text='100%')
        self.refresh_covers()
        self.gui.refresh_collection()

    def update_file_tree(self, files):
        for item in self.file_tree.get_children():
            self.file_tree.delete(item)
        for f in files:
            self.file_tree.insert('', tk.END, values=(f['name'], format_size(f['size'])))

    def stop_download(self):
        if self.crawler:
            self.crawler.stop()
        self._stop_requested = True
        self.gui.log('正在停止...', 'warning')
        self.stop_btn.config(state=tk.DISABLED)

    def _update_anti_status(self):
        proxy = self.gui.nh_proxy_var.get().strip()
        speed = self.gui.nh_speed_mode_var.get()

        self.anti_stealth_lbl.config(
            foreground='#22863a' if self.gui.nh_stealth_var.get() else 'gray',
            text='\u25cf 隐身' if self.gui.nh_stealth_var.get() else '\u25cb 隐身')

        self.anti_proxy_lbl.config(
            foreground='#22863a' if proxy else 'gray',
            text='\u25cf 代理' if proxy else '\u25cb 代理')

        self.anti_tls_lbl.config(
            foreground='#22863a' if SCRAPLING_AVAILABLE else 'gray',
            text='\u25cf TLS' if SCRAPLING_AVAILABLE else '\u25cb TLS')

        delay_text = '0.01-0.05s' if speed == '极速' else ('0.3-1s' if speed == '保守' else '0s')
        self.anti_delay_lbl.config(text=f'\u25cf 延迟:{delay_text}',
                                   foreground='#22863a' if speed != '狂暴' else '#e36209')

        self.anti_ua_lbl.config(
            foreground='#22863a',
            text='\u25cf UA池:7')

        snapshot = self.crawler.scheduler.snapshot() if self.crawler else None
        if snapshot:
            mode_text = '浏览优先' if snapshot['browser_priority'] else '自适应'
            cooldown = int(snapshot['site_cooldown'] + 0.999)
            cooldown_text = f'  冷却:{cooldown}s' if cooldown else ''
            self.anti_req_lbl.config(
                text=f'{mode_text}  请求:{snapshot["requests"]}  并发:{snapshot["active"]}/{snapshot["limit"]}  熔断:{snapshot["open_routes"]}{cooldown_text}')
        else:
            self.anti_req_lbl.config(text='自适应  请求:0  并发:0')

        if self.crawler and hasattr(self.crawler, 'anti_crawl'):
            ac = self.crawler.anti_crawl
            status_text, color = ac.get_status_text()
            defense = ac.get_defense_info()
            self.anti_block_lbl.config(
                text=f'\u25cf {status_text}  [{defense}]', foreground=color)

        if self.is_downloading:
            self.gui.root.after(1000, self._update_anti_status)

    def _update_anti_status_safe(self):
        try:
            self._update_anti_status()
        except Exception:
            pass

    def _toggle_browser_priority(self):
        if self.crawler:
            self.crawler.scheduler.configure(
                browser_priority=self.gui.nh_browser_priority_var.get())
        self._update_anti_status_safe()

    def browse_output(self):
        path = filedialog.askdirectory()
        if path:
            self.gui.nh_output_var.set(path)
            self.refresh_covers()

    def open_output(self):
        path = self.gui.nh_output_var.get()
        if os.path.exists(path):
            os.startfile(path)
        else:
            messagebox.showinfo('提示', '目录不存在')

    def test_proxy_speed(self):
        proxy = self.gui.nh_proxy_var.get().strip() or None
        if not proxy:
            messagebox.showwarning('提示', '请填写代理地址')
            return
        test_crawler = NHentaiCrawler(proxy=proxy, output_dir=self.gui.nh_output_var.get(),
                                      stealth_mode=False, use_browser_fallback=False,
                                      workers=1, speed_mode='极速')
        success, result = test_crawler.test_proxy_speed()
        if success:
            self.gui.log(f'[测速] 延迟: {result:.0f}ms', 'success')
            messagebox.showinfo('测速结果', f'延迟: {result:.0f}ms')
        else:
            self.gui.log(f'[测速] 失败: {result}', 'error')
            messagebox.showerror('测速失败', f'代理不可用\n{result}')

    def view_gallery_info(self):
        input_text = self.input_text.get(1.0, tk.END).strip()
        if not input_text:
            messagebox.showwarning('提示', '请输入画廊ID')
            return

        gallery_ids = []
        for line in input_text.split('\n'):
            line = line.strip()
            if not line:
                continue
            if line.isdigit():
                gallery_ids.append(line)
            else:
                match = re.search(r'/g/(\d+)', line)
                if match:
                    gallery_ids.append(match.group(1))

        if not gallery_ids:
            messagebox.showwarning('提示', '未找到有效的画廊ID')
            return

        unique_ids = list(dict.fromkeys(gallery_ids))
        self.gui.log(f'[信息] 查询 {len(unique_ids)} 个画廊...', 'header')

        def fetch_info():
            crawler = NHentaiCrawler(
                proxy=self.gui.nh_proxy_var.get().strip() or None,
                output_dir=self.gui.nh_output_var.get(),
                stealth_mode=self.gui.nh_stealth_var.get(),
                use_browser_fallback=True,
                workers=1, speed_mode='保守'
            )

            for gid in unique_ids:
                info, error = crawler.get_gallery_info_enhanced(gid)
                if error:
                    self.gui.post(self.gui.log, f'[{gid}] 查询失败: {error}', 'error')
                    continue
                self.gui.post(self._log_info_detail, gid, info)

        threading.Thread(target=fetch_info, daemon=True).start()

    def _log_info_detail(self, gallery_id, info):
        log = self.gui.log
        sep = '━' * 50

        log(f'\n{sep}', 'header')
        log(f'  画廊 #{gallery_id}', 'header')
        log(f'{sep}', 'header')

        title_en = info.get('title', '---')
        title_jp = info.get('title_jp', '')
        log(f'  英文标题: {title_en}', 'info')
        if title_jp:
            log(f'  日文标题: {title_jp}', 'info')

        full_title = info.get('full_title', '')
        if full_title and full_title not in (title_en, title_jp):
            log(f'  完整标题: {full_title}', 'thread')

        log(f'  URL: https://nhentai.net/g/{gallery_id}/', 'thread')

        cover = info.get('cover_url', '')
        if cover:
            log(f'  封面: {cover}', 'thread')

        log('  ── 基本 ──', 'header')
        log(f'  Media ID : {info.get("media_id", "?")}', 'info')
        log(f'  页数     : {info.get("num_pages", "?")}', 'info')
        log(f'  图片格式 : {info.get("ext", "?")}', 'info')
        fav = info.get('favorites', 0)
        log(f'  收藏数   : {fav:,}', 'info' if fav > 0 else 'warning')
        upload = info.get('upload_date', '')
        log(f'  上传时间 : {upload if upload else "未知"}', 'info')

        for cat_key, cat_label, cat_color in [
            ('languages', '语言', 'success'),
            ('categories', '分类', 'thread'),
            ('parodies', '作品来源', 'warning'),
            ('artists', '艺术家', 'warning'),
            ('groups', '团体', 'warning'),
            ('tags', '标签', 'info'),
        ]:
            items = info.get(cat_key, [])
            if items:
                log(f'  ── {cat_label} ({len(items)}) ──', 'header')
                log(f'  {", ".join(items)}', cat_color)

        log(f'{sep}\n', 'header')


# ==================== JM Comic 面板 ====================
class JMComicPanel(ttk.Frame):
    def __init__(self, parent, gui):
        super().__init__(parent)
        self.gui = gui
        self.running = False
        self._jm_stop_event = threading.Event()
        self._jm_thread = None
        self._jm_domains = None
        self._jm_domain_discovered = False
        self.scheduler = AdaptiveScheduler(
            max_concurrency=8, min_concurrency=1,
            failure_threshold=3, cooldown=25,
            recovery_successes=10, proxies=['http://127.0.0.1:7897'])
        self.setup_ui()
        self.gui.log('[JM Comic] 面板已初始化', 'info')

    def setup_ui(self):
        main_frame = ttk.Frame(self, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 配置区
        config_frame = ttk.LabelFrame(main_frame, text='JM Comic 配置', padding=8)
        config_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(config_frame, text='下载目录:').grid(row=0, column=0, sticky=tk.W, padx=(0, 5))
        self.jm_dir_var = tk.StringVar(value=str(Path.cwd() / 'downloads' / 'JMComic'))
        ttk.Entry(config_frame, textvariable=self.jm_dir_var, width=40).grid(row=0, column=1, sticky=tk.EW)
        ttk.Button(config_frame, text='浏览', command=self._browse_dir, width=6).grid(row=0, column=2, padx=(5, 0))

        ttk.Label(config_frame, text='代理:').grid(row=1, column=0, sticky=tk.W, pady=(8, 0))
        self.jm_proxy_var = tk.StringVar(value='http://127.0.0.1:7897')
        proxy_entry = ttk.Entry(config_frame, textvariable=self.jm_proxy_var, width=30)
        proxy_entry.grid(row=1, column=1, sticky=tk.W, pady=(8, 0))
        ttk.Label(config_frame, text='多个用逗号分隔；direct=直连备用',
                  foreground='gray', font=('Microsoft YaHei UI', 8)).grid(row=1, column=2, sticky=tk.W, pady=(8, 0))

        ttk.Label(config_frame, text='模式:').grid(row=2, column=0, sticky=tk.W, pady=(8, 0))
        mode_frame = ttk.Frame(config_frame)
        mode_frame.grid(row=2, column=1, columnspan=2, sticky=tk.W, pady=(8, 0))
        self.jm_mode_var = tk.StringVar(value='api')
        ttk.Radiobutton(mode_frame, text='API (免登录)', variable=self.jm_mode_var,
                        value='api').pack(side=tk.LEFT, padx=(0, 15))
        ttk.Radiobutton(mode_frame, text='HTML (需登录)', variable=self.jm_mode_var,
                        value='html').pack(side=tk.LEFT, padx=(0, 15))
        ttk.Label(mode_frame, text='仅保留可用模式', foreground='gray').pack(side=tk.LEFT)

        # 登录信息
        login_frame = ttk.LabelFrame(config_frame, text='登录信息 (HTML模式需要)', padding=5)
        login_frame.grid(row=3, column=0, columnspan=3, sticky=tk.EW, pady=(8, 0))
        login_frame.columnconfigure(2, weight=1)

        ttk.Label(login_frame, text='用户名:').grid(row=0, column=0, sticky=tk.W, padx=(0, 5))
        self.jm_username_var = tk.StringVar()
        ttk.Entry(login_frame, textvariable=self.jm_username_var, width=20).grid(row=0, column=1, sticky=tk.W,
                                                                                   padx=(0, 10))
        ttk.Label(login_frame, text='密码:').grid(row=0, column=2, sticky=tk.W, padx=(0, 5))
        self.jm_password_var = tk.StringVar()
        ttk.Entry(login_frame, textvariable=self.jm_password_var, width=20, show='*').grid(row=0, column=3, sticky=tk.W)

        # 线程与反封禁配置
        thread_frame = ttk.Frame(config_frame)
        thread_frame.grid(row=4, column=0, columnspan=3, sticky=tk.EW, pady=(8, 0))
        ttk.Label(thread_frame, text='图片线程:').pack(side=tk.LEFT)
        self.jm_image_threads_var = tk.IntVar(value=8)
        ttk.Spinbox(thread_frame, from_=1, to=64, textvariable=self.jm_image_threads_var,
                    width=5).pack(side=tk.LEFT, padx=4)
        ttk.Label(thread_frame, text='章节线程:').pack(side=tk.LEFT, padx=(10, 0))
        self.jm_photo_threads_var = tk.IntVar(value=4)
        ttk.Spinbox(thread_frame, from_=1, to=16, textvariable=self.jm_photo_threads_var,
                    width=5).pack(side=tk.LEFT, padx=4)
        ttk.Label(thread_frame, text='重试:').pack(side=tk.LEFT, padx=(10, 0))
        self.jm_retry_var = tk.IntVar(value=8)
        ttk.Spinbox(thread_frame, from_=1, to=20, textvariable=self.jm_retry_var,
                    width=4).pack(side=tk.LEFT, padx=4)
        ttk.Label(thread_frame, text='延迟(秒):').pack(side=tk.LEFT, padx=(10, 0))
        self.jm_delay_var = tk.DoubleVar(value=0.5)
        ttk.Spinbox(thread_frame, from_=0, to=10, textvariable=self.jm_delay_var,
                    increment=0.5, width=4).pack(side=tk.LEFT, padx=4)

        ttk.Label(thread_frame, text='反封:').pack(side=tk.LEFT, padx=(10, 0))
        self.jm_antiblock_var = tk.StringVar(value='中等')
        ttk.Combobox(thread_frame, textvariable=self.jm_antiblock_var,
                     values=['保守', '中等', '激进'], state='readonly', width=4).pack(side=tk.LEFT, padx=4)
        self.jm_browser_priority_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(thread_frame, text='浏览优先', variable=self.jm_browser_priority_var,
                        command=self._toggle_jm_browser_priority).pack(side=tk.LEFT, padx=(8, 0))

        # ID输入区
        input_frame = ttk.LabelFrame(main_frame, text='JM Comic 下载任务', padding=8)
        input_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(input_frame, text='本子ID:').grid(row=0, column=0, sticky=tk.NW, padx=(0, 5))
        self.jm_id_text = tk.Text(input_frame, height=5, width=30)
        self.jm_id_text.grid(row=0, column=1, sticky=tk.EW)
        ttk.Label(input_frame,
                  text='一行一个ID\n章节ID加 p 前缀\n如: JM123456 或 p123456',
                  foreground='gray').grid(row=0, column=2, sticky=tk.NW, padx=(5, 0))
        input_frame.columnconfigure(1, weight=1)

        self.jm_existing_label = ttk.Label(input_frame, text='',
                                           font=('Microsoft YaHei UI', 8),
                                           foreground='#22863a')
        self.jm_existing_label.grid(row=1, column=1, sticky=tk.W, pady=(2, 0))
        self.jm_id_text.bind('<KeyRelease>', lambda e: self.gui.root.after(300, self._check_existing))

        # 按钮区
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=(0, 8))

        self.jm_download_btn = ttk.Button(btn_frame, text='开始下载', command=self._start_download,
                                          width=12, style='Accent.TButton')
        self.jm_download_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.jm_view_btn = ttk.Button(btn_frame, text='查看本子信息', command=self._view_album, width=12)
        self.jm_view_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.jm_stop_btn = ttk.Button(btn_frame, text='停止', command=self._stop, width=8, state=tk.DISABLED)
        self.jm_stop_btn.pack(side=tk.LEFT)

        self.jm_domain_btn = ttk.Button(btn_frame, text='发现域名', command=self._discover_domains, width=8)
        self.jm_domain_btn.pack(side=tk.RIGHT, padx=(4, 0))
        self.jm_domain_label = ttk.Label(btn_frame, text='', foreground='gray',
                                         font=('Microsoft YaHei UI', 8))
        self.jm_domain_label.pack(side=tk.RIGHT, padx=4)
        ttk.Button(btn_frame, text='打开目录', command=self._open_dir, width=6).pack(side=tk.RIGHT)

        # 反封禁状态栏
        jm_anti = ttk.Frame(main_frame)
        jm_anti.pack(fill=tk.X, pady=(4, 0))
        self.jm_anti_tls = ttk.Label(jm_anti, text='\u25cf TLS: Chrome',
                                     font=('Microsoft YaHei UI', 8), foreground='#22863a')
        self.jm_anti_tls.pack(side=tk.LEFT, padx=(0, 10))
        self.jm_anti_domain = ttk.Label(jm_anti, text='\u25cf 域名池: 内置',
                                        font=('Microsoft YaHei UI', 8), foreground='#22863a')
        self.jm_anti_domain.pack(side=tk.LEFT, padx=(0, 10))
        self.jm_anti_delay = ttk.Label(jm_anti, text='\u25cf 延迟',
                                       font=('Microsoft YaHei UI', 8), foreground='#22863a')
        self.jm_anti_delay.pack(side=tk.LEFT, padx=(0, 10))
        self.jm_anti_proxy = ttk.Label(jm_anti, text='\u25cb 代理',
                                       font=('Microsoft YaHei UI', 8), foreground='gray')
        self.jm_anti_proxy.pack(side=tk.LEFT, padx=(0, 10))
        self.jm_anti_retry = ttk.Label(jm_anti, text='\u25cf 重试',
                                       font=('Microsoft YaHei UI', 8), foreground='#22863a')
        self.jm_anti_retry.pack(side=tk.LEFT, padx=(0, 10))
        self.jm_anti_req = ttk.Label(jm_anti, text='请求: 0',
                                     font=('Microsoft YaHei UI', 8), foreground='gray')
        self.jm_anti_req.pack(side=tk.RIGHT)

        self._jm_req_count = 0
        self._update_jm_anti_status()

        # 日志区
        log_frame = ttk.LabelFrame(main_frame, text='JM Comic 日志', padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.jm_log_area = scrolledtext.ScrolledText(log_frame, height=10, state=tk.DISABLED, wrap=tk.WORD,
                                                     font=('Consolas', 9))
        self.jm_log_area.pack(fill=tk.BOTH, expand=True)

        if not JM_AVAILABLE:
            self._jm_log('[警告] jmcomic 模块未安装或不可用', 'error')
            self._jm_log('请确保 jmcomic 文件夹已复制到当前目录', 'warning')
        else:
            self._jm_log('[JM Comic] 模块已加载，可以开始下载', 'success')

    def _browse_dir(self):
        path = filedialog.askdirectory(initialdir=self.jm_dir_var.get())
        if path:
            self.jm_dir_var.set(path)
            self._jm_log(f'下载目录: {path}')

    def _discover_domains(self):
        self.jm_domain_btn.config(state=tk.DISABLED)
        self.jm_domain_label.config(text='发现中...', foreground='#e36209')
        self._jm_log('[域名] 开始从发布页发现JM Comic可用域名...', 'header')

        def run():
            import requests as req
            proxy_str = self.jm_proxy_var.get().strip()
            proxies = {'http': proxy_str, 'https': proxy_str} if proxy_str else None
            hd = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            }
            found_domains = set()

            # Round 1: Scrape publish/portal pages
            publish_sites = [
                'https://jmcomicog.net/',
                'https://jmcomicgo.org/',
                'https://jm365.work/3YeBdF',
                'https://18comic.vip/',
            ]
            for site in publish_sites:
                try:
                    r = req.get(site, headers=hd, proxies=proxies, timeout=15,
                                allow_redirects=True)
                    self.gui.post(self._jm_log,
                                       f'[域名] {site} → {r.url[:60]} (HTTP {r.status_code})', 'info')

                    urls = re.findall(r'https?://[a-zA-Z0-9][-a-zA-Z0-9.]*\.[a-zA-Z]{2,}[^\s"\'<>]*', r.text)
                    for u in urls:
                        m = re.search(r'https?://([^/\s:\*\?]+)', u)
                        if m:
                            d = m.group(1).lower()
                            if ('.' in d and not d.endswith(('.png','.jpg','.gif','.css','.js','.ico','.svg','.woff','.ttf')) and
                                    'google' not in d and 'bootstrap' not in d and
                                    'cloudflare' not in d and 'font' not in d and
                                    'cdn' not in d and 'jquery' not in d and
                                    'twitter' not in d and 'facebook' not in d and
                                    'schema.org' not in d and 'w3.org' not in d):
                                found_domains.add(d)
                except Exception as e:
                    self.gui.post(self._jm_log,
                                       f'[域名] {site} 失败: {str(e)[:60]}', 'warning')

            # Round 2: From known domains, scrape for more domains
            known_seed = [
                '18comic.vip', '18comic.ink',
                'jmcomic-zzz.one', 'jmcomic-zzz.org',
                'comic18j-robo.me', 'comic18j-bubu.club', 'comic18j-robo.cc',
            ]
            for seed in known_seed:
                try:
                    r = req.get(f'https://{seed}/', headers=hd, proxies=proxies,
                               timeout=10, allow_redirects=True)
                    urls = re.findall(r'https?://[a-zA-Z0-9][-a-zA-Z0-9.]*\.[a-zA-Z]{2,}[^\s"\'<>]*', r.text)
                    for u in urls:
                        m = re.search(r'https?://([^/\s:\*\?]+)', u)
                        if m:
                            d = m.group(1).lower()
                            if ('.' in d and ('comic' in d or '18' in d or 'jm' in d or 'cd' in d) and
                                    not d.endswith(('.png','.jpg','.gif','.css','.js','.ico')) and
                                    'google' not in d and 'cloudflare' not in d):
                                found_domains.add(d)
                except Exception:
                    pass

            # Fallback known list
            known = [
                '18comic.vip', '18comic.ink',
                'jmcomic-zzz.one', 'jmcomic-zzz.org',
                'comic18j-robo.me', 'comic18j-bubu.club', 'comic18j-robo.cc',
                'jmcomic1.me', 'jmcomic.me', '18comic.org',
                'jm-comic2.cc', 'jm-comic3.cc',
                'www.cdnhjk.net', 'www.cdngwc.cc', 'www.cdngwc.net',
                'www.cdngwc.club', 'www.cdnhjk.cc',
                'www.jmapibackup.info', 'www.jmapinode1.top',
            ]
            found_domains.update(known)

            self.gui.post(self._jm_log,
                               f'[域名] 收集到 {len(found_domains)} 个候选域名，正在检测可用性...', 'info')

            # Round 3: Health check
            healthy = []
            for d in sorted(found_domains)[:40]:
                try:
                    r = req.get(f'https://{d}/', headers=hd, proxies=proxies, timeout=8,
                               allow_redirects=True)
                    if r.status_code < 500:
                        healthy.append(d)
                except Exception:
                    pass

            self._jm_domains = healthy
            self._jm_domain_discovered = True

            count = len(healthy)
            msg = f'[域名] 共 {count} 个可用'
            if healthy:
                names = ', '.join(healthy[:8])
                if count > 8:
                    names += f' ... +{count-8}'
                msg += f': {names}'
            self.gui.post(self._jm_log, msg, 'success')
            self.gui.post(lambda: self.jm_domain_label.config(
                                text=f'{count}个可用', foreground='#22863a'))
            self.gui.post(lambda: self.jm_domain_btn.config(state=tk.NORMAL))

        import warnings
        warnings.filterwarnings('ignore', '.*Unverified HTTPS request.*')
        threading.Thread(target=run, daemon=True).start()

    def _jm_log(self, msg, tag='info'):
        self.jm_log_area.configure(state=tk.NORMAL)
        timestamp = datetime.now().strftime('%H:%M:%S')
        self.jm_log_area.insert(tk.END, f'[{timestamp}] {msg}\n')
        self.jm_log_area.see(tk.END)
        self.jm_log_area.configure(state=tk.DISABLED)

    def _open_dir(self):
        path = self.jm_dir_var.get()
        if os.path.exists(path):
            os.startfile(path)
        else:
            messagebox.showinfo('提示', '目录不存在，下载后自动创建')

    def _set_running(self, running):
        self.running = running
        if running:
            self.jm_download_btn.configure(state=tk.DISABLED)
            self.jm_view_btn.configure(state=tk.DISABLED)
            self.jm_stop_btn.configure(state=tk.NORMAL)
            self._jm_req_count = 0
            self._update_jm_anti_status()
        else:
            self.jm_download_btn.configure(state=tk.NORMAL)
            self.jm_view_btn.configure(state=tk.NORMAL)
            self.jm_stop_btn.configure(state=tk.DISABLED)
            self._jm_thread = None

    def _stop(self):
        self._jm_stop_event.set()
        self._jm_log('正在停止...', 'warning')
        self.jm_stop_btn.configure(state=tk.DISABLED)

    def _update_jm_anti_status(self):
        proxy = self.jm_proxy_var.get().strip()
        anti = self.jm_antiblock_var.get()
        delay = self.jm_delay_var.get()

        self.jm_anti_proxy.config(
            foreground='#22863a' if proxy else 'gray',
            text='\u25cf 代理' if proxy else '\u25cb 代理')

        self.jm_anti_tls.config(
            foreground='#22863a',
            text='\u25cf TLS:Chrome')

        domain_text = '动态池' if getattr(self, '_jm_domain_discovered', False) else '内置池'
        self.jm_anti_domain.config(
            foreground='#22863a' if getattr(self, '_jm_domain_discovered', False) else '#e36209',
            text=f'\u25cf 域名:{domain_text}')

        self.jm_anti_delay.config(
            foreground='#22863a' if delay > 0 else '#e36209',
            text=f'\u25cf 延迟:{delay}s')

        self.jm_anti_retry.config(
            foreground='#22863a' if self.jm_retry_var.get() >= 5 else '#e36209',
            text=f'\u25cf 重试:{self.jm_retry_var.get()}')

        snapshot = self.scheduler.snapshot()
        mode_text = '浏览优先' if snapshot['browser_priority'] else '自适应'
        cooldown = int(snapshot['site_cooldown'] + 0.999)
        cooldown_text = f'  冷却:{cooldown}s' if cooldown else ''
        self.jm_anti_req.config(
            text=f'{mode_text}  请求:{snapshot["requests"]}  并发:{snapshot["active"]}/{snapshot["limit"]}  熔断:{snapshot["open_routes"]}{cooldown_text}')

        if self.running:
            self.gui.root.after(1000, self._update_jm_anti_status)

    def _toggle_jm_browser_priority(self):
        self.scheduler.configure(browser_priority=self.jm_browser_priority_var.get())
        self._update_jm_anti_status()

    def _get_ids(self, prefix=''):
        text = self.jm_id_text.get('1.0', tk.END).strip()
        if not text:
            return []

        ids = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if prefix and line.lower().startswith(prefix.lower()):
                ids.append(line)
            elif prefix:
                ids.append(f'{prefix}{line}')
            else:
                ids.append(line)
        return ids

    def _split_input_ids(self):
        album_ids = []
        photo_ids = []
        for raw in self._get_ids():
            text = raw.strip()
            if not text:
                continue
            if text.lower().startswith('p'):
                pid = text[1:]
                if pid.isdigit():
                    photo_ids.append(pid)
            elif text.lower().startswith('jm') and text[2:].isdigit():
                album_ids.append(text[2:])
            elif text.isdigit():
                album_ids.append(text)
        return album_ids, photo_ids

    def _get_option(self, config=None):
        if not JM_AVAILABLE:
            self._jm_log('[错误] jmcomic 模块未安装', 'error')
            return None

        config = config or {}
        base_dir = config['base_dir'] if 'base_dir' in config else self.jm_dir_var.get()
        os.makedirs(base_dir, exist_ok=True)

        proxy = config['proxy'] if 'proxy' in config else self.jm_proxy_var.get().strip() or None
        proxy_pool = parse_proxy_pool(proxy)
        primary_proxy = next((item for item in proxy_pool if item), None)

        anti = config['anti'] if 'anti' in config else self.jm_antiblock_var.get()
        retry_value = config['retry'] if 'retry' in config else self.jm_retry_var.get()
        image_threads = (config['image_threads'] if 'image_threads' in config
                         else self.jm_image_threads_var.get())
        photo_threads_value = (config['photo_threads'] if 'photo_threads' in config
                               else self.jm_photo_threads_var.get())
        if anti == '保守':
            retry_times = retry_value
            img_threads = min(image_threads, 4)
            photo_threads = min(photo_threads_value, 2)
        elif anti == '激进':
            retry_times = max(retry_value, 12)
            img_threads = min(image_threads, 16)
            photo_threads = min(photo_threads_value, 4)
        else:
            retry_times = retry_value
            img_threads = image_threads
            photo_threads = photo_threads_value
        browser_priority = (config['browser_priority'] if 'browser_priority' in config
                            else self.jm_browser_priority_var.get())
        self.scheduler.configure(max_concurrency=img_threads, proxies=proxy_pool,
                                 browser_priority=browser_priority)

        if hasattr(self, '_jm_domains') and self._jm_domains:
            # Use dynamically discovered domains
            domains = {
                'api': self._jm_domains,
                'html': self._jm_domains,
            }
            self.gui.post(self._jm_log,
                                f'使用动态域名: {len(self._jm_domains)} 个', 'info')
        else:
            domains = {
                'api': [
                    'www.cdnhjk.net', 'www.cdngwc.cc', 'www.cdngwc.net',
                    'www.cdngwc.club', 'www.cdnhjk.cc',
                    'www.jmapibackup.info', 'www.jmapinode1.top',
                ],
                'html': [
                    'jmcomic1.me', 'jmcomic.me', '18comic.vip', '18comic.org',
                    'jm-comic2.cc', 'jm-comic3.cc',
                    'jmcomic-zzz.one', 'jmcomic-zzz.org',
                    'comic18j-robo.me', 'comic18j-bubu.club', 'comic18j-robo.cc',
                    '18comic.ink',
                ],
            }

        option_data = {
            'log': True,
            'dir_rule': {
                'base_dir': base_dir,
                'rule': 'Bd_Aauthor_Atitle_Pindex',
            },
            'client': {
                'impl': config['mode'] if 'mode' in config else self.jm_mode_var.get(),
                'retry_times': retry_times,
                'domain': domains.get(config['mode'] if 'mode' in config else self.jm_mode_var.get(),
                                      ['18comic.vip']),
                'postman': {
                    'type': 'curl_cffi',
                    'meta_data': {
                        'impersonate': 'chrome',
                        'proxies': primary_proxy,
                    }
                },
            },
            'download': {
                'image': {'suffix': '.jpg'},
                'threading': {
                    'image': img_threads,
                    'photo': photo_threads,
                },
            },
        }

        username = config['username'] if 'username' in config else self.jm_username_var.get().strip()
        password = config['password'] if 'password' in config else self.jm_password_var.get().strip()

        if username and password:
            option_data.setdefault('plugins', {})
            option_data['plugins']['after_init'] = [{
                'plugin': 'login',
                'kwargs': {'username': username, 'password': password}
            }]

        try:
            option = jmcomic.JmOption.construct(option_data)
            strategy = JmAdaptiveStrategy(self.scheduler, self._jm_stop_event)
            original_new_client = option.new_jm_client

            def adaptive_new_client(*args, **kwargs):
                kwargs.setdefault('domain_retry_strategy', strategy)
                return original_new_client(*args, **kwargs)

            option.new_jm_client = adaptive_new_client
            return option
        except Exception as e:
            self.gui.post(self._jm_log, f'创建配置失败: {e}', 'error')
            return None

    def _get_downloaded_cache(self, output_dir=None):
        output_dir = output_dir or self.jm_dir_var.get()
        index = load_download_index(output_dir)

        cache_file = Path(output_dir) / '.downloaded.json'
        if cache_file.exists():
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    legacy = json.load(f)
                if isinstance(legacy, dict):
                    for gid, entry in legacy.items():
                        if isinstance(entry, dict):
                            current = index.setdefault(str(gid), {})
                            for key, value in entry.items():
                                current.setdefault(key, value)
                return index
            except Exception:
                pass
        return index

    def _save_downloaded_cache(self, cache, output_dir=None):
        output_dir = output_dir or self.jm_dir_var.get()
        for gid, entry in cache.items():
            if isinstance(entry, dict):
                update_download_index(output_dir, gid, **entry)

    def _mark_downloaded(self, jm_id, title='', author='', output_dir=None,
                         status='complete', total=0, missing=0):
        cache = self._get_downloaded_cache(output_dir)
        entry = {
            'title': title,
            'author': author,
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'status': status,
            'total': total,
            'missing': missing,
        }
        cache[str(jm_id)] = entry
        self._save_downloaded_cache(cache, output_dir)

    def _is_downloaded(self, jm_id):
        cache = self._get_downloaded_cache()
        return str(jm_id) in cache

    def _build_expected_dir(self, album, option):
        base_dir = option.dir_rule.base_dir
        rule = option.dir_rule.rule_dsl
        parts = []
        for seg in rule.split('_'):
            seg = seg.strip()
            if seg == 'Bd':
                parts.append(base_dir)
            elif seg == 'Aauthor':
                author = album.authors[0] if album.authors else 'default_author'
                parts.append(sanitize_filename(author))
            elif seg == 'Atitle':
                parts.append(sanitize_filename(album.name))
            elif seg.startswith('A'):
                key = seg[1:]
                val = getattr(album, key, None) or getattr(album, key.lower(), None) or ''
                parts.append(sanitize_filename(str(val)))
            elif seg == 'Pindex':
                parts.append('')  # photo level, not album level
        path = os.path.join(*[p for p in parts if p])
        return Path(path)

    def _dir_has_images(self, dir_path):
        if not dir_path.exists():
            return False
        for ext in ('*.jpg', '*.png', '*.webp', '*.jpeg', '*.gif'):
            if list(dir_path.glob(ext)):
                return True
        for child in dir_path.iterdir():
            if child.is_dir():
                if self._dir_has_images(child):
                    return True
        return False

    @staticmethod
    def _count_valid_images(dir_path):
        count = 0
        if not dir_path.exists():
            return count
        for path in dir_path.rglob('*'):
            if not path.is_file() or path.suffix.lower() not in {'.jpg', '.jpeg', '.png', '.webp', '.gif'}:
                continue
            try:
                with Image.open(path) as image:
                    image.verify()
                count += 1
            except Exception:
                path.unlink(missing_ok=True)
        return count

    def _check_album_downloaded(self, aid, option, output_dir=None):
        cache = self._get_downloaded_cache(output_dir)
        try:
            client = option.new_jm_client()
            album = client.get_album_detail(aid)
            expected_dir = self._build_expected_dir(album, option)
            expected_total = sum(len(photo) for photo in album)
            valid_count = self._count_valid_images(expected_dir)
            if valid_count >= expected_total > 0:
                if cache.get(str(aid), {}).get('status') != 'complete':
                    self._mark_downloaded(
                        aid, album.name, ', '.join(album.authors) if album.authors else '',
                        output_dir, status='complete', total=expected_total, missing=0)
                return True
            if cache.get(str(aid), {}).get('status') == 'complete':
                self._mark_downloaded(
                    aid, album.name, ', '.join(album.authors) if album.authors else '',
                    output_dir, status='partial', total=expected_total,
                    missing=max(0, expected_total - valid_count))
        except Exception:
            return False
        return False

    def _scan_existing_albums(self, album_ids):
        existing = []
        cache = self._get_downloaded_cache()
        for aid in album_ids:
            if str(aid) in cache:
                existing.append(aid)
        return list(dict.fromkeys(existing))

    def _check_existing(self):
        aids = self._get_ids()
        if not aids:
            self.jm_existing_label.config(text='')
            return
        cache = self._get_downloaded_cache()
        existing = [a for a in aids if cache.get(str(a), {}).get('status') == 'complete']
        if existing:
            self.jm_existing_label.config(
                text=f'已缓存 {len(existing)}/{len(aids)}: {", ".join(existing)}',
                foreground='#22863a')
        else:
            self.jm_existing_label.config(
                text=f'输入 {len(aids)} 本',
                foreground='gray')

    def _start_download(self):
        if self.running:
            self._jm_log('[提示] 已有 JM 任务正在运行', 'warning')
            return
        if not JM_AVAILABLE:
            self._jm_log('[错误] jmcomic 模块未安装', 'error')
            return

        album_ids, photo_ids = self._split_input_ids()

        if not album_ids and not photo_ids:
            self._jm_log('[提示] 请输入至少一个本子ID或章节ID', 'warning')
            return

        self._jm_stop_event.clear()
        self._set_running(True)
        config = {
            'base_dir': self.jm_dir_var.get(),
            'proxy': self.jm_proxy_var.get().strip() or None,
            'mode': self.jm_mode_var.get(),
            'anti': self.jm_antiblock_var.get(),
            'retry': self.jm_retry_var.get(),
            'image_threads': self.jm_image_threads_var.get(),
            'photo_threads': self.jm_photo_threads_var.get(),
            'delay': self.jm_delay_var.get(),
            'username': self.jm_username_var.get().strip(),
            'password': self.jm_password_var.get().strip(),
            'browser_priority': self.jm_browser_priority_var.get(),
        }
        panel = self

        class GuiProgressDler(JmDownloader):
            def __init__(self, option):
                super().__init__(option, cancel_event=panel._jm_stop_event)

            def before_photo(self, photo):
                super().before_photo(photo)
                panel.gui.post(panel._jm_log,
                    f'  章节: {photo.name} ({photo.index}/{len(photo.from_album)}) [{len(photo)}张]')

        def run():
            completed_count = partial_count = failed_count = 0
            try:
                option = self._get_option(config)
                if option is None:
                    return

                all_ids = list(album_ids)
                skipped, td = [], []
                for aid in all_ids:
                    if self._check_album_downloaded(aid, option, config['base_dir']):
                        skipped.append(aid)
                        self.gui.post(self._jm_log, f'{aid} 已下载，跳过', 'success')
                    else:
                        td.append(aid)

                if skipped:
                    self.gui.post(self._jm_log, f'跳过 {len(skipped)} 本已下载的', 'success')
                if not td and not photo_ids:
                    self.gui.post(self._jm_log, '全部已下载', 'success')
                    return

                album_cnt = len(td)
                total_to_dl = album_cnt + len(photo_ids)

                if album_cnt:
                    self.gui.post(self._jm_log, f'开始下载 {album_cnt} 本', 'header')

                for i, aid in enumerate(td, 1):
                    if self._jm_stop_event.is_set():
                        break
                    try:
                        album, dler = download_album(aid, option, downloader=GuiProgressDler,
                                                     check_exception=False)
                        if album is None or self._jm_stop_event.is_set():
                            break
                        title = album.name if hasattr(album, 'name') else ''
                        author = ', '.join(album.authors) if album.authors else ''
                        complete = dler.all_success and not self._jm_stop_event.is_set()
                        total_images = sum(len(photo) for photo in album)
                        failed_images = len(dler.download_failed_image)
                        failed_photos = len(dler.download_failed_photo)
                        downloaded_images = sum(
                            len(images)
                            for photo_map in dler.download_success_dict.values()
                            for images in photo_map.values()
                        )
                        missing = max(total_images - downloaded_images, failed_images, failed_photos)
                        self._mark_downloaded(
                            aid, title, author, config['base_dir'],
                            status='complete' if complete else 'partial',
                            total=total_images, missing=missing,
                        )
                        append_download_history(
                            'JM Comic', aid, title,
                            'complete' if complete else 'partial', total_images, missing,
                            path=config['base_dir'],
                            error=(f'{failed_images} 张图片、{failed_photos} 个章节失败'
                                   if not complete else None))
                        self.gui.post(self.gui.refresh_history)
                        icon = '\u2705' if complete else '\u26a0'
                        result_text = '\u5b8c\u6210' if complete else '\u90e8\u5206\u5b8c\u6210'
                        self.gui.post(self._jm_log,
                            f'{icon} {aid} [{title[:20]}] {result_text} ({i}/{album_cnt})',
                            'success' if complete else 'warning')
                        if complete:
                            completed_count += 1
                        else:
                            partial_count += 1
                    except Exception as e:
                        if self._jm_stop_event.is_set():
                            break
                        failed_count += 1
                        append_download_history('JM Comic', aid, aid, 'failed', error=e)
                        self.gui.post(self._jm_log,
                            f'\u274c {aid} \u5931\u8d25: {e}', 'error')
                        self.gui.post(self.gui.refresh_history)
                    if i < album_cnt:
                        self._jm_stop_event.wait(config['delay'])

                for i, pid in enumerate(photo_ids, album_cnt + 1):
                    if self._jm_stop_event.is_set():
                        break
                    try:
                        photo, dler = download_photo(pid, option, downloader=GuiProgressDler,
                                                     check_exception=False)
                        if self._jm_stop_event.is_set():
                            append_download_history('JM Comic', f'p{pid}', f'章节 {pid}',
                                                    'cancelled', error='用户取消')
                            break
                        complete = dler.all_success
                        status = 'complete' if complete else 'partial'
                        missing = len(dler.download_failed_image)
                        append_download_history('JM Comic', f'p{pid}', f'章节 {pid}', status,
                                                total=len(photo) if photo else 0, missing=missing,
                                                error=f'{missing} 张图片失败' if missing else None)
                        self.gui.post(self._jm_log,
                            f'{"完成" if complete else "部分完成"} 章节 {pid} ({i}/{total_to_dl})',
                            'success' if complete else 'warning')
                        if complete:
                            completed_count += 1
                        else:
                            partial_count += 1
                    except Exception as e:
                        if self._jm_stop_event.is_set():
                            append_download_history('JM Comic', f'p{pid}', f'章节 {pid}',
                                                    'cancelled', error='用户取消')
                            break
                        failed_count += 1
                        append_download_history('JM Comic', f'p{pid}', f'章节 {pid}', 'failed', error=e)
                        self.gui.post(self._jm_log,
                            f'\u274c \u7ae0\u8282 {pid} \u5931\u8d25: {e}', 'error')
                    if i < total_to_dl:
                        self._jm_stop_event.wait(config['delay'])

                if self._jm_stop_event.is_set():
                    summary = (f'任务已停止：完整 {completed_count}，部分 {partial_count}，'
                               f'失败 {failed_count}')
                    tag = 'warning'
                else:
                    summary = (f'任务结束：完整 {completed_count}，部分 {partial_count}，'
                               f'失败 {failed_count}，跳过 {len(skipped)}')
                    tag = 'success' if partial_count == 0 and failed_count == 0 else 'warning'
                self.gui.post(self._jm_log, summary, tag)
            except Exception as e:
                self.gui.post(self._jm_log, f'\u9519\u8bef: {e}', 'error')
            finally:
                self.gui.post(self._set_running, False)

        self._jm_thread = threading.Thread(target=run, daemon=True)
        self._jm_thread.start()

    def _view_album(self):
        if self.running:
            self._jm_log('[提示] 已有 JM 任务正在运行', 'warning')
            return
        if not JM_AVAILABLE:
            self._jm_log('[错误] jmcomic 模块未安装', 'error')
            return

        album_ids, _ = self._split_input_ids()
        if not album_ids:
            self._jm_log('[提示] 请输入至少一个本子ID', 'warning')
            return

        self._set_running(True)
        self._jm_log(f'查询本子信息: {album_ids}')
        config = {
            'base_dir': self.jm_dir_var.get(),
            'proxy': self.jm_proxy_var.get().strip() or None,
            'mode': self.jm_mode_var.get(),
            'anti': self.jm_antiblock_var.get(),
            'retry': self.jm_retry_var.get(),
            'image_threads': self.jm_image_threads_var.get(),
            'photo_threads': self.jm_photo_threads_var.get(),
            'username': self.jm_username_var.get().strip(),
            'password': self.jm_password_var.get().strip(),
            'browser_priority': self.jm_browser_priority_var.get(),
        }

        def run():
            try:
                option = self._get_option(config)
                if option is None:
                    return
                client = option.new_jm_client()
                for aid in album_ids:
                    try:
                        album = client.get_album_detail(aid)
                        info = (
                            f"\n{'=' * 50}\n"
                            f"标题: {album.name}\n"
                            f"ID: JM{album.album_id}\n"
                            f"作者: {', '.join(album.authors) if album.authors else '未知'}\n"
                            f"页数: {album.page_count}\n"
                            f"章节数: {len(album.episode_list)}\n"
                            f"标签: {', '.join(album.tags[:10]) if album.tags else '无'}\n"
                            f"{'=' * 50}"
                        )
                        self.gui.post(self._jm_log, info)
                    except Exception as e:
                        self.gui.post(self._jm_log, f'查询 {aid} 失败: {e}', 'error')
                self.gui.post(self._jm_log, '查询完成!', 'success')
            except Exception as e:
                self.gui.post(self._jm_log, f'错误: {e}', 'error')
            finally:
                self.gui.post(self._set_running, False)

        threading.Thread(target=run, daemon=True).start()


# ==================== 每周必看面板 ====================
class WeeklyPanel(ttk.Frame):
    def __init__(self, parent, gui):
        super().__init__(parent)
        self.gui = gui
        self._data = None
        self._current_type = ''
        self._selected_category = ''
        self._photos = []
        self._cover_domain_idx = 0
        self._cover_urls = {}
        self._load_job = None
        self._cover_cache_dir = Path(__file__).parent / '.cache' / 'weekly_covers'
        self._cover_cache_dir.mkdir(parents=True, exist_ok=True)
        self._weekly_cache_dir = Path(__file__).parent / '.cache' / 'weekly_data'
        self._weekly_cache_dir.mkdir(parents=True, exist_ok=True)
        self._weekly_cache_lock = threading.Lock()
        self._cover_locks = {}
        self._cover_locks_guard = threading.Lock()
        self.setup_ui()

    def setup_ui(self):
        header = ttk.Frame(self, padding=5)
        header.pack(fill=tk.X)
        ttk.Label(header, text='每周必看 - JM Comic',
                  font=('Microsoft YaHei UI', 11, 'bold')).pack(side=tk.LEFT)
        self._status_lbl = ttk.Label(header, text='正在发现可用CDN...', foreground='gray')
        self._status_lbl.pack(side=tk.LEFT, padx=8)
        self._refresh_btn = ttk.Button(header, text='刷新内容', command=self.load_data)
        self._refresh_btn.pack(side=tk.RIGHT, padx=4)
        self._cover_refresh_btn = ttk.Button(header, text='补齐封面', command=self.refresh_covers)
        self._cover_refresh_btn.pack(side=tk.RIGHT, padx=4)
        self._cover_clear_btn = ttk.Button(header, text='清理缓存', command=self.clear_cover_cache)
        self._cover_clear_btn.pack(side=tk.RIGHT, padx=4)
        self._discover_btn = ttk.Button(header, text='发现域名', command=self._discover_domains)
        self._discover_btn.pack(side=tk.RIGHT, padx=4)

        sel_frame = ttk.Frame(self, padding=5)
        sel_frame.pack(fill=tk.X)
        ttk.Label(sel_frame, text='期数:').pack(side=tk.LEFT)
        self._category_var = tk.StringVar()
        self._category_combo = ttk.Combobox(sel_frame, textvariable=self._category_var,
                                            state='readonly', width=28)
        self._category_combo.pack(side=tk.LEFT, padx=4)
        self._category_combo.bind('<<ComboboxSelected>>', self._on_category_change)
        self._loading_lbl = ttk.Label(sel_frame, text='', foreground='#e36209')
        self._loading_lbl.pack(side=tk.LEFT, padx=4)
        self._cache_lbl = ttk.Label(sel_frame, text=self._cover_cache_summary(), foreground='#666666')
        self._cache_lbl.pack(side=tk.RIGHT, padx=4)

        self._tab_frame = ttk.Frame(self, padding=5)
        self._tab_frame.pack(fill=tk.X)

        self._canvas = tk.Canvas(self, highlightthickness=0, bg='#f0f0f0')
        self._scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._scrollbar.set)
        self._scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._inner = ttk.Frame(self._canvas)
        self._canvas_window = self._canvas.create_window((0, 0), window=self._inner, anchor='nw')
        self._inner.bind('<Configure>', lambda e: self._canvas.configure(scrollregion=self._canvas.bbox('all')))
        self._canvas.bind('<Configure>', self._on_canvas_resize)
        for w in (self._canvas, self._inner):
            w.bind('<MouseWheel>', self._on_wheel)
            w.bind('<Enter>', lambda e, widget=w: widget.bind_all('<MouseWheel>', self._on_wheel))
            w.bind('<Leave>', lambda e: self._canvas.unbind_all('<MouseWheel>'))

        self.after(200, self.load_data)

    def _on_wheel(self, event):
        self._canvas.yview_scroll(int(-1 * (event.delta / 120)), 'units')

    def _on_canvas_resize(self, event):
        self._canvas.itemconfigure(self._canvas_window, width=event.width)
        if getattr(self, '_layout_after', None):
            self.after_cancel(self._layout_after)
        self._layout_after = self.after(180, self._reflow_cards)

    def _reflow_cards(self):
        cards = getattr(self, '_card_widgets', [])
        if not cards:
            return
        cols = self._calc_columns()
        for idx, item in enumerate(cards):
            item['card'].grid_configure(row=idx // cols, column=idx % cols)
        for col in range(8):
            self._inner.columnconfigure(col, weight=1 if col < cols else 0)

    def _discover_domains(self):
        self._discover_btn.config(state=tk.DISABLED)
        self._status_lbl.config(text='发现域名中...', foreground='#e36209')
        self._cover_domain_idx = 0
        proxy = self.gui.jm_tab.jm_proxy_var.get().strip() or None
        threading.Thread(target=self._do_discover_domains, args=(proxy,), daemon=True).start()

    def _do_discover_domains(self, proxy=None):
        import jmcomic
        from jmcomic import JmModuleConfig
        import requests as req
        import warnings
        warnings.filterwarnings('ignore', '.*Unverified HTTPS request.*')

        hd = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'
        }
        proxies = {'http': proxy, 'https': proxy} if proxy else None

        found = set(JmModuleConfig.DOMAIN_IMAGE_LIST)
        publish_sites = [
            'https://jmcomicog.net/', 'https://jmcomicgo.org/',
            'https://18comic.vip/', 'https://18comic.ink/',
        ]

        for site in publish_sites:
            try:
                r = req.get(site, headers=hd, proxies=proxies, timeout=12,
                            allow_redirects=True)
                urls = re.findall(r'https?://[a-zA-Z0-9][-a-zA-Z0-9.]*\.[a-zA-Z]{2,}[^\s"\'<>]*', r.text)
                for u in urls:
                    m = re.match(r'https?://([^/\s:]+)', u)
                    if m:
                        d = m.group(1).lower()
                        if ('msp' in d or 'cdn' in d or 'img' in d) and \
                           not d.endswith(('.png','.jpg','.css','.js','.ico','.svg','.woff','.ttf')):
                            found.add(d)
            except Exception:
                pass
        self._cover_urls = {}
        ordered = [d for d in found if '.' in d and 'msp' in d]
        if ordered:
            self._cover_domains = ordered + [d for d in found if d not in ordered]
        else:
            self._cover_domains = list(found)
        self.gui.post(lambda: self._status_lbl.config(
            text=f'{len(found)} 个图片CDN可用', foreground='#22863a'))
        self.gui.post(lambda: self._discover_btn.config(state=tk.NORMAL))

    def load_data(self):
        self._refresh_btn.config(state=tk.DISABLED)
        self._status_lbl.config(text='加载期数中...', foreground='#e36209')
        option = self._get_jm_option()
        threading.Thread(target=self._do_load, args=(option,), daemon=True).start()

    def _do_load(self, option):
        try:
            if not JM_AVAILABLE:
                self.gui.post(lambda: self._status_lbl.config(text='jmcomic未安装', foreground='#d73a49'))
                return
            if option is None:
                self.gui.post(lambda: self._status_lbl.config(text='配置失败', foreground='#d73a49'))
                return
            client = option.new_jm_client()
            weekly_info = self._to_plain_data(client.get_weekly_info())
            self._data = weekly_info
            try:
                self._save_weekly_json('weekly_info.json', weekly_info)
            except Exception:
                pass
            cats = weekly_info.get('categories', [])
            types = weekly_info.get('type', [])
            self.gui.post(self._build_ui, cats, types)
        except Exception as e:
            error_text = str(e)[:40]
            cached = self._load_weekly_json('weekly_info.json')
            if cached:
                self._data = cached
                cats = cached.get('categories', [])
                types = cached.get('type', [])
                self.gui.post(self._build_ui, cats, types)
                self.gui.post(lambda: self._status_lbl.config(
                    text='网络不可用，已加载本地榜单', foreground='#b35c00'))
            else:
                self.gui.post(lambda: self._status_lbl.config(
                    text=f'加载失败: {error_text}', foreground='#d73a49'))
        finally:
            self.gui.post(lambda: self._refresh_btn.config(state=tk.NORMAL))

    def _get_jm_option(self):
        try:
            import jmcomic
            jm_tab = self.gui.jm_tab
            base_dir = jm_tab.jm_dir_var.get()
            os.makedirs(base_dir, exist_ok=True)
            proxy_str = jm_tab.jm_proxy_var.get().strip()
            proxy_pool = parse_proxy_pool(proxy_str)
            primary_proxy = next((item for item in proxy_pool if item), None)
            jm_tab.scheduler.configure(proxies=proxy_pool)
            if hasattr(jm_tab, '_jm_domains') and jm_tab._jm_domains:
                domains = jm_tab._jm_domains
            else:
                domains = ['www.cdngwc.cc', 'www.cdnhjk.net', 'www.jmapinode1.top']
            option = jmcomic.JmOption.construct({
                'log': False,
                'dir_rule': {'base_dir': base_dir, 'rule': 'Bd_Atitle_Pindex'},
                'client': {
                    'impl': 'api',
                    'retry_times': 3,
                    'domain': domains,
                    'postman': {
                        'type': 'curl_cffi',
                        'meta_data': {
                            'impersonate': 'chrome',
                            'proxies': primary_proxy,
                        }
                    },
                },
                'download': {
                    'image': {'suffix': '.jpg'},
                    'threading': {'image': 1, 'photo': 1},
                },
            })
            strategy = JmAdaptiveStrategy(jm_tab.scheduler, jm_tab._jm_stop_event)
            original_new_client = option.new_jm_client

            def adaptive_new_client(*args, **kwargs):
                kwargs.setdefault('domain_retry_strategy', strategy)
                return original_new_client(*args, **kwargs)

            option.new_jm_client = adaptive_new_client
            return option
        except Exception:
            return None

    def _build_ui(self, categories, types):
        self._status_lbl.config(text=f'{len(categories)} 期可用', foreground='#22863a')
        cat_options = []
        cat_map = {}
        for c in categories:
            label = f'{c.get("title","")}  ({c.get("time","")})'
            cat_options.append(label)
            cat_map[label] = c.get('id', '')
        self._category_combo['values'] = cat_options
        if cat_options:
            self._category_combo.current(0)
            self._category_map = cat_map
            self._change_category()

        for w in self._tab_frame.winfo_children():
            w.destroy()
        self._type_btns = {}
        for t in types:
            tid = t.get('id', '')
            tname = t.get('title', tid)
            btn = ttk.Button(self._tab_frame, text=tname, width=8,
                             command=lambda tid=tid: self._switch_type(tid))
            btn.pack(side=tk.LEFT, padx=2)
            self._type_btns[tid] = btn
        if types:
            first_id = types[0].get('id', '')
            self._switch_type(first_id)

    def _on_category_change(self, event=None):
        self._change_category()

    def _change_category(self):
        label = self._category_var.get()
        self._selected_category = self._category_map.get(label, '')
        self._fetch_weekly()

    def _switch_type(self, type_id):
        self._current_type = type_id
        for tid, btn in self._type_btns.items():
            btn.state(['pressed' if tid == type_id else '!pressed'])
        self._fetch_weekly()

    def _fetch_weekly(self):
        if not self._selected_category or not self._current_type:
            return
        self._loading_lbl.config(text='加载中...')
        self._fetch_id = getattr(self, '_fetch_id', 0) + 1
        fid = self._fetch_id
        category_id = self._selected_category
        type_id = self._current_type
        filename = self._weekly_result_filename(category_id, type_id)
        option = self._get_jm_option()
        threading.Thread(target=self._do_fetch_weekly,
                         args=(fid, category_id, type_id, filename, option), daemon=True).start()

    def _do_fetch_weekly(self, fid, category_id, type_id, filename, option):
        if fid != getattr(self, '_fetch_id', 0):
            return
        try:
            if option is None:
                return
            client = option.new_jm_client()
            result = self._to_plain_data(client.get_weekly(category_id, type_id))
            if fid == getattr(self, '_fetch_id', 0):
                try:
                    self._save_weekly_json(filename, result)
                except Exception:
                    pass
            if fid == getattr(self, '_fetch_id', 0):
                self.gui.post(self._render_cards, result)
        except Exception as e:
            error_text = str(e)[:30]
            cached = self._load_weekly_json(filename)
            if cached and fid == getattr(self, '_fetch_id', 0):
                self.gui.post(self._render_cards, cached)
                self.gui.post(lambda: self._loading_lbl.config(text='已使用本地榜单'))
            elif fid == getattr(self, '_fetch_id', 0):
                self.gui.post(lambda: self._loading_lbl.config(
                    text=f'加载失败: {error_text}'))
        finally:
            if fid == getattr(self, '_fetch_id', 0):
                self.gui.post_after(1200, self._clear_weekly_loading, fid)

    def _clear_weekly_loading(self, fid):
        if fid == getattr(self, '_fetch_id', 0):
            self._loading_lbl.config(text='')

    def _weekly_result_filename(self, category_id=None, type_id=None):
        category = re.sub(r'[^a-zA-Z0-9_-]', '_', str(
            self._selected_category if category_id is None else category_id))
        weekly_type = re.sub(r'[^a-zA-Z0-9_-]', '_', str(
            self._current_type if type_id is None else type_id))
        return f'{category}_{weekly_type}.json'

    @classmethod
    def _to_plain_data(cls, value):
        if isinstance(value, dict):
            return {str(key): cls._to_plain_data(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._to_plain_data(item) for item in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if hasattr(value, 'items'):
            return {str(key): cls._to_plain_data(item) for key, item in value.items()}
        if hasattr(value, '__dict__'):
            return cls._to_plain_data(vars(value))
        return str(value)

    def _save_weekly_json(self, filename, data):
        path = self._weekly_cache_dir / filename
        temp_path = path.with_name(path.name + '.tmp')
        with self._weekly_cache_lock:
            try:
                with open(temp_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                temp_path.replace(path)
            finally:
                temp_path.unlink(missing_ok=True)

    def _load_weekly_json(self, filename):
        path = self._weekly_cache_dir / filename
        if not path.exists():
            return None
        with self._weekly_cache_lock:
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else None
            except Exception:
                path.unlink(missing_ok=True)
                return None

    def _render_cards(self, result):
        for w in self._inner.winfo_children():
            w.destroy()
        self._photos.clear()
        self._load_gen = getattr(self, '_load_gen', 0) + 1
        comic_list = result.get('list', []) if isinstance(result, dict) else getattr(result, 'list', [])
        total = result.get('total', 0) if isinstance(result, dict) else getattr(result, 'total', 0)
        self._status_lbl.config(text=f'{len(comic_list)}/{total} 本', foreground='#22863a')

        if not comic_list:
            ttk.Label(self._inner, text='本期暂无数据', foreground='gray').pack(pady=20)
            return

        cols = self._calc_columns()
        self._card_widgets = []
        for idx, comic in enumerate(comic_list):
            row, col = idx // cols, idx % cols
            card_data = self._create_card(comic, row, col, cols, idx)
            self._card_widgets.append(card_data)

        for c in range(cols):
            self._inner.columnconfigure(c, weight=1)

        self._load_covers_async(self._load_gen)
        output_dir = self.gui.jm_tab.jm_dir_var.get()
        threading.Thread(target=self._refresh_card_statuses,
                         args=(self._card_widgets, self._load_gen, output_dir), daemon=True).start()

    def refresh_covers(self):
        for item in getattr(self, '_card_widgets', []):
            item['loaded'] = False
            try:
                item['thumb'].configure(image='', text='读取本地封面..', bg='#e8e8e8')
            except tk.TclError:
                pass
        self._photos.clear()
        self._load_gen = getattr(self, '_load_gen', 0) + 1
        self._load_covers_async(self._load_gen)

    def clear_cover_cache(self):
        if not messagebox.askyesno('清理封面缓存', '确定删除所有已保存的每周必看封面？'):
            return
        self._load_gen = getattr(self, '_load_gen', 0) + 1
        self._cover_urls.clear()
        self._photos.clear()
        for item in getattr(self, '_card_widgets', []):
            item['loaded'] = False
            try:
                item['thumb'].configure(image='', text='本地无封面', bg='#eeeeee', fg='#777777')
            except tk.TclError:
                pass
        self._cover_refresh_btn.config(state=tk.DISABLED)
        self._cache_lbl.config(text='正在清理本地封面库...')

        def clear_files():
            with self._cover_locks_guard:
                locks = list(self._cover_locks.values())
            removed = 0
            for lock in locks:
                lock.acquire()
            try:
                for path in self._cover_cache_dir.glob('*.jpg'):
                    try:
                        path.unlink()
                        removed += 1
                    except OSError:
                        pass
            finally:
                for lock in reversed(locks):
                    lock.release()
            self.gui.post(self._finish_clear_cover_cache, removed)

        threading.Thread(target=clear_files, daemon=True).start()

    def _finish_clear_cover_cache(self, removed):
        self._cover_refresh_btn.config(state=tk.NORMAL)
        self._cache_lbl.config(text='本地封面库: 0 张')
        self._status_lbl.config(text=f'已清理 {removed} 张封面', foreground='#666666')

    def _cover_cache_summary(self):
        count = sum(1 for path in self._cover_cache_dir.glob('*.jpg')
                    if '.part.' not in path.name)
        return f'本地封面库: {count} 张'

    def _cover_lock(self, comic_id):
        with self._cover_locks_guard:
            return self._cover_locks.setdefault(str(comic_id), threading.Lock())

    def _calc_columns(self):
        w = self._canvas.winfo_width()
        return max(2, w // 238) if w > 10 else 5

    def _create_card(self, comic, row, col, cols, idx):
        cid = str(comic.get('id', '')) if isinstance(comic, dict) else str(getattr(comic, 'id', ''))
        name = comic.get('name', '') if isinstance(comic, dict) else getattr(comic, 'name', '')
        author = comic.get('author', '') if isinstance(comic, dict) else getattr(comic, 'author', '')

        card = tk.Frame(self._inner, bg='white', relief=tk.SOLID, bd=1,
                        width=222, height=370)
        card.grid(row=row, column=col, padx=7, pady=7, sticky='n')
        card.grid_propagate(False)

        cover_frame = tk.Frame(card, bg='#e8e8e8', width=204, height=270)
        cover_frame.pack(padx=8, pady=(8, 5))
        cover_frame.pack_propagate(False)
        thumb_label = tk.Label(cover_frame, text='加载封面...', bg='#e8e8e8',
                               font=('Microsoft YaHei UI', 8), fg='#777777')
        thumb_label.pack(fill=tk.BOTH, expand=True)

        display_name = name if name else f'JM{cid}'
        tk.Label(card, text=display_name, bg='white', font=('Microsoft YaHei UI', 9, 'bold'),
                 anchor=tk.W, justify=tk.LEFT, wraplength=202, height=2).pack(fill=tk.X, padx=9)

        meta_frame = tk.Frame(card, bg='white')
        meta_frame.pack(fill=tk.X, padx=9, pady=(2, 0))
        meta_text = author[:16] if author else f'JM{cid}'
        tk.Label(meta_frame, text=meta_text, bg='white', fg='#777777', anchor=tk.W,
                 font=('Microsoft YaHei UI', 7)).pack(side=tk.LEFT, fill=tk.X, expand=True)
        status_label = tk.Label(meta_frame, text='检查中', bg='white', fg='#777777',
                                font=('Microsoft YaHei UI', 7, 'bold'))
        status_label.pack(side=tk.RIGHT)

        btn_frame = tk.Frame(card, bg='white')
        btn_frame.pack(fill=tk.X, padx=8, pady=(5, 7))
        dl_btn = tk.Label(btn_frame, text='下载', bg='#4a9eff', fg='white',
                           font=('Microsoft YaHei UI', 8), cursor='hand2', pady=3)
        dl_btn.pack(fill=tk.X, expand=True)
        dl_btn.bind('<Button-1>', lambda e, cid=cid, nm=name[:20]: self._confirm_download(cid, nm))

        return {'card': card, 'thumb': thumb_label, 'status': status_label,
                'cid': cid, 'name': name[:20], 'author': author[:18], 'loaded': False}

    def _load_covers_async(self, gen):
        items = list(getattr(self, '_card_widgets', []))
        if not items:
            return
        proxy = self.gui.jm_tab.jm_proxy_var.get().strip() or None
        self._cover_refresh_btn.config(state=tk.DISABLED)
        cached_ids = {item['cid'] for item in items if self._cached_cover_is_valid(item['cid'])}
        cached_count = len(cached_ids)
        self._cache_lbl.config(text=f'封面: 本地 {cached_count}/{len(items)}，后台补齐中')

        def load_all():
            from jmcomic import JmModuleConfig
            completed = 0
            saved_ids = set(cached_ids)
            with ThreadPoolExecutor(max_workers=min(6, len(items))) as pool:
                futures = {
                    pool.submit(self._try_load_cover, item['cid'], JmModuleConfig, proxy, gen): item
                    for item in items
                }
                for future in as_completed(futures):
                    item = futures[future]
                    try:
                        image = future.result()
                    except Exception:
                        image = None
                    completed += 1
                    if image is not None and self._cached_cover_is_valid(item['cid']):
                        saved_ids.add(item['cid'])
                    if getattr(self, '_load_gen', 0) == gen:
                        self.gui.post(self._apply_thumb, item, image, gen)
                        self.gui.post(lambda c=completed, s=len(saved_ids), t=len(items):
                                   self._cache_lbl.config(text=f'封面: {c}/{t} 已处理，本地 {s}/{t}'))
            if getattr(self, '_load_gen', 0) == gen:
                self.gui.post(lambda: self._cover_refresh_btn.config(state=tk.NORMAL))
                self.gui.post(lambda: self._cache_lbl.config(text=self._cover_cache_summary()))

        threading.Thread(target=load_all, daemon=True).start()

    def _cached_cover_is_valid(self, comic_id):
        cache_path = self._cover_cache_dir / f'{comic_id}.jpg'
        if not cache_path.exists() or cache_path.stat().st_size == 0:
            return False
        try:
            with Image.open(cache_path) as cached:
                cached.verify()
            return True
        except Exception:
            cache_path.unlink(missing_ok=True)
            return False

    def _load_cached_cover(self, comic_id):
        if not self._cached_cover_is_valid(comic_id):
            return None
        cache_path = self._cover_cache_dir / f'{comic_id}.jpg'
        try:
            with Image.open(cache_path) as cached:
                return self._format_weekly_cover(cached)
        except Exception:
            cache_path.unlink(missing_ok=True)
            return None

    def _try_load_cover(self, comic_id, config, proxy=None, gen=None):
        cached = self._load_cached_cover(comic_id)
        if cached is not None:
            return cached
        with self._cover_lock(comic_id):
            if gen is not None and gen != getattr(self, '_load_gen', 0):
                return None
            cached = self._load_cached_cover(comic_id)
            if cached is not None:
                return cached
            cache_path = self._cover_cache_dir / f'{comic_id}.jpg'
            domains = list(getattr(self, '_cover_domains', []))
            if not domains:
                domains = list(getattr(config, 'DOMAIN_IMAGE_LIST', []))
            if not domains:
                domains = ['cdn-msp.jmapiproxy1.cc', 'cdn-msp3.jmapiproxy2.cc',
                           'cdn-msp.jmapinodeudzn.net']
            preferred = self._cover_urls.get(comic_id)
            if preferred in domains:
                domains.remove(preferred)
                domains.insert(0, preferred)
            for domain in domains[:12]:
                for suffix in ('_3x4.jpg', '.jpg'):
                    for active_proxy in ([proxy, None] if proxy else [None]):
                        url = f'https://{domain}/media/albums/{comic_id}{suffix}'
                        image = self._fetch_cover_url(url, active_proxy)
                        if image is None:
                            continue
                        if gen is not None and gen != getattr(self, '_load_gen', 0):
                            return None
                        temp_path = cache_path.with_name(cache_path.stem + '.part.jpg')
                        try:
                            image.save(temp_path, 'JPEG', quality=88)
                            with Image.open(temp_path) as saved_image:
                                saved_image.verify()
                            temp_path.replace(cache_path)
                            if gen is not None and gen != getattr(self, '_load_gen', 0):
                                cache_path.unlink(missing_ok=True)
                                return None
                        finally:
                            temp_path.unlink(missing_ok=True)
                        self._cover_urls[comic_id] = domain
                        return self._format_weekly_cover(image)
            return None

    @staticmethod
    def _format_weekly_cover(image):
        image = image.convert('RGB')
        target_w, target_h = 204, 270
        ratio = max(target_w / image.width, target_h / image.height)
        resized = image.resize((max(1, int(image.width * ratio)),
                                max(1, int(image.height * ratio))), Image.Resampling.LANCZOS)
        left = max(0, (resized.width - target_w) // 2)
        top = max(0, (resized.height - target_h) // 2)
        return resized.crop((left, top, left + target_w, top + target_h))

    @staticmethod
    def _fetch_cover_url(url, proxy=None):
        headers = {
            'User-Agent': 'Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/124.0 Mobile Safari/537.36',
            'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
            'Referer': 'https://18comic.vip/',
            'X-Requested-With': 'com.JMComic3.app',
        }
        proxies = {'http': proxy, 'https': proxy} if proxy else None
        try:
            resp = requests.get(url, headers=headers, proxies=proxies, timeout=(5, 15))
            content_type = resp.headers.get('Content-Type', '').lower()
            if resp.status_code != 200 or not resp.content or \
                    (content_type and not content_type.startswith('image/')) or \
                    len(resp.content) > 8 * 1024 * 1024:
                return None
            image = Image.open(io.BytesIO(resp.content))
            image.load()
            return image.convert('RGB')
        except Exception:
            return None

    def _refresh_card_statuses(self, items, gen, output_dir):
        cache = self.gui.jm_tab._get_downloaded_cache(output_dir)
        for item in items:
            entry = cache.get(item['cid'], {})
            status = entry.get('status', 'none')
            if status == 'complete':
                text, color = '已完整下载', '#22863a'
            elif status == 'partial':
                text, color = f'缺失 {entry.get("missing", 0)} 项', '#b35c00'
            else:
                text, color = '未下载', '#777777'
            self.gui.post(self._apply_card_status, item, text, color, gen)

    def _apply_card_status(self, item, text, color, gen):
        if gen != getattr(self, '_load_gen', 0):
            return
        try:
            item['status'].configure(text=text, fg=color)
        except tk.TclError:
            pass

    def _apply_thumb(self, item, image, gen):
        if gen != getattr(self, '_load_gen', 0):
            return
        if item.get('loaded'):
            return
        if image is None:
            try:
                item['thumb'].configure(text='封面暂不可用', image='', bg='#eeeeee', fg='#777777')
            except tk.TclError:
                pass
            return
        photo = ImageTk.PhotoImage(image)
        try:
            item['thumb'].configure(image=photo, text='', bg='white')
        except tk.TclError:
            return
        item['loaded'] = True
        self._photos.append(photo)

    def _confirm_download(self, comic_id, name):
        if messagebox.askyesno('确认下载', f'是否下载《{name}》(ID:{comic_id})？'):
            self._download_comic(comic_id)

    def _download_comic(self, comic_id):
        self.gui.notebook.select(1)
        self.gui.jm_tab.jm_id_text.delete('1.0', tk.END)
        self.gui.jm_tab.jm_id_text.insert('1.0', str(comic_id))
        self.gui.jm_tab._start_download()


# ==================== 下载历史 ====================
class HistoryPanel(ttk.Frame):
    STATUS_TEXT = {
        'complete': '完整', 'partial': '缺失', 'failed': '失败', 'cancelled': '已停止',
    }

    def __init__(self, parent, gui):
        super().__init__(parent, padding=8)
        self.gui = gui
        self._records = []
        self.setup_ui()
        self.refresh()

    def setup_ui(self):
        toolbar = ttk.Frame(self)
        toolbar.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(toolbar, text='下载历史', font=('Microsoft YaHei UI', 12, 'bold')).pack(side=tk.LEFT)
        self.filter_var = tk.StringVar(value='全部')
        ttk.Combobox(toolbar, textvariable=self.filter_var,
                     values=['全部', 'NHentai', 'JM Comic', '失败/缺失'],
                     state='readonly', width=11).pack(side=tk.LEFT, padx=12)
        self.filter_var.trace_add('write', lambda *_: self.refresh())
        ttk.Button(toolbar, text='刷新', command=self.refresh).pack(side=tk.RIGHT)
        ttk.Button(toolbar, text='打开目录', command=self.open_selected).pack(side=tk.RIGHT, padx=6)
        ttk.Button(toolbar, text='重试所选', command=self.retry_selected).pack(side=tk.RIGHT)

        columns = ('time', 'site', 'id', 'title', 'status', 'progress', 'reason')
        self.tree = ttk.Treeview(self, columns=columns, show='headings', selectmode='browse')
        headings = {'time': '时间', 'site': '来源', 'id': 'ID', 'title': '标题',
                    'status': '结果', 'progress': '完整度', 'reason': '失败归因'}
        widths = {'time': 145, 'site': 85, 'id': 90, 'title': 310,
                  'status': 70, 'progress': 85, 'reason': 360}
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], minwidth=60,
                             anchor=tk.CENTER if col in ('site', 'id', 'status', 'progress') else tk.W)
        scroll = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.tag_configure('complete', foreground='#16733c')
        self.tree.tag_configure('partial', foreground='#a14d00')
        self.tree.tag_configure('failed', foreground='#b42318')
        self.tree.bind('<Double-Button-1>', lambda _e: self.open_selected())

    def refresh(self):
        if not hasattr(self, 'tree'):
            return
        self.tree.delete(*self.tree.get_children())
        records = list(reversed(load_app_state().get('history', [])))
        selected_filter = self.filter_var.get()
        if selected_filter in ('NHentai', 'JM Comic'):
            records = [r for r in records if r.get('site') == selected_filter]
        elif selected_filter == '失败/缺失':
            records = [r for r in records if r.get('status') in ('failed', 'partial')]
        self._records = records
        for idx, record in enumerate(records):
            failure = record.get('failure') or {}
            total, missing = record.get('total', 0), record.get('missing', 0)
            progress = f'{max(0, total - missing)}/{total}' if total else '-'
            reason = failure.get('reason', '')
            detail = failure.get('detail', '')
            if detail and detail not in reason:
                reason = f'{reason}: {detail}'
            status = record.get('status', 'failed')
            self.tree.insert('', tk.END, iid=str(idx), tags=(status,), values=(
                record.get('time', ''), record.get('site', ''), record.get('id', ''),
                record.get('title', ''), self.STATUS_TEXT.get(status, status), progress, reason[:180]))

    def _selected_record(self):
        selection = self.tree.selection()
        if not selection:
            return None
        try:
            return self._records[int(selection[0])]
        except (ValueError, IndexError):
            return None

    def open_selected(self):
        record = self._selected_record()
        if not record:
            return
        path = Path(record.get('path') or '')
        if path.is_file():
            path = path.parent
        if path.exists():
            os.startfile(str(path))
        else:
            messagebox.showinfo('提示', '记录中的目录已移动或不存在')

    def retry_selected(self):
        record = self._selected_record()
        if not record:
            return
        item_id = record.get('id', '')
        if record.get('site') == 'NHentai':
            self.gui.notebook.select(self.gui.nhentai_tab)
            self.gui.nhentai_tab.input_text.delete('1.0', tk.END)
            self.gui.nhentai_tab.input_text.insert('1.0', item_id)
            self.gui.nhentai_tab.start_download()
        else:
            self.gui.notebook.select(self.gui.jm_tab)
            self.gui.jm_tab.jm_id_text.delete('1.0', tk.END)
            self.gui.jm_tab.jm_id_text.insert('1.0', item_id)
            self.gui.jm_tab._start_download()


# ==================== 配置档案 ====================
class ProfilePanel(ttk.Frame):
    def __init__(self, parent, gui):
        super().__init__(parent, padding=14)
        self.gui = gui
        self.setup_ui()
        self.refresh_profiles()

    def setup_ui(self):
        ttk.Label(self, text='配置档案', font=('Microsoft YaHei UI', 12, 'bold')).pack(anchor=tk.W)
        ttk.Label(self, text='保存目录、代理、线程和重试策略；登录密码不会写入档案。',
                  foreground='#666666').pack(anchor=tk.W, pady=(2, 12))

        manage = ttk.LabelFrame(self, text='档案管理', padding=10)
        manage.pack(fill=tk.X)
        self.name_var = tk.StringVar()
        self.combo = ttk.Combobox(manage, textvariable=self.name_var, width=30, state='normal')
        self.combo.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(manage, text='应用', command=self.apply_profile).pack(side=tk.LEFT, padx=3)
        ttk.Button(manage, text='保存当前配置', command=self.save_profile).pack(side=tk.LEFT, padx=3)
        ttk.Button(manage, text='删除', command=self.delete_profile).pack(side=tk.LEFT, padx=3)

        preview = ttk.LabelFrame(self, text='当前档案内容', padding=8)
        preview.pack(fill=tk.BOTH, expand=True, pady=(12, 0))
        self.preview = scrolledtext.ScrolledText(preview, state=tk.DISABLED, height=20,
                                                 font=('Consolas', 10), wrap=tk.WORD)
        self.preview.pack(fill=tk.BOTH, expand=True)
        self.combo.bind('<<ComboboxSelected>>', lambda _e: self.show_preview())

    def _current_config(self):
        nh, jm = self.gui.nhentai_tab, self.gui.jm_tab
        return {
            'nh': {'proxy': self.gui.nh_proxy_var.get(), 'output': self.gui.nh_output_var.get(),
                   'quality': self.gui.nh_quality_var.get(), 'retry': self.gui.nh_retry_var.get(),
                   'workers': self.gui.nh_workers_var.get(), 'speed': self.gui.nh_speed_mode_var.get(),
                   'stealth': self.gui.nh_stealth_var.get(),
                   'browser_priority': self.gui.nh_browser_priority_var.get()},
            'jm': {'proxy': jm.jm_proxy_var.get(), 'output': jm.jm_dir_var.get(),
                   'mode': jm.jm_mode_var.get(), 'image_threads': jm.jm_image_threads_var.get(),
                   'photo_threads': jm.jm_photo_threads_var.get(), 'retry': jm.jm_retry_var.get(),
                   'delay': jm.jm_delay_var.get(), 'antiblock': jm.jm_antiblock_var.get(),
                   'browser_priority': jm.jm_browser_priority_var.get()},
        }

    def refresh_profiles(self):
        state = load_app_state()
        names = sorted(state.get('profiles', {}))
        self.combo['values'] = names
        active = state.get('active_profile', '')
        if active in names:
            self.name_var.set(active)
        elif names and not self.name_var.get():
            self.name_var.set(names[0])
        self.show_preview()

    def save_profile(self):
        name = self.name_var.get().strip()
        if not name:
            messagebox.showwarning('提示', '请输入档案名称')
            return
        config = self._current_config()
        def mutate(state):
            state['profiles'][name] = config
            state['active_profile'] = name
        update_app_state(mutate)
        self.refresh_profiles()
        self.gui.status_var.set(f'已保存配置档案: {name}')

    def apply_profile(self):
        name = self.name_var.get().strip()
        state = load_app_state()
        profile = state.get('profiles', {}).get(name)
        if not profile:
            messagebox.showwarning('提示', '请选择一个已有档案')
            return
        nh, jm = profile.get('nh', {}), profile.get('jm', {})
        mappings = [
            (self.gui.nh_proxy_var, nh.get('proxy')), (self.gui.nh_output_var, nh.get('output')),
            (self.gui.nh_quality_var, nh.get('quality')), (self.gui.nh_retry_var, nh.get('retry')),
            (self.gui.nh_workers_var, nh.get('workers')), (self.gui.nh_speed_mode_var, nh.get('speed')),
            (self.gui.nh_stealth_var, nh.get('stealth')),
            (self.gui.nh_browser_priority_var, nh.get('browser_priority')),
            (self.gui.jm_tab.jm_proxy_var, jm.get('proxy')),
            (self.gui.jm_tab.jm_dir_var, jm.get('output')), (self.gui.jm_tab.jm_mode_var, jm.get('mode')),
            (self.gui.jm_tab.jm_image_threads_var, jm.get('image_threads')),
            (self.gui.jm_tab.jm_photo_threads_var, jm.get('photo_threads')),
            (self.gui.jm_tab.jm_retry_var, jm.get('retry')), (self.gui.jm_tab.jm_delay_var, jm.get('delay')),
            (self.gui.jm_tab.jm_antiblock_var, jm.get('antiblock')),
            (self.gui.jm_tab.jm_browser_priority_var, jm.get('browser_priority')),
        ]
        for var, value in mappings:
            if value is not None:
                var.set(value)
        update_app_state(lambda current: current.update(active_profile=name))
        self.gui.nhentai_tab.refresh_covers()
        self.gui.refresh_collection()
        self.gui.status_var.set(f'已应用配置档案: {name}')

    def apply_active_profile(self):
        state = load_app_state()
        name = state['active_profile']
        if name not in state['profiles']:
            return
        self.name_var.set(name)
        self.apply_profile()

    def delete_profile(self):
        name = self.name_var.get().strip()
        state = load_app_state()
        if name not in state['profiles']:
            return
        def mutate(current):
            current['profiles'].pop(name, None)
            if current['active_profile'] == name:
                current['active_profile'] = ''
        update_app_state(mutate)
        self.name_var.set('')
        self.refresh_profiles()

    def show_preview(self):
        state = load_app_state()
        profile = state.get('profiles', {}).get(self.name_var.get().strip(), {})
        self.preview.configure(state=tk.NORMAL)
        self.preview.delete('1.0', tk.END)
        if profile:
            self.preview.insert('1.0', json.dumps(profile, ensure_ascii=False, indent=2))
        else:
            self.preview.insert('1.0', '输入名称后点击“保存当前配置”创建档案。')
        self.preview.configure(state=tk.DISABLED)


# ==================== 统一主GUI ====================
class UnifiedGUI:
    def __init__(self, root):
        self.root = root
        self.root.title('JM & NHentai 统一下载器')
        self.root.geometry('1360x900')
        self.root.minsize(1024, 720)

        self.capsule = None
        self._cf_monitor_id = None
        self._closing = False
        self._ui_queue = queue.Queue()

        self.setup_ui()
        self.profile_tab.apply_active_profile()
        self.root.protocol('WM_DELETE_WINDOW', self.on_close)
        self._ui_pump_id = self.root.after(30, self._drain_ui_queue)
        self.start_cf_monitor()

    def post(self, func, *args, **kwargs):
        if not self._closing:
            self._ui_queue.put((0, func, args, kwargs))

    def post_after(self, delay_ms, func, *args, **kwargs):
        if not self._closing:
            self._ui_queue.put((delay_ms, func, args, kwargs))

    def _drain_ui_queue(self):
        if self._closing:
            return
        while True:
            try:
                delay, func, args, kwargs = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                if delay:
                    self.root.after(delay, lambda f=func, a=args, k=kwargs: f(*a, **k))
                else:
                    func(*args, **kwargs)
            except tk.TclError:
                pass
        self._ui_pump_id = self.root.after(30, self._drain_ui_queue)

    def on_close(self):
        if self._closing:
            return
        self._closing = True
        if self.nhentai_tab.crawler:
            self.nhentai_tab.crawler.stop()
        self.jm_tab._jm_stop_event.set()
        if self.capsule:
            self.capsule.on_close()
        for after_id in (self._cf_monitor_id, getattr(self, '_ui_pump_id', None)):
            if after_id:
                try:
                    self.root.after_cancel(after_id)
                except tk.TclError:
                    pass
        self.root.destroy()

    def setup_ui(self):
        style = ttk.Style(self.root)
        if 'vista' in style.theme_names():
            style.theme_use('vista')
        style.configure('TNotebook.Tab', padding=(16, 8), font=('Microsoft YaHei UI', 9))
        style.configure('TLabelframe.Label', font=('Microsoft YaHei UI', 9, 'bold'))
        style.configure('Accent.TButton', font=('Microsoft YaHei UI', 9, 'bold'))
        # Notebook 标签栏
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        # NHentai 标签页
        self.nhentai_tab = NHentaiPanel(self.notebook, self)
        self.notebook.add(self.nhentai_tab, text='  NHentai  ')

        # JM Comic 标签页
        self.jm_tab = JMComicPanel(self.notebook, self)
        self.notebook.add(self.jm_tab, text='  JM Comic  ')

        # 每周必看标签页
        self.weekly_tab = WeeklyPanel(self.notebook, self)
        self.notebook.add(self.weekly_tab, text='  每周必看  ')

        # 合集标签页
        self.collection_tab = JMCollectionPanel(self.notebook, self)
        self.notebook.add(self.collection_tab, text='  合集  ')

        self.history_tab = HistoryPanel(self.notebook, self)
        self.notebook.add(self.history_tab, text='  下载历史  ')

        self.profile_tab = ProfilePanel(self.notebook, self)
        self.notebook.add(self.profile_tab, text='  配置档案  ')

        # 底部状态栏
        status_frame = ttk.Frame(self.root)
        status_frame.pack(fill=tk.X, padx=5, pady=(0, 3))

        self.status_var = tk.StringVar(value='就绪')
        ttk.Label(status_frame, textvariable=self.status_var,
                  foreground='gray', font=('Microsoft YaHei UI', 8)).pack(side=tk.LEFT)

        ttk.Label(status_frame, text=f'  |  合集: {get_collection_desc()}',
                  foreground='#888888', font=('Microsoft YaHei UI', 8)).pack(side=tk.LEFT)

    def log(self, msg, tag='info'):
        if hasattr(self, 'nhentai_tab') and hasattr(self.nhentai_tab, 'log_text'):
            self.nhentai_tab.log_text.configure(state=tk.NORMAL)
            timestamp = datetime.now().strftime('%H:%M:%S')
            self.nhentai_tab.log_text.insert(tk.END, f'[{timestamp}] {msg}\n', tag)
            self.nhentai_tab.log_text.see(tk.END)
            self.nhentai_tab.log_text.configure(state=tk.DISABLED)

    def clear_log(self):
        if hasattr(self, 'nhentai_tab') and hasattr(self.nhentai_tab, 'log_text'):
            self.nhentai_tab.log_text.configure(state=tk.NORMAL)
            self.nhentai_tab.log_text.delete(1.0, tk.END)
            self.nhentai_tab.log_text.configure(state=tk.DISABLED)

    def refresh_collection(self):
        if hasattr(self, 'collection_tab'):
            self.collection_tab.build_collection()

    def refresh_history(self):
        if hasattr(self, 'history_tab'):
            self.history_tab.refresh()

    def start_cf_monitor(self):
        def update():
            if hasattr(self, 'nhentai_tab') and self.nhentai_tab.crawler and \
               self.nhentai_tab.is_downloading:
                if self.nhentai_tab.crawler.cloudflare_hits > 0:
                    color = 'orange' if self.nhentai_tab.crawler.cloudflare_hits < 3 else 'red'
                else:
                    color = 'green'
                self.cf_status_label.config(foreground=color)
                self.cf_text_label.config(text=f'CF:{self.nhentai_tab.crawler.cloudflare_hits}')
            self._cf_monitor_id = self.root.after(2000, update)
        self._cf_monitor_id = self.root.after(2000, update)

    def toggle_capsule(self):
        if self.capsule:
            try:
                if self.capsule.win.winfo_exists():
                    self.capsule.on_close()
            except tk.TclError:
                self.capsule = None
                try:
                    self.capsule_btn.config(text='开启胶囊')
                except tk.TclError:
                    pass
        else:
            self.capsule = FloatingCapsule(self)
            try:
                self.capsule_btn.config(text='关闭胶囊')
            except tk.TclError:
                pass


def main():
    root = tk.Tk()
    app = UnifiedGUI(root)

    root.after(500, app.refresh_collection)

    root.mainloop()


if __name__ == '__main__':
    main()
