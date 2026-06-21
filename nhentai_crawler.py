#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nhentai.net 图片爬虫 - Scrapling 增强版
支持VPN/代理，自动下载图片并打包成ZIP
增强特性：StealthyFetcher 反爬、自适应解析、浏览器 TLS 指纹伪装
"""

import os
import re
import sys
import time
import json
import zipfile
import random
import logging
import argparse
from pathlib import Path
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# Scrapling 导入尝试
SCRAPLING_AVAILABLE = False
StealthyFetcher = None
StealthySession = None
Fetcher = None
FetcherSession = None
DynamicFetcher = None
Page = None

try:
    from scrapling.fetchers import StealthyFetcher, StealthySession, Fetcher, FetcherSession, DynamicFetcher
    from scrapling.core import Page
    SCRAPLING_AVAILABLE = True
    logging.getLogger(__name__).info("Scrapling 已加载，启用增强反爬模式")
except ImportError:
    # 降级到 requests
    import requests
    from bs4 import BeautifulSoup
    logging.getLogger(__name__).warning(
        "Scrapling 未安装，将使用 requests 模式。建议: pip install scrapling"
    )

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 默认配置
DEFAULT_CONFIG = {
    'output_dir': './downloads',
    'max_workers': 3,
    'retry_times': 3,
    'timeout': 30,
    'delay_range': (1, 3),
    'image_quality': 'low',
    'stealth_mode': True,
    'use_browser_fallback': True,
    'solve_cloudflare': True,
    'max_browser_rounds': 2,
}


class NHentaiCrawler:
    """nhentai 爬虫核心类 - Scrapling 增强版"""

    def __init__(self, proxy=None, config=None):
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self.proxy = proxy
        self.session = None
        self.stealth_session = None  # StealthySession 实例
        self.browser_session = None  # DynamicSession 实例
        self._session_mode = "requests"  # requests / stealth / fetcher
        self.use_scrapling = SCRAPLING_AVAILABLE and self.config.get('stealth_mode', True)
        self._browser_required_cache = {}
        self._lock = threading.Lock()  # 用于线程安全的结果收集

        self.setup_session()

    def setup_session(self):
        """配置会话，优先使用 Scrapling StealthySession"""
        if not self.use_scrapling:
            self._setup_requests_session()
            return

        # 1. 尝试创建 StealthySession（隐身模式，可绕过 Cloudflare）
        try:
            kwargs = {
                'headless': True,
                'solve_cloudflare': self.config.get('solve_cloudflare', True),
                'timeout': self.config['timeout'],
            }
            if self.proxy:
                kwargs['proxy'] = self.proxy

            self.stealth_session = StealthySession(**kwargs)
            self._session_mode = "stealth"
            logger.info("启用 StealthySession 隐身模式 (Cloudflare 自动绕过)")
            return
        except Exception as e:
            logger.warning(f"StealthySession 创建失败: {e}，尝试降级到 Fetcher 模式")

        # 2. 降级到基础 FetcherSession（TLS 指纹伪装）
        try:
            if self.proxy:
                self.session = FetcherSession(proxy=self.proxy, timeout=self.config['timeout'])
            else:
                self.session = FetcherSession(timeout=self.config['timeout'])
            self._session_mode = "fetcher"
            logger.info("启用 FetcherSession TLS 指纹伪装模式")
            return
        except Exception as e:
            logger.warning(f"FetcherSession 创建失败: {e}，降级到 requests")

        # 3. 最终降级
        self._setup_requests_session()

    def _setup_requests_session(self):
        """原始 requests 会话（降级模式）"""
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Referer': 'https://nhentai.net/',
        })

        if self.proxy:
            self.session.proxies = {
                'http': self.proxy,
                'https': self.proxy,
            }
        self._session_mode = "requests"
        logger.info("使用 requests 标准模式（无隐身保护）")

    def get_page(self, url, retry=None, force_browser=False):
        """
        获取页面内容，带重试机制和智能模式切换
        """
        retry = retry or self.config['retry_times']

        # 检查是否需要浏览器模式（历史记录）
        if self.use_scrapling and self.config.get('use_browser_fallback'):
            if url in self._browser_required_cache:
                force_browser = self._browser_required_cache[url]

        for attempt in range(retry):
            try:
                if force_browser and self.use_scrapling and DynamicFetcher is not None:
                    return self._fetch_with_browser(url, attempt)
                elif self._session_mode == "stealth" and self.stealth_session:
                    return self._fetch_with_stealth(url, attempt)
                elif self._session_mode == "fetcher" and self.session:
                    return self._fetch_with_fetcher(url, attempt)
                else:
                    return self._fetch_with_requests(url, attempt)

            except Exception as e:
                error_msg = str(e).lower()
                # 如果检测到 Cloudflare 相关拦截，记录并尝试切换浏览器模式
                if self.use_scrapling and self.config.get('use_browser_fallback') and \
                   any(keyword in error_msg for keyword in ['cloudflare', 'captcha', 'turnstile', 'cf-ray']):
                    self._browser_required_cache[url] = True
                    logger.warning(f'检测到 Cloudflare 防护，切换浏览器模式 (尝试 {attempt+1}/{retry})')
                    time.sleep(random.uniform(2, 5))
                else:
                    logger.warning(f'请求失败 (尝试 {attempt+1}/{retry}): {e}')
                if attempt < retry - 1:
                    time.sleep(random.uniform(1, 3))

        return None

    def _fetch_with_stealth(self, url, attempt):
        page = self.stealth_session.fetch(url)
        return self._adapt_page_response(page, url)

    def _fetch_with_fetcher(self, url, attempt):
        """FetcherSession 获取页面"""
        page = self.session.get(url)
        return self._adapt_page_response(page, url)

    def _fetch_with_browser(self, url, attempt):
        """完整浏览器模式（DynamicFetcher）"""
        if DynamicFetcher is None:
            raise ImportError("DynamicFetcher 不可用")
        page = DynamicFetcher.fetch(
            url,
            headless=True,
            wait_until='networkidle',
            timeout=self.config['timeout'] * 1000  # Scrapling 内部使用毫秒
        )
        return self._adapt_page_response(page, url)

    def _fetch_with_requests(self, url, attempt):
        """原始 requests 获取"""
        response = self.session.get(url, timeout=self.config['timeout'])
        response.raise_for_status()
        return response

    def _adapt_page_response(self, scrapling_page, url):
        """将 Scrapling Page 适配为兼容原有代码的 Response 对象"""
        class AdaptedResponse:
            def __init__(self, page, url):
                self.page = page
                self.url = url
                self.status_code = 200
                # Scrapling Page 通常有 .html 属性
                try:
                    self.text = page.html if hasattr(page, 'html') else str(page)
                except Exception:
                    self.text = ''

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise Exception(f"HTTP {self.status_code}")

        # 如果直接得到了字符串
        if isinstance(scrapling_page, str):
            class StringResponse:
                def __init__(self, content, url):
                    self.text = content
                    self.url = url
                    self.status_code = 200
                def raise_for_status(self):
                    pass
            return StringResponse(scrapling_page, url)

        return AdaptedResponse(scrapling_page, url)

    def get_gallery_info(self, gallery_id):
        """获取画廊信息和图片URL列表"""
        url = f'https://nhentai.net/g/{gallery_id}/'
        logger.info(f'获取画廊信息: {url}')

        response = self.get_page(url)
        if not response:
            logger.error(f'无法访问画廊: {gallery_id}')
            return None

        soup = self._parse_html(response)

        title_elem = soup.find('h1', class_='title')
        title = title_elem.get_text(strip=True) if title_elem else f'gallery_{gallery_id}'
        title = re.sub(r'[<>:"/\\|?*]', '_', title)[:100]

        # 缩略图
        thumb_divs = soup.select('.thumb-container img')
        num_pages = len(thumb_divs)

        if num_pages == 0:
            logger.error('未找到图片')
            return None

        # 提取 media_id（增强版正则）
        media_id = None
        scripts = soup.find_all('script')
        for script in scripts:
            if script.string and 'media_id' in script.string:
                for pattern in [
                    r'media_id["\s:=]+["\']?(\d+)',
                    r'"media_id":"(\d+)"',
                    r'media_id\s*=\s*(\d+)',
                ]:
                    match = re.search(pattern, script.string)
                    if match:
                        media_id = match.group(1)
                        break
                if media_id:
                    break

        if not media_id and thumb_divs:
            first_src = thumb_divs[0].get('data-src', '') or thumb_divs[0].get('src', '')
            match = re.search(r'/galleries/(\d+)/', first_src) or \
                    re.search(r'/(\d+)/\d+t?\.\w+', first_src)
            if match:
                media_id = match.group(1)

        if not media_id:
            logger.error('无法提取 media_id')
            return None

        logger.info(f'标题: {title}')
        logger.info(f'页数: {num_pages}')
        logger.info(f'Media ID: {media_id}')

        # 构建图片 URL 列表
        image_urls = []
        for i in range(1, num_pages + 1):
            ext = 'jpg'
            for thumb in thumb_divs:
                src = thumb.get('data-src', '') or thumb.get('src', '')
                if f'/{i}.' in src or f'/{i}t.' in src:
                    if '.png' in src:
                        ext = 'png'
                    elif '.webp' in src:
                        ext = 'webp'
                    break

            if self.config['image_quality'] == 'high':
                image_url = f'https://i.nhentai.net/galleries/{media_id}/{i}.{ext}'
            else:
                image_url = f'https://i.nhentai.net/galleries/{media_id}/{i}t.{ext}'

            image_urls.append((i, image_url))

        return {
            'id': gallery_id,
            'title': title,
            'media_id': media_id,
            'num_pages': num_pages,
            'image_urls': image_urls
        }

    def _parse_html(self, response):
        from bs4 import BeautifulSoup
        return BeautifulSoup(response.text, 'html.parser')

    def download_image(self, url, save_path, retry=None):
        """下载单张图片（始终使用轻量 requests 流式下载）"""
        retry = retry or self.config['retry_times']

        for attempt in range(retry):
            try:
                import requests as req
                with req.Session() as dl_session:
                    dl_session.headers.update({
                        'User-Agent': self._get_user_agent(),
                        'Referer': 'https://nhentai.net/',
                    })
                    if self.proxy:
                        dl_session.proxies = {'http': self.proxy, 'https': self.proxy}
                    response = dl_session.get(url, timeout=self.config['timeout'], stream=True)
                    response.raise_for_status()
                    with open(save_path, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            f.write(chunk)
                return True
            except Exception as e:
                logger.warning(f'下载失败 (尝试 {attempt+1}/{retry}): {e}')
                if attempt < retry - 1:
                    time.sleep(random.uniform(1, 2))
        return False

    def _get_user_agent(self):
        ua_list = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        ]
        return random.choice(ua_list)

    def download_gallery(self, gallery_id):
        """下载整个画廊并打包成ZIP（多线程并发）"""
        info = self.get_gallery_info(gallery_id)
        if not info:
            return False

        # 创建临时目录
        temp_dir = Path(self.config['output_dir']) / 'temp' / str(gallery_id)
        temp_dir.mkdir(parents=True, exist_ok=True)

        # 准备下载任务
        logger.info(f'开始并发下载 {info["num_pages"]} 张图片 (workers={self.config["max_workers"]})...')
        tasks = []
        for page_num, image_url in info['image_urls']:
            ext = image_url.split('.')[-1]
            image_path = temp_dir / f'{page_num:04d}.{ext}'
            if image_path.exists():
                logger.info(f'图片已存在: {page_num}/{info["num_pages"]}')
                with self._lock:
                    tasks.append((page_num, image_path, True))  # 标记为已存在
            else:
                tasks.append((page_num, image_path, False))

        # 并发下载（仅处理不存在的图片）
        download_tasks = [(idx, url, path) for idx, path, exists in tasks if not exists]
        if not download_tasks:
            logger.info('所有图片已存在，无需下载')
        else:
            with ThreadPoolExecutor(max_workers=self.config['max_workers']) as executor:
                future_map = {}
                task_dict = {str(p): (i, (page_num, p, exists)) for i, (page_num, p, exists) in enumerate(tasks)}
                for idx, url, path in download_tasks:
                    future = executor.submit(self.download_image, url, path)
                    future_map[future] = (idx, path)

                for future in as_completed(future_map):
                    idx, path = future_map[future]
                    success = future.result()
                    with self._lock:
                        entry = task_dict.get(str(path))
                        if entry:
                            i, (page_num, p, _) = entry
                            tasks[i] = (page_num, p, success)
                    if success:
                        logger.info(f'下载成功: {idx}/{info["num_pages"]}')
                    else:
                        logger.error(f'下载失败: 第{idx}页')

        # 收集成功下载的图片
        downloaded = [path for _, path, success in tasks if success]
        if not downloaded:
            logger.error('没有成功下载任何图片')
            return False

        # 打包成ZIP
        zip_name = f'{gallery_id}_{info["title"]}.zip'
        zip_path = Path(self.config['output_dir']) / zip_name

        logger.info(f'打包成ZIP: {zip_path}')
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for image_path in sorted(downloaded):
                zf.write(image_path, image_path.name)

        # 清理临时文件
        for image_path in downloaded:
            try:
                image_path.unlink()
            except OSError:
                pass
        try:
            temp_dir.rmdir()
        except OSError:
            pass

        logger.info(f'完成! ZIP文件: {zip_path}')
        logger.info(f'共下载 {len(downloaded)}/{info["num_pages"]} 张图片')
        return True

    def search_and_download(self, query, max_galleries=5):
        """搜索并下载"""
        url = f'https://nhentai.net/search/?q={query}'
        logger.info(f'搜索: {query}')

        response = self.get_page(url)
        if not response:
            logger.error('搜索失败')
            return []

        soup = self._parse_html(response)

        gallery_links = soup.select('.gallery a')
        if not gallery_links:
            logger.warning('未找到结果')
            return []

        gallery_ids = []
        for link in gallery_links[:max_galleries]:
            href = link.get('href', '')
            match = re.search(r'/g/(\d+)/', href)
            if match:
                gallery_ids.append(match.group(1))

        logger.info(f'找到 {len(gallery_ids)} 个画廊')
        results = []
        for gid in gallery_ids:
            success = self.download_gallery(gid)
            results.append((gid, success))
            time.sleep(random.uniform(2, 5))
        return results

    def __del__(self):
        if hasattr(self, 'stealth_session') and self.stealth_session:
            try:
                self.stealth_session.close()
            except Exception:
                logger.debug('stealth_session 关闭失败')
        if hasattr(self, 'browser_session') and self.browser_session:
            try:
                self.browser_session.close()
            except Exception:
                logger.debug('browser_session 关闭失败')


def main():
    parser = argparse.ArgumentParser(description='nhentai.net 图片爬虫 - Scrapling 增强版')
    parser.add_argument('target', nargs='?', help='画廊ID或搜索关键词')
    parser.add_argument('-p', '--proxy', help='代理地址')
    parser.add_argument('-o', '--output', default='./downloads', help='输出目录')
    parser.add_argument('-q', '--quality', choices=['low', 'high'], default='low', help='图片质量')
    parser.add_argument('-w', '--workers', type=int, default=3, help='并发下载数')
    parser.add_argument('-s', '--search', action='store_true', help='搜索模式')
    parser.add_argument('-n', '--number', type=int, default=5, help='搜索模式下最大下载数量')
    parser.add_argument('--no-stealth', dest='stealth', action='store_false', default=True, help='禁用隐身模式')
    parser.add_argument('--no-browser-fallback', action='store_true', help='禁用浏览器后备')

    args = parser.parse_args()

    if not args.target:
        parser.print_help()
        print('\n示例:')
        print('  python nhentai_crawler.py 123456 -p http://127.0.0.1:7890')
        print('  python nhentai_crawler.py "keyword" -s')
        print('\nScrapling 增强说明:')
        print('  - 自动模拟 Chrome TLS 指纹')
        print('  - Cloudflare Turnstile 自动绕过')
        print('  - 智能模式切换（遭遇防护时自动启用浏览器）')
        sys.exit(1)

    config = {
        'output_dir': args.output,
        'max_workers': args.workers,
        'image_quality': args.quality,
        'stealth_mode': args.stealth,
        'use_browser_fallback': not args.no_browser_fallback,
        'solve_cloudflare': True,
    }

    if not SCRAPLING_AVAILABLE and args.stealth:
        logger.warning("Scrapling 未安装，隐身模式不可用。运行: pip install scrapling")
        config['stealth_mode'] = False

    crawler = NHentaiCrawler(proxy=args.proxy, config=config)
    Path(args.output).mkdir(parents=True, exist_ok=True)

    if args.search:
        crawler.search_and_download(args.target, max_galleries=args.number)
    else:
        gallery_id = args.target.strip().rstrip('/')
        if gallery_id.isdigit():
            crawler.download_gallery(gallery_id)
        else:
            match = re.search(r'/g/(\d+)', args.target)
            if match:
                crawler.download_gallery(match.group(1))
            else:
                logger.error('无效的画廊ID或URL')
                sys.exit(1)


if __name__ == '__main__':
    main()