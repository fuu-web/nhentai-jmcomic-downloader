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
import csv
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

from nhentai_engine import (
    NHentaiCrawler, AntiCrawlManager,
    SCRAPLING_AVAILABLE, StealthySession, FetcherSession, DynamicFetcher,
)
from integrity import IntegrityVerifier, image_is_valid
from task_queue import PersistentTaskQueue, PENDING, RUNNING, DONE, FAILED, PAUSED

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


# ==================== Quiet Current visual system ====================
UI = {
    'bg': '#F2F4F8', 'surface': '#FBFCFE', 'surface_alt': '#E8ECF3',
    'surface_soft': '#F6F8FC', 'text': '#1C2028', 'muted': '#687180',
    'border': '#D7DDE8', 'primary': '#665CFF', 'primary_hover': '#5147E8',
    'primary_soft': '#E9E7FF', 'cyan': '#1FA7B8', 'cyan_soft': '#DFF5F7',
    'success': '#1F9D72', 'active': '#42D392', 'warning': '#D48636',
    'danger': '#D95462', 'log_bg': '#171A21', 'log_text': '#CDD5E3',
}
FONT_UI = ('Microsoft YaHei UI', 9)
FONT_SMALL = ('Microsoft YaHei UI', 8)
FONT_TITLE = ('Microsoft YaHei UI', 17, 'bold')
FONT_SECTION = ('Microsoft YaHei UI', 11, 'bold')
FONT_MONO = ('Cascadia Mono', 9)


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
        header = ttk.Frame(self, style='Toolbar.TFrame', padding=(18, 14))
        header.pack(fill=tk.X, padx=12, pady=(12, 6))

        ttk.Label(header, text='精选合集',
                  style='Toolbar.Section.TLabel').pack(side=tk.LEFT)
        self.stats_label = ttk.Label(header, text='', style='Toolbar.Muted.TLabel')
        self.stats_label.pack(side=tk.LEFT, padx=(4, 0))

        self.download_all_btn = ttk.Button(header, text='下载全部合集',
                                           command=self.download_all, width=12,
                                           style='Accent.TButton')
        self.download_all_btn.pack(side=tk.RIGHT, padx=4)
        self.refresh_btn = ttk.Button(header, text='刷新状态',
                                      command=self.build_collection, width=8)
        self.refresh_btn.pack(side=tk.RIGHT, padx=2)

        self.canvas = tk.Canvas(self, highlightthickness=0, bg=UI['bg'])
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

            card = tk.Frame(self.inner_frame, bg=UI['surface'], relief=tk.FLAT,
                            highlightthickness=1, highlightbackground=UI['border'],
                            width=214, height=354)
            card.grid(row=row, column=col, padx=7, pady=7, sticky='n')
            card.grid_propagate(False)

            cover_frame = tk.Frame(card, bg=UI['surface_alt'], width=196, height=258)
            cover_frame.pack(padx=8, pady=(8, 5))
            cover_frame.pack_propagate(False)
            thumb = tk.Label(cover_frame, text='正在整理封面...', bg=UI['surface_alt'],
                             fg=UI['muted'], font=FONT_SMALL)
            thumb.pack(fill=tk.BOTH, expand=True)

            display_title = title if title != gid else '---'
            tk.Label(card, text=display_title, bg=UI['surface'], anchor=tk.W, justify=tk.LEFT,
                     font=('Microsoft YaHei UI', 9, 'bold'), fg=UI['text'],
                     wraplength=194, height=2).pack(fill=tk.X, padx=9)

            meta = tk.Frame(card, bg=UI['surface'])
            meta.pack(fill=tk.X, padx=9, pady=(2, 0))
            tk.Label(meta, text=f'#{gid}', bg=UI['surface'], fg=UI['muted'],
                     font=('Consolas', 8)).pack(side=tk.LEFT)
            tk.Label(meta, text=status_text, bg=UI['surface'], fg=status_fg,
                     font=('Microsoft YaHei UI', 8, 'bold')).pack(side=tk.RIGHT)

            btn_frame = tk.Frame(card, bg=UI['surface'])
            btn_frame.pack(fill=tk.X, padx=8, pady=(5, 7))

            dl_btn = tk.Label(btn_frame, text='加入队列', bg=UI['primary'], fg='white',
                              font=FONT_SMALL, cursor='hand2', pady=4)
            dl_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)
            dl_btn.bind('<Button-1>', lambda e, gid=gid: self.download_single(gid))
            dl_btn.bind('<Enter>', lambda _e, w=dl_btn: w.config(bg=UI['primary_hover']))
            dl_btn.bind('<Leave>', lambda _e, w=dl_btn: w.config(bg=UI['primary']))

            if dir_path and dir_path.exists():
                open_btn = tk.Label(btn_frame, text='打开', bg=UI['surface_alt'], fg=UI['text'],
                                    font=FONT_SMALL, cursor='hand2',
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
        self.gui.notebook.select(self.gui.nhentai_tab)
        self.gui.nhentai_tab.input_text.delete(1.0, tk.END)
        self.gui.nhentai_tab.input_text.insert(1.0, gallery_id)
        self.gui.nhentai_tab.start_download()

    def download_all(self):
        collection_ids = load_collection_ids()
        if messagebox.askyesno('确认', f'将下载合集全部 {len(collection_ids)} 个画廊，确认？'):
            all_ids = '\n'.join(collection_ids)
            self.gui.notebook.select(self.gui.nhentai_tab)
            self.gui.nhentai_tab.input_text.delete(1.0, tk.END)
            self.gui.nhentai_tab.input_text.insert(1.0, all_ids)
            self.gui.nhentai_tab.start_download()


# ==================== 悬浮胶囊类 ====================
class FloatingCapsule:
    COMPACT_SIZE = (48, 48)
    EXPANDED_SIZE = (320, 300)
    EDGE_GAP = 6
    COMPACT_EDGE_INSET = 12

    def __init__(self, main_gui):
        self.main_gui = main_gui
        self.win = tk.Toplevel(main_gui.root)
        self.win.title('')
        self.win.overrideredirect(True)
        self.win.attributes('-topmost', True)
        self.win.attributes('-alpha', 0.97)
        saved = load_app_state().get('capsule', {})
        self.drag_data = {'x': 0, 'y': 0, 'moved': False}
        self._dock_side = saved.get('dock_side') if saved.get('dock_side') in ('left', 'right') else 'right'
        self._saved_y = int(saved.get('y', 120) or 120)
        self.listen_clipboard = bool(saved.get('listen_clipboard', True))
        self.expanded = False
        self.queue = []
        self.downloading = False
        self.current_gid = ''
        self._last_clipboard_text = ''
        self._stop_event = threading.Event()
        self._crawler = None
        self._clipboard_after_id = None
        self._status_after_id = None
        self._single_click_after_id = None
        self._paste_after_id = None
        self._ignore_next_release = False
        self._closed = False
        self.setup_ui()
        self.setup_position()
        self.setup_bindings()
        self.win.protocol("WM_DELETE_WINDOW", self.on_close)
        self.start_clipboard_check()
        self._status_after_id = self.win.after(500, self._refresh_main_queue_status)

    def setup_ui(self):
        panel_bg = '#171A21'
        panel_alt = '#222733'
        border = '#41495B'
        text = '#E5EAF2'
        muted = '#8F9AAA'
        primary = UI['primary']

        self.container = tk.Frame(self.win, bg=panel_bg, bd=0,
                                  highlightthickness=1, highlightbackground=UI['primary'])
        self.container.pack(fill=tk.BOTH, expand=True)
        self.icon_label = tk.Canvas(self.container, width=46, height=46, bg=panel_bg,
                                    highlightthickness=0, cursor='fleur')
        self.icon_label.pack(fill=tk.BOTH, expand=True)
        self._spider_color = primary
        self._draw_spider_icon(primary)

        self.expand_frame = tk.Frame(self.container, bg=panel_bg)
        panel_header = tk.Frame(self.expand_frame, bg=panel_bg)
        panel_header.pack(fill=tk.X, padx=10, pady=(8, 2))
        self.mode_label = tk.Label(panel_header, text='蜘蛛投递器', bg=panel_bg, fg=text,
                                   font=('Microsoft YaHei UI', 9, 'bold'), anchor=tk.W)
        self.mode_label.pack(side=tk.LEFT)
        self.status_label = tk.Label(panel_header, text='空闲', bg=panel_bg,
                                     fg=muted, font=('Microsoft YaHei UI', 7), anchor=tk.E)
        self.status_label.pack(side=tk.RIGHT)
        self.collapse_btn = tk.Label(panel_header, text='收起', bg=panel_alt, fg='#B8C0D0',
                                     font=('Microsoft YaHei UI', 7), cursor='hand2',
                                     padx=7, pady=2)
        self.collapse_btn.pack(side=tk.RIGHT, padx=(0, 8))
        self.collapse_btn.bind('<Button-1>', self.toggle_expand)

        monitor_row = tk.Frame(self.expand_frame, bg=panel_bg)
        monitor_row.pack(fill=tk.X, padx=10, pady=(3, 3))
        self.listen_var = tk.BooleanVar(value=self.listen_clipboard)
        self.listen_check = tk.Checkbutton(
            monitor_row, text='自动识别剪贴板', variable=self.listen_var,
            command=self._toggle_clipboard_listener, bg=panel_bg, fg='#B8C0D0',
            activebackground=panel_bg, activeforeground='white', selectcolor=panel_alt,
            font=('Microsoft YaHei UI', 7), relief=tk.FLAT, highlightthickness=0)
        self.listen_check.pack(side=tk.LEFT)
        self.queue_status_label = tk.Label(monitor_row, text='主队列 0', bg=panel_bg,
                                           fg=UI['cyan'], font=('Cascadia Mono', 7))
        self.queue_status_label.pack(side=tk.RIGHT)

        input_row = tk.Frame(self.expand_frame, bg=panel_bg)
        input_row.pack(fill=tk.X, padx=10, pady=(5, 6))
        tk.Label(input_row, text='快速投递', bg=panel_bg, fg='#B8C0D0',
                 font=('Microsoft YaHei UI', 8, 'bold')).pack(anchor=tk.W, pady=(0, 5))
        self.entry = tk.Entry(input_row, bg=panel_alt, fg=text, insertbackground=UI['active'],
                              font=('Cascadia Mono', 9), relief=tk.FLAT,
                              highlightthickness=1, highlightbackground=border,
                              highlightcolor=primary)
        self.entry.pack(fill=tk.X, ipady=6)
        self.entry.insert(0, '粘贴画廊ID或URL...')
        self.entry.config(fg=muted)
        self.entry.bind('<FocusIn>', self.on_focus_in)
        self.entry.bind('<FocusOut>', self.on_focus_out)
        self.entry.bind('<Return>', self.on_submit)
        self.entry.bind('<Control-v>', self.on_paste)
        self.entry.bind('<Button-3>', self.on_right_click)

        list_header = tk.Frame(self.expand_frame, bg=panel_bg)
        list_header.pack(fill=tk.X, padx=10)
        tk.Label(list_header, text='最近投递', bg=panel_bg, fg='#B8C0D0',
                 font=('Microsoft YaHei UI', 8, 'bold')).pack(side=tk.LEFT)
        tk.Label(list_header, text='任务由主队列执行', bg=panel_bg, fg=muted,
                 font=('Microsoft YaHei UI', 7)).pack(side=tk.RIGHT)
        list_frame = tk.Frame(self.expand_frame, bg=panel_bg)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(5, 8))
        self.queue_listbox = tk.Listbox(list_frame, bg=panel_alt, fg='#D1D8E4',
                                        font=('Cascadia Mono', 8), selectbackground=primary,
                                        selectforeground='white', relief=tk.FLAT, height=6,
                                        highlightthickness=1, highlightbackground=border,
                                        activestyle='none')
        scrollbar = tk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.queue_listbox.yview)
        self.queue_listbox.configure(yscrollcommand=scrollbar.set)
        self.queue_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        btn_row = tk.Frame(self.expand_frame, bg=panel_bg)
        btn_row.pack(fill=tk.X, padx=10, pady=(0, 10))
        self.clear_btn = tk.Label(btn_row, text='清空记录', bg=panel_alt, fg='#B8C0D0',
                                  font=('Microsoft YaHei UI', 7), cursor='hand2', padx=10, pady=5)
        self.clear_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.clear_btn.bind('<Button-1>', self.clear_queue)
        self.open_btn = tk.Label(btn_row, text='打开主窗口', bg=primary, fg='white',
                                 font=('Microsoft YaHei UI', 7, 'bold'), cursor='hand2',
                                 padx=11, pady=5)
        self.open_btn.pack(side=tk.RIGHT)
        self.open_btn.bind('<Button-1>', self.open_main_window)

    def _draw_spider_icon(self, color=None):
        canvas = self.icon_label
        canvas.delete('all')
        color = color or UI['primary']
        self._spider_color = color
        canvas.create_oval(2, 2, 44, 44, fill='#202532', outline=color, width=2)
        for points in (
                (17, 19, 10, 14, 5, 15), (16, 22, 8, 21, 4, 24),
                (17, 25, 10, 30, 5, 29), (29, 19, 36, 14, 41, 15),
                (30, 22, 38, 21, 42, 24), (29, 25, 36, 30, 41, 29)):
            canvas.create_line(*points, fill=color, width=2, smooth=True)
        canvas.create_oval(17, 11, 29, 23, fill=color, outline='')
        canvas.create_oval(15, 20, 31, 35, fill=color, outline='')
        canvas.create_oval(20, 15, 22, 17, fill='#171A21', outline='')
        canvas.create_oval(24, 15, 26, 17, fill='#171A21', outline='')

    def setup_position(self):
        self.win.update_idletasks()
        w, h = self.COMPACT_SIZE
        x = (-self.COMPACT_EDGE_INSET if self._dock_side == 'left'
             else self.win.winfo_screenwidth() - w + self.COMPACT_EDGE_INSET)
        y = max(self.EDGE_GAP, min(self._saved_y,
                                   self.win.winfo_screenheight() - h - self.EDGE_GAP - 40))
        self.win.geometry(f'{w}x{h}{int(x):+d}{int(y):+d}')

    def setup_bindings(self):
        self.icon_label.bind('<Button-1>', self.start_drag)
        self.icon_label.bind('<B1-Motion>', self.do_drag)
        self.icon_label.bind('<ButtonRelease-1>', self.stop_drag)
        self.icon_label.bind('<Double-Button-1>', self._on_spider_double_click)
        self.icon_label.bind('<Button-3>', self._show_context_menu)
        self.context_menu = tk.Menu(self.win, tearoff=False, bg='#222733', fg='#E5EAF2',
                                    activebackground=UI['primary'], activeforeground='white',
                                    relief=tk.FLAT, borderwidth=1)

    def start_clipboard_check(self):
        self._clipboard_after_id = self.win.after(2000, self.check_clipboard)

    def check_clipboard(self):
        if not self.win.winfo_exists():
            return
        if not self.listen_clipboard:
            self._clipboard_after_id = self.win.after(2000, self.check_clipboard)
            return
        try:
            text = self.win.clipboard_get()
            if text:
                if text == self._last_clipboard_text:
                    self._clipboard_after_id = self.win.after(2000, self.check_clipboard)
                    return
                self._last_clipboard_text = text
                ids = self.extract_ids(text)
                new_ids = list(ids)
                if new_ids:
                    for gid in new_ids:
                        self.add_to_queue(gid)
                    self.status_label.config(text=f'发现新任务 · {new_ids[0]}', fg='#67D4E0')
        except tk.TclError:
            pass
        self._clipboard_after_id = self.win.after(2000, self.check_clipboard)

    def on_close(self):
        if self._closed:
            return
        self._closed = True
        self._stop_event.set()
        try:
            self._save_capsule_state()
        except Exception as exc:
            try:
                self.main_gui.log(f'[蜘蛛] 保存位置失败: {exc}', 'warning')
            except Exception:
                pass
        finally:
            for attr in ('_clipboard_after_id', '_status_after_id',
                         '_single_click_after_id', '_paste_after_id'):
                after_id = getattr(self, attr, None)
                if after_id:
                    try:
                        self.win.after_cancel(after_id)
                    except tk.TclError:
                        pass
                    setattr(self, attr, None)
            try:
                self.win.destroy()
            except tk.TclError:
                pass
            self.main_gui.capsule = None
            try:
                self.main_gui.capsule_btn.config(text='开启胶囊')
            except tk.TclError:
                pass

    def start_drag(self, event):
        self.drag_data['x'] = event.x
        self.drag_data['y'] = event.y
        self.drag_data['moved'] = False

    def do_drag(self, event):
        if abs(event.x - self.drag_data['x']) > 3 or abs(event.y - self.drag_data['y']) > 3:
            self.drag_data['moved'] = True
        x = self.win.winfo_x() + event.x - self.drag_data['x']
        y = self.win.winfo_y() + event.y - self.drag_data['y']
        self.win.geometry(f'{int(x):+d}{int(y):+d}')

    def stop_drag(self, event):
        moved = self.drag_data.get('moved', False)
        self.drag_data['x'] = 0
        self.drag_data['y'] = 0
        self.drag_data['moved'] = False
        self._snap_to_edge()
        if self._ignore_next_release:
            self._ignore_next_release = False
        elif not moved:
            self._single_click_after_id = self.win.after(220, self._run_single_click)

    def _run_single_click(self):
        self._single_click_after_id = None
        if not self._closed and not getattr(self.main_gui, '_closing', False):
            try:
                if self.win.winfo_exists():
                    self.toggle_expand()
            except tk.TclError:
                pass

    def _on_spider_double_click(self, _event=None):
        self._ignore_next_release = True
        if self._single_click_after_id:
            self.win.after_cancel(self._single_click_after_id)
            self._single_click_after_id = None
        self.open_main_window()

    def _show_context_menu(self, event):
        if self._closed or getattr(self.main_gui, '_closing', False):
            return
        self.context_menu.delete(0, tk.END)
        self.context_menu.add_command(
            label='暂停剪贴板监听' if self.listen_clipboard else '开启剪贴板监听',
            command=self._toggle_clipboard_from_menu)
        self.context_menu.add_command(
            label='继续主队列' if self.main_gui.nhentai_tab._queue_paused else '暂停主队列',
            command=self.main_gui.nhentai_tab.toggle_queue_pause)
        self.context_menu.add_separator()
        self.context_menu.add_command(label='打开主窗口', command=self.open_main_window)
        self.context_menu.add_command(label='打开下载历史', command=self._open_history)
        self.context_menu.add_separator()
        self.context_menu.add_command(label='关闭蜘蛛', command=self.on_close)
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        except tk.TclError:
            return
        finally:
            try:
                self.context_menu.grab_release()
            except tk.TclError:
                pass

    def _toggle_clipboard_from_menu(self):
        self.listen_var.set(not self.listen_clipboard)
        self._toggle_clipboard_listener()

    def _open_history(self):
        self.open_main_window()
        self.main_gui.notebook.select(self.main_gui.history_tab)

    def _current_size(self):
        return self.EXPANDED_SIZE if self.expanded else self.COMPACT_SIZE

    def _snap_to_edge(self):
        self.win.update_idletasks()
        w, h = self._current_size()
        screen_w = self.win.winfo_screenwidth()
        screen_h = self.win.winfo_screenheight()
        current_x = self.win.winfo_x()
        self._dock_side = 'left' if current_x + w / 2 < screen_w / 2 else 'right'
        if self.expanded:
            x = self.EDGE_GAP if self._dock_side == 'left' else screen_w - w - self.EDGE_GAP
        else:
            x = (-self.COMPACT_EDGE_INSET if self._dock_side == 'left'
                 else screen_w - w + self.COMPACT_EDGE_INSET)
        y = max(self.EDGE_GAP, min(self.win.winfo_y(), screen_h - h - self.EDGE_GAP - 40))
        self.win.geometry(f'{w}x{h}{int(x):+d}{int(y):+d}')
        self._saved_y = int(y)
        self._save_capsule_state()

    def toggle_expand(self, event=None):
        if self.expanded:
            self.expand_frame.pack_forget()
            self.icon_label.pack(fill=tk.BOTH, expand=True)
            self.expanded = False
        else:
            self.icon_label.pack_forget()
            self.expand_frame.pack(fill=tk.BOTH, expand=True)
            self.expanded = True
        self._snap_to_edge()

    def _toggle_clipboard_listener(self):
        self.listen_clipboard = bool(self.listen_var.get())
        self.status_label.config(
            text='正在监听剪贴板' if self.listen_clipboard else '剪贴板监听已暂停',
            fg='#65D6A5' if self.listen_clipboard else '#F0B267')
        self._save_capsule_state()

    def _save_capsule_state(self):
        payload = {
            'dock_side': self._dock_side,
            'y': int(getattr(self, '_saved_y', self.win.winfo_y())),
            'listen_clipboard': bool(self.listen_clipboard),
        }
        try:
            update_app_state(lambda state: state.update(capsule=payload))
            return True
        except Exception:
            return False

    def on_focus_in(self, event):
        if self.entry.get() == '粘贴画廊ID或URL...':
            self.entry.delete(0, tk.END)
            self.entry.config(fg='#E5EAF2')

    def on_focus_out(self, event):
        if not self.entry.get().strip():
            self.entry.insert(0, '粘贴画廊ID或URL...')
            self.entry.config(fg='#8F9AAA')

    def on_paste(self, event):
        if self._paste_after_id:
            try:
                self.win.after_cancel(self._paste_after_id)
            except tk.TclError:
                pass
        self._paste_after_id = self.win.after(50, self.process_paste)
        return None

    def process_paste(self):
        self._paste_after_id = None
        if self._closed or getattr(self.main_gui, '_closing', False):
            return
        try:
            if not self.win.winfo_exists() or not self.entry.winfo_exists():
                return
        except tk.TclError:
            return
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
        return list(dict.fromkeys(ids))

    def add_to_queue(self, gallery_id):
        gallery_id = str(gallery_id)
        task_id = self.main_gui.nhentai_tab.task_queue.add('NHentai', gallery_id, gallery_id)
        if gallery_id not in self.queue:
            self.queue.append(gallery_id)
            self.queue = self.queue[-30:]
            self.queue_listbox.insert(tk.END, f'#{gallery_id}  ·  已投递')
            if self.queue_listbox.size() > 30:
                self.queue_listbox.delete(0)
        self.status_label.config(text=f'已投递 · {gallery_id}', fg='#67D4E0')
        self.main_gui.log(f'[蜘蛛] 已投递到主队列: {gallery_id} (任务 {task_id})', 'header')
        self.main_gui.nhentai_tab._update_queue_label()
        self.main_gui.nhentai_tab._ensure_queue_worker()
        self.update_count()

    def update_count(self):
        self._refresh_main_queue_status(schedule=False)

    def process_queue(self):
        self.main_gui.nhentai_tab._ensure_queue_worker()

    def on_download_done(self, gid, success):
        self._refresh_main_queue_status(schedule=False)

    def _refresh_main_queue_status(self, schedule=True):
        if not self.win.winfo_exists():
            return
        counts = self.main_gui.nhentai_tab.task_queue.count_by_status()
        pending = counts[PENDING]
        running = counts[RUNNING]
        failed = counts[FAILED]
        paused = counts[PAUSED]
        summary = f'主队列 待{pending} / 进行{running} / 失败{failed}'
        if paused:
            summary += f' / 暂停{paused}'
        self.queue_status_label.config(text=summary)

        if running or self.main_gui.nhentai_tab.is_downloading:
            color = UI['cyan']
            title = self.main_gui.nhentai_tab.transfer_title.cget('text')
            percent = self.main_gui.nhentai_tab.file_percent.cget('text')
            speed = self.main_gui.nhentai_tab.speed_label.cget('text')
            progress = ' · '.join(part for part in (percent, speed) if part)
            text = f'{title[:20]}  {progress}' if progress else title
            self.status_label.config(text=(text or '正在下载')[:42], fg='#67D4E0')
        elif failed:
            color = UI['danger']
            self.status_label.config(text=f'有 {failed} 个失败任务', fg='#FF8490')
        elif pending or paused:
            color = UI['warning']
            self.status_label.config(text=f'等待执行 · {pending + paused} 个任务', fg='#F0B267')
        else:
            color = UI['success'] if self.listen_clipboard else UI['primary']
            self.status_label.config(
                text='空闲 · 监听剪贴板' if self.listen_clipboard else '空闲 · 监听已暂停',
                fg='#65D6A5' if self.listen_clipboard else '#A99BFF')
        if color != self._spider_color:
            self._draw_spider_icon(color)
            self.container.config(highlightbackground=color)
        if schedule:
            self._status_after_id = self.win.after(500, self._refresh_main_queue_status)

    def clear_queue(self, event=None):
        self.queue.clear()
        self.queue_listbox.delete(0, tk.END)
        self.status_label.config(text='最近投递记录已清空', fg='#8F9AAA')
        self.main_gui.log('[蜘蛛] 最近投递记录已清空', 'info')

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
        self.task_queue = PersistentTaskQueue()
        self._queue_paused = False
        self._queue_worker_running = False
        self._anti_status_after_id = None
        self.setup_ui()
        self.gui.root.after(1200, self._recover_queue_on_start)

    def setup_ui(self):
        # ===== 设置区 =====
        settings_frame = ttk.LabelFrame(self, text='工作方式', padding=12,
                                        style='Card.TLabelframe')
        settings_frame.pack(fill=tk.X, padx=12, pady=(12, 6))

        row1 = ttk.Frame(settings_frame)
        row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text='代理出口').pack(side=tk.LEFT)
        self.gui.nh_proxy_var = tk.StringVar(value='http://127.0.0.1:7897')
        ttk.Entry(row1, textvariable=self.gui.nh_proxy_var, width=30).pack(side=tk.LEFT, padx=4)
        ttk.Label(row1, text='多个用逗号分隔，direct 为直连备用',
                  style='Muted.TLabel').pack(side=tk.LEFT)
        ttk.Label(row1, text='保存到').pack(side=tk.LEFT, padx=(14, 0))
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
        self.gui.nh_pause_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2, text='等待', variable=self.gui.nh_pause_var,
                        command=self._toggle_nh_pause).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(row2, text='测速', command=self.test_proxy_speed, width=5).pack(side=tk.LEFT, padx=(10, 0))
        self.gui.cf_status_label = ttk.Label(row2, text='\u25cf', foreground=UI['success'], font=('Segoe UI', 12))
        self.gui.cf_status_label.pack(side=tk.LEFT, padx=(10, 0))
        self.gui.cf_text_label = ttk.Label(row2, text='CF:0', style='Muted.TLabel')
        self.gui.cf_text_label.pack(side=tk.LEFT)

        # ===== 请求、反封禁与队列状态 =====
        anti_frame = ttk.Frame(settings_frame)
        anti_frame.pack(fill=tk.X, pady=(2, 0))

        self.anti_stealth_lbl = ttk.Label(anti_frame, text='\u25cf 隐身',
                                           font=FONT_SMALL, foreground=UI['success'])
        self.anti_stealth_lbl.pack(side=tk.LEFT, padx=(0, 10))
        self.anti_ua_lbl = ttk.Label(anti_frame, text='\u25cf UA池:7',
                                     font=FONT_SMALL, foreground=UI['success'])
        self.anti_ua_lbl.pack(side=tk.LEFT, padx=(0, 10))
        self.anti_delay_lbl = ttk.Label(anti_frame, text='\u25cf 延迟',
                                        font=FONT_SMALL, foreground=UI['success'])
        self.anti_delay_lbl.pack(side=tk.LEFT, padx=(0, 10))
        self.anti_proxy_lbl = ttk.Label(anti_frame, text='\u25cb 代理',
                                        font=FONT_SMALL, foreground=UI['muted'])
        self.anti_proxy_lbl.pack(side=tk.LEFT, padx=(0, 10))
        self.anti_tls_lbl = ttk.Label(anti_frame, text='\u25cf TLS',
                                      font=FONT_SMALL, foreground=UI['success'])
        self.anti_tls_lbl.pack(side=tk.LEFT, padx=(0, 10))
        self.queue_label = ttk.Label(anti_frame, text='队列 0/0/0', style='Pill.TLabel')
        self.queue_label.pack(side=tk.RIGHT)
        self.anti_req_lbl = ttk.Label(anti_frame, text='请求 0  并发 0/0',
                                      font=('Cascadia Mono', 8), foreground=UI['cyan'])
        self.anti_req_lbl.pack(side=tk.RIGHT, padx=(0, 10))
        self.anti_block_lbl = ttk.Label(anti_frame, text='\u25cf 反爬就绪',
                                        font=FONT_SMALL, foreground=UI['success'])
        self.anti_block_lbl.pack(side=tk.RIGHT, padx=(0, 10))

        # ===== 主体分栏 =====
        main_pane = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main_pane.pack(fill=tk.BOTH, expand=True, padx=12, pady=(6, 12))

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
        ttk.Label(cover_header, text='本地书架', style='Section.TLabel').pack(side=tk.LEFT, padx=4, pady=5)
        self.cover_toggle_btn = ttk.Button(cover_header, text='隐藏封面',
                                           command=self.toggle_covers, width=8)
        self.cover_toggle_btn.pack(side=tk.RIGHT, padx=4, pady=2)

        self.cover_gallery = self._create_cover_gallery(cover_outer)
        self.cover_gallery.pack(fill=tk.BOTH, expand=True)
        self.cover_gallery.canvas.bind('<Configure>', self._on_cover_gallery_resize)

        # 文件列表
        file_frame = ttk.LabelFrame(left_split, text='画廊文件', padding=6,
                                    style='Card.TLabelframe')
        left_split.add(file_frame, weight=4)
        columns = ('name', 'size')
        self.file_tree = ttk.Treeview(file_frame, columns=columns, show='headings', height=10,
                                      style='Files.Treeview')
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
        input_frame = ttk.LabelFrame(right_split, text='把 ID 或链接放进来', padding=10,
                                     style='Card.TLabelframe')
        right_split.add(input_frame, weight=0)
        self.input_text = scrolledtext.ScrolledText(input_frame, height=4, wrap=tk.WORD)
        self.input_text.configure(bg=UI['surface_soft'], fg=UI['text'], insertbackground=UI['primary'],
                                  selectbackground=UI['primary_soft'], relief=tk.FLAT,
                                  highlightthickness=1, highlightbackground=UI['border'],
                                  highlightcolor=UI['primary'], padx=10, pady=8, font=FONT_UI)
        self.input_text.pack(fill=tk.X)

        btn_row = ttk.Frame(input_frame)
        btn_row.pack(fill=tk.X, pady=(4, 0))
        self.start_btn = ttk.Button(btn_row, text='加入队列', command=self.start_download,
                                    style='Accent.TButton')
        self.start_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.stop_btn = ttk.Button(btn_row, text='停止', command=self.stop_download,
                                   state=tk.DISABLED, style='Danger.TButton')
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.view_btn = ttk.Button(btn_row, text='查看信息', command=self.view_gallery_info)
        self.view_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_row, text='打开下载目录', command=self.open_output).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_row, text='清理临时文件', command=self.cleanup_partial_files).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_row, text='清空日志', command=self.gui.clear_log).pack(side=tk.LEFT, padx=(0, 6))
        self.gui.capsule_btn = ttk.Button(btn_row, text='开启胶囊', command=self.gui.toggle_capsule)
        self.gui.capsule_btn.pack(side=tk.RIGHT)

        queue_row = ttk.Frame(input_frame)
        queue_row.pack(fill=tk.X, pady=(4, 0))
        self.queue_pause_btn = ttk.Button(queue_row, text='暂停队列', command=self.toggle_queue_pause, width=8)
        self.queue_pause_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.retry_failed_btn = ttk.Button(queue_row, text='重试失败', command=self.retry_failed, width=8)
        self.retry_failed_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.integrity_btn = ttk.Button(queue_row, text='校验修复', command=self.integrity_check, width=8)
        self.integrity_btn.pack(side=tk.LEFT)

        # 进度区
        transfer_frame = ttk.LabelFrame(right_split, text='当前传输', padding=12,
                                        style='Card.TLabelframe')
        right_split.add(transfer_frame, weight=0)

        self.transfer_title = ttk.Label(transfer_frame, text='空闲 · 等待队列任务',
                                        style='Section.TLabel')
        self.transfer_title.pack(anchor=tk.W)
        self.transfer_detail = ttk.Label(transfer_frame, text='', style='Muted.TLabel')
        self.transfer_detail.pack(anchor=tk.W)

        prog_row = ttk.Frame(transfer_frame)
        prog_row.pack(fill=tk.X, pady=(6, 0))
        self.file_progress = ttk.Progressbar(prog_row, mode='determinate', length=300,
                                             style='Quiet.Horizontal.TProgressbar')
        self.file_progress.pack(fill=tk.X, side=tk.LEFT, expand=True)
        self.file_percent = ttk.Label(prog_row, text='0%', width=6, anchor=tk.E)
        self.file_percent.pack(side=tk.RIGHT, padx=(6, 0))

        spd_row = ttk.Frame(transfer_frame)
        spd_row.pack(fill=tk.X, pady=(4, 0))
        self.speed_label = ttk.Label(spd_row, text='', style='Muted.TLabel')
        self.speed_label.pack(side=tk.LEFT)
        self.time_label = ttk.Label(spd_row, text='', style='Muted.TLabel')
        self.time_label.pack(side=tk.RIGHT)

        # 日志区
        log_frame = ttk.LabelFrame(right_split, text='爬虫控制台', padding=6,
                                   style='Card.TLabelframe')
        right_split.add(log_frame, weight=2)
        self.log_text = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, state=tk.DISABLED,
                                                   font=FONT_MONO)
        self.log_text.configure(bg=UI['log_bg'], fg=UI['log_text'], insertbackground='white',
                                selectbackground='#3C466A', relief=tk.FLAT,
                                highlightthickness=0, padx=10, pady=8)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.init_log_tags()

    def _create_cover_gallery(self, parent):
        canvas = tk.Canvas(parent, highlightthickness=0, bg=UI['bg'])
        scrollbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack_forget()
        inner = ttk.Frame(canvas)
        canvas_window = canvas.create_window((0, 0), window=inner, anchor='nw')
        inner.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        for w in (canvas, inner):
            w.bind('<MouseWheel>', lambda ev: canvas.yview_scroll(
                int(-1 * (ev.delta / 120)), 'units'))

        class CoverGalleryWrapper(ttk.Frame):
            def __init__(self, master, canvas_w, scrollbar_w, inner_w):
                super().__init__(master)
                self.canvas = canvas_w
                self.inner = inner_w
                self.scrollbar = scrollbar_w
                self.canvas_window = canvas_window
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

    def _on_cover_gallery_resize(self, event):
        self.cover_gallery.canvas.itemconfigure(
            self.cover_gallery.canvas_window, width=max(1, event.width))
        if getattr(self, '_cover_reflow_after', None):
            self.after_cancel(self._cover_reflow_after)
        self._cover_reflow_after = self.after(80, self._reflow_cover_gallery)

    def _reflow_cover_gallery(self):
        items = getattr(self, '_cover_items', [])
        if not items:
            return
        width = max(1, self.cover_gallery.canvas.winfo_width())
        cols = max(1, width // 136)
        old_cols = getattr(self, '_cover_columns', 0)
        for idx, item in enumerate(items):
            item.grid_configure(row=idx // cols, column=idx % cols)
        for col in range(max(old_cols, cols)):
            self.cover_gallery.inner.columnconfigure(col, weight=1 if col < cols else 0)
        self._cover_columns = cols

    def init_log_tags(self):
        self.log_text.tag_configure('info', foreground=UI['log_text'])
        self.log_text.tag_configure('success', foreground='#65D6A5', font=('Cascadia Mono', 9, 'bold'))
        self.log_text.tag_configure('error', foreground='#FF8490')
        self.log_text.tag_configure('warning', foreground='#F0B267')
        self.log_text.tag_configure('header', foreground='#A99BFF', font=('Cascadia Mono', 9, 'bold'))
        self.log_text.tag_configure('thread', foreground='#67D4E0')

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
        self._cover_items = []

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

        for idx, (gd, first_img) in enumerate(valid):
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
            item.grid(row=0, column=idx, padx=3, pady=3, sticky='n')
            self._cover_items.append(item)
            if photo:
                ttk.Label(item, image=photo).pack()

            dn = strip_status_tag(gd.name)
            if len(dn) > 22:
                dn = dn[:20] + '...'
            is_complete = TAG_OK in gd.name
            ttk.Label(item, text=dn, font=('Microsoft YaHei UI', 8),
                      foreground='#22863a' if is_complete else '#e36209', wraplength=120).pack()

        self._reflow_cover_gallery()

    def _recover_queue_on_start(self):
        try:
            n_running = self.task_queue.recover()
            pending = self.task_queue.pending('NHentai')
            if pending:
                self.gui.log(f'[队列] 检测到上次未完成任务 {len(pending)} 个（恢复 {n_running} 个进行中），自动继续', 'header')
                self._ensure_queue_worker()
            elif n_running:
                self.gui.log(f'[队列] 已恢复 {n_running} 个进行中任务', 'info')
            self._update_queue_label()
        except Exception as e:
            self.gui.log(f'[队列] 恢复失败: {e}', 'error')

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

        self.task_queue.add_many([('NHentai', gid, gid) for gid in unique_ids])
        self.gui.log(f'[队列] 已加入 {len(unique_ids)} 个画廊，开始处理', 'header')
        self._update_queue_label()
        self._ensure_queue_worker()

    def _ensure_queue_worker(self):
        if self._queue_worker_running:
            return
        self._queue_worker_running = True
        self.is_downloading = True
        self.gui.set_download_tab_running('nh', True)
        self._stop_requested = False
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.file_tree.delete(*self.file_tree.get_children())
        self._nh_req_count = 0
        self._update_anti_status()
        self._worker_config = {
            'proxy': self.gui.nh_proxy_var.get().strip() or None,
            'output_dir': self.gui.nh_output_var.get(),
            'quality': self.gui.nh_quality_var.get(),
            'max_rounds': self.gui.nh_retry_var.get(),
            'stealth_mode': self.gui.nh_stealth_var.get(),
            'workers': self.gui.nh_workers_var.get(),
            'speed_mode': self.gui.nh_speed_mode_var.get(),
            'browser_priority': self.gui.nh_browser_priority_var.get(),
            'pause_enabled': self.gui.nh_pause_var.get(),
        }
        self.download_thread = threading.Thread(target=self._queue_worker, daemon=True)
        self.download_thread.start()

    def _update_queue_label(self):
        try:
            counts = self.task_queue.count_by_status()
            self.queue_label.config(
                text=f'队列 待{counts[PENDING]}/进行{counts[RUNNING]}/失败{counts[FAILED]}'
                     + (f'  暂停{counts[PAUSED]}' if counts[PAUSED] else ''))
            self.queue_pause_btn.config(text='继续队列' if self._queue_paused else '暂停队列')
        except Exception:
            pass

    def toggle_queue_pause(self):
        self._queue_paused = not self._queue_paused
        if self._queue_paused:
            self.task_queue.pause_all()
            self.gui.log('[队列] 已暂停，当前任务结束后停止', 'warning')
        else:
            self.task_queue.resume_all()
            self.gui.log('[队列] 已继续', 'info')
        self._update_queue_label()

    def retry_failed(self):
        n = self.task_queue.retry_all()
        self._update_queue_label()
        if n:
            self.gui.log(f'[队列] 已重试 {n} 个失败任务', 'info')
            self._ensure_queue_worker()
        else:
            self.gui.log('[队列] 没有可重试的失败任务', 'info')

    def _queue_worker(self):
        crawler = None
        done = failed = 0
        try:
            config = dict(self._worker_config)
            output_dir = config['output_dir']
            crawler = NHentaiCrawler(
                proxy=config['proxy'],
                output_dir=output_dir,
                quality=config['quality'],
                max_rounds=config['max_rounds'],
                stealth_mode=config['stealth_mode'],
                use_browser_fallback=True,
                workers=config['workers'],
                speed_mode=config['speed_mode'],
                browser_priority=config['browser_priority'],
                pause_enabled=config['pause_enabled'],
            )
            self.crawler = crawler
            self.gui.post(self._update_queue_label)

            while not self._stop_requested and not crawler._stop_event.is_set():
                if self._queue_paused:
                    self.gui.post(self._update_queue_label)
                    time.sleep(0.5)
                    continue
                task = self.task_queue.next('NHentai')
                if not task:
                    break
                claimed = self.task_queue.claim(task['id'])
                if claimed is None:
                    continue
                gid = claimed['item_id']
                self.gui.post(self._update_queue_label)
                ok, result = self._process_one(crawler, output_dir, gid)
                if ok:
                    done += 1
                    self.task_queue.finish(claimed['id'], True)
                    self._write_nh_history(claimed['id'], gid, result)
                elif self._stop_requested or crawler._stop_event.is_set():
                    # 用户主动停止：保留现场为暂停，不记失败、不自动重试
                    self.task_queue.park(claimed['id'])
                    result.update(status='cancelled', error='用户取消')
                    self._write_nh_history(claimed['id'], gid, result)
                else:
                    self.task_queue.finish(claimed['id'], False, error='下载失败')
                    cur = self.task_queue.get(claimed['id'])
                    if cur and cur['attempts'] < cur['max_attempts']:
                        self.task_queue.retry(claimed['id'])
                        self.gui.post(self.gui.log,
                                      f'[队列] [{gid}] 失败，将自动重试 {cur["attempts"]}/{cur["max_attempts"]}', 'warning')
                    else:
                        failed += 1
                        self._write_nh_history(claimed['id'], gid, result)
                self.gui.post(self._update_queue_label)

            counts = self.task_queue.count_by_status()
            remaining = counts[PENDING] + counts[PAUSED]
            total = done + failed + remaining
            self._last_err_count = len(crawler.errors) if crawler else 0
            self.gui.post(self.download_finished, total, done, failed, self._stop_requested)
        except Exception as e:
            self.gui.post(self.gui.log, f'[队列] 异常: {translate_error(str(e))}', 'error')
            self.gui.post(self.download_finished, done + failed, done, failed, False)
        finally:
            self._queue_worker_running = False
            if crawler is not None:
                crawler.close()
            self.crawler = None

    @staticmethod
    def _default_nh_result(gid):
        return {
            'title': str(gid), 'status': 'failed', 'total': 0, 'missing': 0,
            'path': '', 'error': '下载失败',
        }

    def _write_nh_history(self, task_id, gid, result):
        data = dict(self._default_nh_result(gid))
        data.update(result or {})
        append_download_history(
            'NHentai', gid, data['title'], data['status'],
            total=data['total'], missing=data['missing'], path=data['path'],
            error=data['error'], task_id=task_id)
        self.gui.post(self.gui.refresh_history)

    def _process_one(self, crawler, output_dir, gid):
        result = self._default_nh_result(gid)

        def callback(evt, gid_inner=None, data=None):
            g = gid_inner or gid
            if evt == 'gallery_info':
                result['title'] = data.get('title') or g
                result['total'] = int(data.get('num_pages') or 0)
                self.gui.post(self.gui.log, f'[{g}] 标题: {data["title"]}', 'info')
                self.gui.post(self.gui.log, f'[{g}] 共 {data["num_pages"]} 页', 'info')
                self.gui.post(lambda: self.transfer_title.config(text=f'下载中 · {data["title"][:40]}'))
                self.gui.post(lambda: self.transfer_detail.config(
                    text=f'任务 {g}  ·  页数 0/{data["num_pages"]}'))
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
                              f'[{g}] 第 {data["round"]}/{data["max_rounds"]} 轮重试，剩余 {data["remaining"]} 页', 'warning')
            elif evt == 'error':
                result['error'] = str(data)
                self.gui.post(self.gui.log, f'[{g}] {data}', 'error')
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
                                       missing=m, total=t)
                result.update(
                    title=data.get('title', g),
                    status='complete' if m == 0 else 'partial',
                    total=t,
                    missing=m,
                    path=data.get('path') or str(Path(output_dir) / data['dir_name']),
                    error=f'仍缺失 {m} 页' if m else None,
                )
                try:
                    verifier_path = data.get('path') or str(Path(output_dir) / data["dir_name"])
                    IntegrityVerifier(output_dir).update_manifest(g, verifier_path)
                except Exception:
                    pass
                self.gui.post(lambda f=data.get('files', []): self.update_file_tree(f))
                self.gui.post(self.refresh_covers)
                self.gui.post(self.gui.refresh_collection)
            elif evt == 'cancelled':
                result.update(
                    title=data.get('title', g), status='cancelled',
                    total=data.get('total', 0), missing=data.get('missing', 0),
                    path=data.get('path', ''), error='用户取消')
                self.gui.post(self.gui.log,
                              f'[{g}] 已停止，保留 {data.get("downloaded", 0)}/{data.get("total", 0)} 页', 'warning')

        try:
            return crawler.download_gallery(gid, callback=callback), result
        except Exception as e:
            result['error'] = str(e)
            self.gui.post(self.gui.log, f'[异常] [{gid}] {translate_error(str(e))}', 'error')
            return False, result

    def integrity_check(self):
        output = Path(self.gui.nh_output_var.get())
        if not output.exists():
            messagebox.showinfo('提示', '输出目录不存在')
            return
        self.integrity_btn.config(state=tk.DISABLED)
        self.gui.log('[校验] 开始完整性扫描...', 'header')
        config = {
            'proxy': self.gui.nh_proxy_var.get().strip() or None,
            'stealth_mode': self.gui.nh_stealth_var.get(),
            'workers': min(self.gui.nh_workers_var.get(), 8),
            'pause_enabled': self.gui.nh_pause_var.get(),
        }

        def run():
            verifier = IntegrityVerifier(output)
            try:
                results = verifier.verify_all()
                ok_count = sum(1 for _v, s in results if s == 'ok')
                self.gui.post(self.gui.log, f'[校验] 扫描 {len(results)} 个画廊，正常 {ok_count} 个', 'header')
                issues = []
                for v, s in results:
                    if v.ok:
                        continue
                    issues.append(v)
                    detail = []
                    if v.missing:
                        detail.append(f'缺页{v.missing}')
                    if v.corrupt:
                        detail.append(f'损坏{v.corrupt}')
                    if v.zero_byte:
                        detail.append(f'空文件{v.zero_byte}')
                    if v.mismatch:
                        detail.append(f'清单不符{v.mismatch}')
                    self.gui.post(self.gui.log, f'[校验] {v.gallery_id}: {";".join(detail)}', 'warning')
                if not issues:
                    self.gui.post(self.gui.log, '[校验] 全部通过', 'success')
                    return
                self.gui.post(self.gui.log, f'[校验] 发现 {len(issues)} 个问题画廊，开始自动补修复...', 'header')
                crawler = NHentaiCrawler(
                    proxy=config['proxy'],
                    output_dir=str(output),
                    stealth_mode=config['stealth_mode'],
                    use_browser_fallback=True,
                    workers=config['workers'],
                    speed_mode='保守',
                    pause_enabled=config['pause_enabled'],
                )
                try:
                    for i, v in enumerate(issues, 1):
                        self.gui.post(self.gui.log, f'[修复] ({i}/{len(issues)}) {v.gallery_id} ...', 'info')
                        ok, verdict = verifier.repair_gallery(crawler, v.gallery_id)
                        tag = 'success' if ok else 'error'
                        self.gui.post(self.gui.log,
                                      f'[修复] {v.gallery_id} {"完成" if ok else "仍有问题: " + str(verdict.missing[:10])}', tag)
                finally:
                    crawler.close()
                self.gui.post(self.gui.refresh_collection)
            except Exception as e:
                self.gui.post(self.gui.log, f'[校验] 异常: {translate_error(str(e))}', 'error')
            finally:
                self.gui.post(lambda: self.integrity_btn.config(state=tk.NORMAL))

        threading.Thread(target=run, daemon=True).start()

    def download_finished(self, total, success, fail, stopped=False):
        self.is_downloading = False
        self.gui.set_download_tab_running('nh', False)
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.gui.log('=' * 50, 'header')
        if stopped:
            self.gui.log(f'已停止，已完成: {success}   未处理: {total - success - fail}   失败: {fail}', 'warning')
        else:
            self.gui.log(f'总计: {total}   完整: {success} {TAG_OK}   失败: {fail}',
                         'success' if fail == 0 else 'warning')
        err_count = getattr(self, '_last_err_count', 0)
        if err_count:
            self.gui.log(f'共 {err_count} 个错误', 'error')
        state = '已停止' if stopped else ('校验完成' if fail == 0 else '处理完成')
        self.transfer_title.config(text=f'{state} · 完整 {success}  失败 {fail}')
        self.transfer_detail.config(text=f'任务 {total}  ·  完整 {success}  ·  失败 {fail}')
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
        if self._anti_status_after_id:
            try:
                self.gui.root.after_cancel(self._anti_status_after_id)
            except tk.TclError:
                pass
            self._anti_status_after_id = None
        proxy = self.gui.nh_proxy_var.get().strip()
        speed = self.gui.nh_speed_mode_var.get()

        self.anti_stealth_lbl.config(
            foreground=UI['success'] if self.gui.nh_stealth_var.get() else UI['muted'],
            text='\u25cf 隐身' if self.gui.nh_stealth_var.get() else '\u25cb 隐身')

        self.anti_proxy_lbl.config(
            foreground=UI['cyan'] if proxy else UI['muted'],
            text='\u25cf 代理' if proxy else '\u25cb 代理')

        self.anti_tls_lbl.config(
            foreground=UI['success'] if SCRAPLING_AVAILABLE else UI['muted'],
            text='\u25cf TLS' if SCRAPLING_AVAILABLE else '\u25cb TLS')

        delay_text = '0.01-0.05s' if speed == '极速' else ('0.3-1s' if speed == '保守' else '0s')
        self.anti_delay_lbl.config(text=f'\u25cf 延迟 {delay_text}',
                                   foreground=UI['success'] if speed != '狂暴' else UI['warning'])

        self.anti_ua_lbl.config(
            foreground=UI['success'],
            text='\u25cf UA池:7')

        snapshot = self.crawler.scheduler.snapshot() if self.crawler else None
        if snapshot:
            mode_text = '浏览优先' if snapshot['browser_priority'] else '自适应'
            pause_text = '等待开启' if snapshot.get('pause_enabled', True) else '等待关闭'
            cooldown = int(snapshot['site_cooldown'] + 0.999)
            cooldown_text = f'  冷却 {cooldown}s' if cooldown else ''
            self.anti_req_lbl.config(
                text=f'{mode_text}  请求 {snapshot["requests"]}  并发 {snapshot["active"]}/{snapshot["limit"]}  熔断 {snapshot["open_routes"]}{cooldown_text}  {pause_text}',
                foreground=UI['cyan'])
        else:
            self.anti_req_lbl.config(text='自适应  请求 0  并发 0/0', foreground=UI['cyan'])

        if self.crawler and hasattr(self.crawler, 'anti_crawl'):
            ac = self.crawler.anti_crawl
            status_text, color = ac.get_status_text()
            defense = ac.get_defense_info()
            self.anti_block_lbl.config(
                text=f'\u25cf {status_text}  [{defense}]', foreground=color)

        if self.is_downloading:
            self._anti_status_after_id = self.gui.root.after(1000, self._update_anti_status)

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

    def _toggle_nh_pause(self):
        if self.crawler:
            self.crawler.scheduler.configure(
                pause_enabled=self.gui.nh_pause_var.get())
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

    def cleanup_partial_files(self):
        if self.is_downloading:
            messagebox.showwarning('提示', '请先停止下载，再清理临时文件')
            return
        output = Path(self.gui.nh_output_var.get())
        if not output.exists():
            messagebox.showinfo('提示', '输出目录不存在')
            return
        files = [path for path in output.rglob('*')
                 if path.is_file() and (path.name.endswith('.part') or '.part.' in path.name)]
        if not files:
            messagebox.showinfo('清理临时文件', '没有发现临时文件')
            return
        total_size = sum(path.stat().st_size for path in files)
        if not messagebox.askyesno(
                '清理临时文件', f'发现 {len(files)} 个临时文件，共 {format_size(total_size)}。确认删除？'):
            return
        removed = 0
        for path in files:
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                self.gui.log(f'[清理] 无法删除 {path}: {exc}', 'warning')
        self.gui.log(f'[清理] 已删除 {removed}/{len(files)} 个临时文件，释放 {format_size(total_size)}', 'success')

    def test_proxy_speed(self):
        proxy = self.gui.nh_proxy_var.get().strip() or None
        if not proxy:
            messagebox.showwarning('提示', '请填写代理地址')
            return
        output_dir = self.gui.nh_output_var.get()

        def run():
            test_crawler = NHentaiCrawler(proxy=proxy, output_dir=output_dir,
                                          stealth_mode=False, use_browser_fallback=False,
                                          workers=1, speed_mode='极速')
            try:
                success, result = test_crawler.test_proxy_speed()
            finally:
                test_crawler.close()
            if success:
                self.gui.post(self.gui.log, f'[测速] 延迟: {result:.0f}ms', 'success')
                self.gui.post(messagebox.showinfo, '测速结果', f'延迟: {result:.0f}ms')
            else:
                self.gui.post(self.gui.log, f'[测速] 失败: {result}', 'error')
                self.gui.post(messagebox.showerror, '测速失败', f'代理不可用\n{result}')

        threading.Thread(target=run, daemon=True).start()

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
        config = {
            'proxy': self.gui.nh_proxy_var.get().strip() or None,
            'output_dir': self.gui.nh_output_var.get(),
            'stealth_mode': self.gui.nh_stealth_var.get(),
        }

        def fetch_info():
            crawler = NHentaiCrawler(
                proxy=config['proxy'],
                output_dir=config['output_dir'],
                stealth_mode=config['stealth_mode'],
                use_browser_fallback=True,
                workers=1, speed_mode='保守'
            )
            try:
                for gid in unique_ids:
                    info, error = crawler.get_gallery_info_enhanced(gid)
                    if error:
                        self.gui.post(self.gui.log, f'[{gid}] 查询失败: {error}', 'error')
                        continue
                    self.gui.post(self._log_info_detail, gid, info)
            finally:
                crawler.close()

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
        self._anti_status_after_id = None
        self._check_existing_after_id = None
        self.scheduler = AdaptiveScheduler(
            max_concurrency=8, min_concurrency=1,
            failure_threshold=3, cooldown=25,
            recovery_successes=10, proxies=['http://127.0.0.1:7897'])
        self.setup_ui()
        self.gui.log('[JM Comic] 面板已初始化', 'info')

    def setup_ui(self):
        main_frame = ttk.Frame(self, padding=12)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 配置区
        config_frame = ttk.LabelFrame(main_frame, text='工作方式', padding=12,
                                      style='Card.TLabelframe')
        config_frame.pack(fill=tk.X, pady=(0, 8))

        connection_frame = ttk.Frame(config_frame)
        connection_frame.grid(row=0, column=0, sticky=tk.NSEW, padx=(0, 14))
        login_frame = ttk.LabelFrame(config_frame, text='登录信息（HTML 模式）', padding=8)
        login_frame.grid(row=0, column=1, sticky=tk.NSEW)
        config_frame.columnconfigure(0, weight=3)
        config_frame.columnconfigure(1, weight=2)

        ttk.Label(connection_frame, text='保存到').grid(row=0, column=0, sticky=tk.W, padx=(0, 5))
        self.jm_dir_var = tk.StringVar(value=str(Path.cwd() / 'downloads' / 'JMComic'))
        ttk.Entry(connection_frame, textvariable=self.jm_dir_var, width=38).grid(row=0, column=1, sticky=tk.EW)
        ttk.Button(connection_frame, text='浏览', command=self._browse_dir, width=6).grid(row=0, column=2, padx=(5, 0))

        ttk.Label(connection_frame, text='代理出口').grid(row=1, column=0, sticky=tk.W, pady=(8, 0))
        self.jm_proxy_var = tk.StringVar(value='http://127.0.0.1:7897')
        proxy_entry = ttk.Entry(connection_frame, textvariable=self.jm_proxy_var, width=30)
        proxy_entry.grid(row=1, column=1, sticky=tk.EW, pady=(8, 0))
        ttk.Label(connection_frame, text='逗号分隔', style='Muted.TLabel').grid(
            row=1, column=2, sticky=tk.W, padx=(5, 0), pady=(8, 0))

        ttk.Label(connection_frame, text='模式').grid(row=2, column=0, sticky=tk.W, pady=(8, 0))
        mode_frame = ttk.Frame(connection_frame)
        mode_frame.grid(row=2, column=1, columnspan=2, sticky=tk.W, pady=(8, 0))
        self.jm_mode_var = tk.StringVar(value='api')
        ttk.Radiobutton(mode_frame, text='API (免登录)', variable=self.jm_mode_var,
                        value='api').pack(side=tk.LEFT, padx=(0, 15))
        ttk.Radiobutton(mode_frame, text='HTML (需登录)', variable=self.jm_mode_var,
                        value='html').pack(side=tk.LEFT, padx=(0, 15))
        connection_frame.columnconfigure(1, weight=1)

        ttk.Label(login_frame, text='用户名').grid(row=0, column=0, sticky=tk.W, padx=(0, 5))
        self.jm_username_var = tk.StringVar()
        ttk.Entry(login_frame, textvariable=self.jm_username_var, width=22).grid(
            row=0, column=1, sticky=tk.EW)
        ttk.Label(login_frame, text='密码').grid(row=1, column=0, sticky=tk.W, padx=(0, 5), pady=(8, 0))
        self.jm_password_var = tk.StringVar()
        ttk.Entry(login_frame, textvariable=self.jm_password_var, width=22, show='*').grid(
            row=1, column=1, sticky=tk.EW, pady=(8, 0))
        login_frame.columnconfigure(1, weight=1)

        # 线程与反封禁配置
        thread_frame = ttk.Frame(config_frame)
        thread_frame.grid(row=1, column=0, columnspan=2, sticky=tk.EW, pady=(10, 0))
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
        self.jm_pause_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(thread_frame, text='等待', variable=self.jm_pause_var,
                        command=self._toggle_jm_pause).pack(side=tk.LEFT, padx=(8, 0))

        # ID输入区
        input_frame = ttk.LabelFrame(main_frame, text='把本子 ID 放进来', padding=12,
                                     style='Card.TLabelframe')
        input_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(input_frame, text='本子ID:').grid(row=0, column=0, sticky=tk.NW, padx=(0, 5))
        self.jm_id_text = tk.Text(input_frame, height=5, width=30, bg=UI['surface_soft'],
                                  fg=UI['text'], insertbackground=UI['primary'],
                                  selectbackground=UI['primary_soft'], relief=tk.FLAT,
                                  highlightthickness=1, highlightbackground=UI['border'],
                                  highlightcolor=UI['primary'], padx=10, pady=8, font=FONT_UI)
        self.jm_id_text.grid(row=0, column=1, sticky=tk.EW)
        ttk.Label(input_frame,
                  text='一行一个ID\n章节ID加 p 前缀\n如: JM123456 或 p123456',
                  foreground='gray').grid(row=0, column=2, sticky=tk.NW, padx=(5, 0))
        input_frame.columnconfigure(1, weight=1)

        self.jm_existing_label = ttk.Label(input_frame, text='',
                                           font=('Microsoft YaHei UI', 8),
                                           foreground='#22863a')
        self.jm_existing_label.grid(row=1, column=1, sticky=tk.W, pady=(2, 0))
        self.jm_id_text.bind('<KeyRelease>', self._schedule_check_existing)

        # 按钮区
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=(0, 8))

        self.jm_download_btn = ttk.Button(btn_frame, text='加入队列', command=self._start_download,
                                          width=12, style='Accent.TButton')
        self.jm_download_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.jm_view_btn = ttk.Button(btn_frame, text='查看本子信息', command=self._view_album, width=12)
        self.jm_view_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.jm_stop_btn = ttk.Button(btn_frame, text='停止', command=self._stop, width=8,
                                      state=tk.DISABLED, style='Danger.TButton')
        self.jm_stop_btn.pack(side=tk.LEFT)

        self.jm_integrity_btn = ttk.Button(btn_frame, text='校验修复', command=self._integrity_check, width=8)
        self.jm_integrity_btn.pack(side=tk.LEFT, padx=(8, 0))

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
        log_frame = ttk.LabelFrame(main_frame, text='爬虫控制台', padding=6,
                                   style='Card.TLabelframe')
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.jm_log_area = scrolledtext.ScrolledText(log_frame, height=10, state=tk.DISABLED, wrap=tk.WORD,
                                                     font=FONT_MONO)
        self.jm_log_area.configure(bg=UI['log_bg'], fg=UI['log_text'], insertbackground='white',
                                   selectbackground='#3C466A', relief=tk.FLAT,
                                   highlightthickness=0, padx=10, pady=8)
        self.jm_log_area.tag_configure('info', foreground=UI['log_text'])
        self.jm_log_area.tag_configure('success', foreground='#65D6A5')
        self.jm_log_area.tag_configure('error', foreground='#FF8490')
        self.jm_log_area.tag_configure('warning', foreground='#F0B267')
        self.jm_log_area.tag_configure('header', foreground='#A99BFF',
                                       font=('Cascadia Mono', 9, 'bold'))
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
        proxy_str = self.jm_proxy_var.get().strip()

        def run():
            import requests as req
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
            trusted_domains = set(known)
            found_domains.intersection_update(trusted_domains)
            found_domains.update(trusted_domains)

            self.gui.post(self._jm_log,
                               f'[域名] 收集到 {len(found_domains)} 个候选域名，正在检测可用性...', 'info')

            # Round 3: Health check
            healthy = []
            for d in sorted(found_domains)[:40]:
                try:
                    r = req.get(f'https://{d}/', headers=hd, proxies=proxies, timeout=8,
                               allow_redirects=True)
                    if 200 <= r.status_code < 400:
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
        self.jm_log_area.insert(tk.END, f'[{timestamp}] {msg}\n', tag)
        self.jm_log_area.see(tk.END)
        self.jm_log_area.configure(state=tk.DISABLED)

    def _open_dir(self):
        path = self.jm_dir_var.get()
        if os.path.exists(path):
            os.startfile(path)
        else:
            messagebox.showinfo('提示', '目录不存在，下载后自动创建')

    def _integrity_check(self):
        """扫描 JM 下载目录，找出损坏/空文件并删除，然后重新下载对应本子补全。"""
        base = Path(self.jm_dir_var.get())
        if not base.exists():
            messagebox.showinfo('提示', '下载目录不存在')
            return

        def run():
            try:
                verifier = IntegrityVerifier(str(base))
                issues = verifier.scan_tree(str(base))
                if issues:
                    self.gui.post(self._jm_log,
                                  f'[校验] 发现 {len(issues)} 个问题文件（损坏/空文件），准备修复', 'header')
                else:
                    self.gui.post(self._jm_log, '[校验] 扫描完成，未发现问题文件', 'success')
                    return
                from collections import defaultdict
                by_album = defaultdict(list)
                for path, issue in issues.items():
                    p = Path(path)
                    self.gui.post(self._jm_log, f'[校验] {issue}: {p.name}', 'warning')
                    rel = p.relative_to(base).parts
                    if len(rel) >= 2:
                        by_album[(rel[0], rel[1])].append(p)
                for files in by_album.values():
                    for p in files:
                        try:
                            p.unlink(missing_ok=True)
                        except OSError:
                            pass
                # 作者/标题 -> album id（依据下载缓存）
                # 目录名的作者部分是 authors[0]，缓存里 author 是逗号连接的全列表，
                # 因此用“目录作者 ∈ 缓存作者列表”匹配，兼容多作者本子
                index = self._get_downloaded_cache(str(base))
                gids = set()
                for (author, title), files in by_album.items():
                    for gid, e in index.items():
                        if not isinstance(e, dict) or e.get('title') != title:
                            continue
                        cache_author = str(e.get('author') or '')
                        authors = [a.strip() for a in cache_author.split(',') if a.strip()]
                        if author in authors or cache_author.startswith(author):
                            gids.add(str(gid))
                            break
                if not gids:
                    self.gui.post(self._jm_log, '[修复] 无法从缓存识别本子ID，请手动重新下载', 'warning')
                    return
                self.gui.post(self._jm_log,
                              f'[修复] 已删除 {len(issues)} 个问题文件，重新下载 {len(gids)} 本补全', 'info')
                self.gui.post(self._start_download_for, sorted(gids))
            except Exception as e:
                self.gui.post(self._jm_log, f'[校验] 异常: {e}', 'error')

        threading.Thread(target=run, daemon=True).start()

    def _start_download_for(self, gids):
        if self.running:
            self._jm_log('[提示] 已有任务运行，请稍后重试', 'warning')
            return
        self.jm_id_text.delete('1.0', tk.END)
        self.jm_id_text.insert('1.0', '\n'.join(gids))
        self._start_download()

    def _set_running(self, running):
        self.running = running
        self.gui.set_download_tab_running('jm', running)
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
        if self._anti_status_after_id:
            try:
                self.gui.root.after_cancel(self._anti_status_after_id)
            except tk.TclError:
                pass
            self._anti_status_after_id = None
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
        pause_text = '等待开' if snapshot.get('pause_enabled', True) else '等待关'
        cooldown = int(snapshot['site_cooldown'] + 0.999)
        cooldown_text = f'  冷却:{cooldown}s' if cooldown else ''
        self.jm_anti_req.config(
            text=f'{mode_text}  请求:{snapshot["requests"]}  并发:{snapshot["active"]}/{snapshot["limit"]}  熔断:{snapshot["open_routes"]}{cooldown_text}  {pause_text}')

        if self.running:
            self._anti_status_after_id = self.gui.root.after(1000, self._update_jm_anti_status)

    def _toggle_jm_browser_priority(self):
        self.scheduler.configure(browser_priority=self.jm_browser_priority_var.get())
        self._update_jm_anti_status()

    def _toggle_jm_pause(self):
        self.scheduler.configure(pause_enabled=self.jm_pause_var.get())
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
        username = config['username'] if 'username' in config else self.jm_username_var.get().strip()
        password = config['password'] if 'password' in config else self.jm_password_var.get().strip()
        self.scheduler.configure(max_concurrency=img_threads, proxies=proxy_pool,
                                 browser_priority=browser_priority,
                                 pause_enabled=self.jm_pause_var.get())

        if hasattr(self, '_jm_domains') and self._jm_domains and not (username and password):
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

        if username and password:
            if getattr(self, '_jm_domains', None):
                self.gui.post(self._jm_log, '检测到登录凭据，已禁用动态域名并使用内置信任域名', 'warning')
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
                # 索引条目里已含 gallery_id 字段，展开时去掉，避免与位置参数冲突
                data = {k: v for k, v in entry.items() if k != 'gallery_id'}
                update_download_index(output_dir, gid, **data)

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

    def _cache_expected_dir(self, entry, output_dir):
        """根据下载缓存里的 author/title 估算 JM 目录（Bd_Aauthor_Atitle_Pindex）。"""
        if not isinstance(entry, dict):
            return None
        title = str(entry.get('title') or '')
        if not title:
            return None
        author = (str(entry.get('author') or '')).split(',')[0].strip()
        return Path(output_dir) / sanitize_filename(author) / sanitize_filename(title)

    def _check_album_downloaded(self, aid, option, output_dir=None):
        cache = self._get_downloaded_cache(output_dir)
        entry = cache.get(str(aid), {}) if isinstance(cache, dict) else {}
        album = None
        expected_total = 0
        expected_dir = None
        try:
            cached_total = int(entry.get('total') or 0)
            if cached_total > 0:
                expected_dir = self._cache_expected_dir(entry, output_dir)
                expected_total = cached_total
            # 缓存信息不足或目录不存在时，请求详情并填充每章 page_arr（否则 len(photo) 会抛错）
            if expected_dir is None or not expected_dir.exists() or expected_total <= 0:
                client = option.new_jm_client()
                album = client.get_album_detail(aid)
                for photo in album:
                    client.check_photo(photo)
                expected_dir = self._build_expected_dir(album, option)
                expected_total = sum(len(photo) for photo in album)
            if expected_dir is None:
                return False
            valid_count = self._count_valid_images(expected_dir)
            title = album.name if album else str(entry.get('title') or aid)
            author = (', '.join(album.authors) if album and album.authors
                      else str(entry.get('author') or ''))
            if valid_count >= expected_total > 0:
                if entry.get('status') != 'complete':
                    self._mark_downloaded(aid, title, author, output_dir,
                                          status='complete', total=expected_total, missing=0)
                return True
            if entry.get('status') == 'complete':
                self._mark_downloaded(aid, title, author, output_dir, status='partial',
                                      total=expected_total,
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
        self._check_existing_after_id = None
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

    def _schedule_check_existing(self, _event=None):
        if self._check_existing_after_id:
            try:
                self.gui.root.after_cancel(self._check_existing_after_id)
            except tk.TclError:
                pass
        self._check_existing_after_id = self.gui.root.after(300, self._check_existing)

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

            def after_image(self, image, img_save_path):
                super().after_image(image, img_save_path)
                panel.gui.post(panel._jm_log,
                    f'    第{image.filename_without_suffix}张 完成')

            def after_photo(self, photo):
                super().after_photo(photo)
                panel.gui.post(panel._jm_log,
                    f'  [章节完成] {photo.id} ({photo.index}/{len(photo.from_album)})')

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
        header = ttk.Frame(self, style='Toolbar.TFrame', padding=(18, 14))
        header.pack(fill=tk.X, padx=12, pady=(12, 6))
        ttk.Label(header, text='每周必看 - JM Comic',
                  style='Toolbar.Section.TLabel').pack(side=tk.LEFT)
        self._status_lbl = ttk.Label(header, text='正在发现可用CDN...', style='Toolbar.Muted.TLabel')
        self._status_lbl.pack(side=tk.LEFT, padx=8)
        self._refresh_btn = ttk.Button(header, text='刷新内容', command=self.load_data)
        self._refresh_btn.pack(side=tk.RIGHT, padx=4)
        self._cover_refresh_btn = ttk.Button(header, text='补齐封面', command=self.refresh_covers)
        self._cover_refresh_btn.pack(side=tk.RIGHT, padx=4)
        self._cover_clear_btn = ttk.Button(header, text='清理缓存', command=self.clear_cover_cache)
        self._cover_clear_btn.pack(side=tk.RIGHT, padx=4)
        self._discover_btn = ttk.Button(header, text='发现域名', command=self._discover_domains)
        self._discover_btn.pack(side=tk.RIGHT, padx=4)

        sel_frame = ttk.Frame(self, padding=(18, 6))
        sel_frame.pack(fill=tk.X, padx=12)
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

        self._canvas = tk.Canvas(self, highlightthickness=0, bg=UI['bg'])
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
        self._current_type = ''
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
            self._selected_category = cat_map.get(cat_options[0], '')

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
        cid = str(comic.get('id', '') or '') if isinstance(comic, dict) else str(getattr(comic, 'id', '') or '')
        name = (comic.get('name') or '') if isinstance(comic, dict) else (getattr(comic, 'name', '') or '')
        author = (comic.get('author') or '') if isinstance(comic, dict) else (getattr(comic, 'author', '') or '')

        card = tk.Frame(self._inner, bg=UI['surface'], relief=tk.FLAT,
                        highlightthickness=1, highlightbackground=UI['border'],
                        width=222, height=370)
        card.grid(row=row, column=col, padx=7, pady=7, sticky='n')
        card.grid_propagate(False)

        cover_frame = tk.Frame(card, bg=UI['surface_alt'], width=204, height=270)
        cover_frame.pack(padx=8, pady=(8, 5))
        cover_frame.pack_propagate(False)
        thumb_label = tk.Label(cover_frame, text='正在整理封面...', bg=UI['surface_alt'],
                               font=FONT_SMALL, fg=UI['muted'])
        thumb_label.pack(fill=tk.BOTH, expand=True)

        display_name = name if name else f'JM{cid}'
        tk.Label(card, text=display_name, bg=UI['surface'], fg=UI['text'],
                 font=('Microsoft YaHei UI', 9, 'bold'), anchor=tk.W,
                 justify=tk.LEFT, wraplength=202, height=2).pack(fill=tk.X, padx=9)

        meta_frame = tk.Frame(card, bg=UI['surface'])
        meta_frame.pack(fill=tk.X, padx=9, pady=(2, 0))
        meta_text = author[:16] if author else f'JM{cid}'
        tk.Label(meta_frame, text=meta_text, bg=UI['surface'], fg=UI['muted'], anchor=tk.W,
                 font=('Microsoft YaHei UI', 7)).pack(side=tk.LEFT, fill=tk.X, expand=True)
        status_label = tk.Label(meta_frame, text='检查中', bg=UI['surface'], fg=UI['muted'],
                                font=('Microsoft YaHei UI', 7, 'bold'))
        status_label.pack(side=tk.RIGHT)

        btn_frame = tk.Frame(card, bg=UI['surface'])
        btn_frame.pack(fill=tk.X, padx=8, pady=(5, 7))
        dl_btn = tk.Label(btn_frame, text='加入队列', bg=UI['primary'], fg='white',
                           font=FONT_SMALL, cursor='hand2', pady=4)
        dl_btn.pack(fill=tk.X, expand=True)
        dl_btn.bind('<Button-1>', lambda e, cid=cid, nm=name[:20]: self._confirm_download(cid, nm))
        dl_btn.bind('<Enter>', lambda _e, w=dl_btn: w.config(bg=UI['primary_hover']))
        dl_btn.bind('<Leave>', lambda _e, w=dl_btn: w.config(bg=UI['primary']))

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
            if image.width * image.height > 50_000_000:
                return None
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
        self.gui.notebook.select(self.gui.jm_tab)
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
        toolbar = ttk.Frame(self, style='Toolbar.TFrame', padding=(16, 12))
        toolbar.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(toolbar, text='下载历史', style='Toolbar.Section.TLabel').pack(side=tk.LEFT)
        ttk.Label(toolbar, text='完成、缺失和失败的任务都会留在这里',
                  style='Toolbar.Muted.TLabel').pack(side=tk.LEFT, padx=(10, 0))
        self.filter_var = tk.StringVar(value='全部')
        ttk.Combobox(toolbar, textvariable=self.filter_var,
                     values=['全部', 'NHentai', 'JM Comic', '失败/缺失'],
                     state='readonly', width=11).pack(side=tk.LEFT, padx=12)
        self.filter_var.trace_add('write', lambda *_: self.refresh())
        ttk.Button(toolbar, text='刷新', command=self.refresh).pack(side=tk.RIGHT)
        ttk.Button(toolbar, text='导出', command=self.export_current).pack(side=tk.RIGHT, padx=6)
        ttk.Button(toolbar, text='删除所选', command=self.delete_selected,
                   style='Danger.TButton').pack(side=tk.RIGHT)
        ttk.Button(toolbar, text='打开目录', command=self.open_selected).pack(side=tk.RIGHT, padx=6)
        ttk.Button(toolbar, text='重试所选', command=self.retry_selected).pack(side=tk.RIGHT)

        columns = ('time', 'site', 'id', 'title', 'status', 'progress', 'reason')
        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill=tk.BOTH, expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=columns, show='headings', selectmode='browse',
                                 style='History.Treeview')
        headings = {'time': '时间', 'site': '来源', 'id': 'ID', 'title': '标题',
                    'status': '结果', 'progress': '完整度', 'reason': '失败归因'}
        widths = {'time': 145, 'site': 85, 'id': 90, 'title': 310,
                  'status': 70, 'progress': 85, 'reason': 360}
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], minwidth=60,
                             anchor=tk.CENTER if col in ('site', 'id', 'status', 'progress') else tk.W)
        scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        scroll_x = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=scroll_x.set)
        self.tree.grid(row=0, column=0, sticky=tk.NSEW)
        scroll.grid(row=0, column=1, sticky=tk.NS)
        scroll_x.grid(row=1, column=0, sticky=tk.EW)
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.tree.tag_configure('complete', foreground=UI['success'])
        self.tree.tag_configure('partial', foreground=UI['warning'])
        self.tree.tag_configure('failed', foreground=UI['danger'])
        self.tree.tag_configure('cancelled', foreground=UI['muted'])
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

    def delete_selected(self):
        record = self._selected_record()
        if not record:
            messagebox.showinfo('删除历史', '请先选择一条历史记录')
            return
        if not messagebox.askyesno(
                '删除历史', f'确认删除这条记录？\n\n{record.get("site", "")}  '
                f'{record.get("id", "")}  {record.get("title", "")}'):
            return

        def mutate(state):
            history = state.get('history', [])
            history_id = record.get('history_id')
            for index in range(len(history) - 1, -1, -1):
                candidate = history[index]
                if ((history_id and candidate.get('history_id') == history_id)
                        or (not history_id and candidate == record)):
                    history.pop(index)
                    return True
            return False

        removed = update_app_state(mutate)
        if removed:
            self.refresh()
            self.gui.status_var.set('已删除一条下载历史')
        else:
            messagebox.showinfo('删除历史', '该记录已经不存在')

    def export_current(self):
        records = list(self._records)
        if not records:
            messagebox.showinfo('导出历史', '当前筛选结果为空')
            return
        path = filedialog.asksaveasfilename(
            title='导出下载历史',
            defaultextension='.json',
            filetypes=[('JSON 文件', '*.json'), ('CSV 文件', '*.csv')],
            initialfile=f'下载历史_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json')
        if not path:
            return
        export_path = Path(path)
        if export_path.suffix.lower() not in ('.json', '.csv'):
            export_path = export_path.with_suffix('.json')
        try:
            if export_path.suffix.lower() == '.csv':
                fields = ('time', 'site', 'id', 'title', 'status', 'total',
                          'missing', 'path', 'failure_code', 'failure_reason', 'failure_detail')
                with open(export_path, 'w', encoding='utf-8-sig', newline='') as f:
                    writer = csv.DictWriter(f, fieldnames=fields)
                    writer.writeheader()
                    for record in records:
                        failure = record.get('failure') or {}
                        writer.writerow({
                            'time': record.get('time', ''), 'site': record.get('site', ''),
                            'id': record.get('id', ''), 'title': record.get('title', ''),
                            'status': record.get('status', ''), 'total': record.get('total', 0),
                            'missing': record.get('missing', 0), 'path': record.get('path', ''),
                            'failure_code': failure.get('code', ''),
                            'failure_reason': failure.get('reason', ''),
                            'failure_detail': failure.get('detail', ''),
                        })
            else:
                with open(export_path, 'w', encoding='utf-8') as f:
                    json.dump(records, f, ensure_ascii=False, indent=2)
            self.gui.status_var.set(f'已导出 {len(records)} 条历史记录')
            messagebox.showinfo('导出完成', f'已导出 {len(records)} 条记录到：\n{export_path}')
        except OSError as exc:
            messagebox.showerror('导出失败', str(exc))


# ==================== 配置档案 ====================
class ProfilePanel(ttk.Frame):
    def __init__(self, parent, gui):
        super().__init__(parent, padding=14)
        self.gui = gui
        self.setup_ui()
        self.refresh_profiles()

    def setup_ui(self):
        ttk.Label(self, text='工作方式', style='PageTitle.TLabel').pack(anchor=tk.W)
        ttk.Label(self, text='保存目录、代理、线程和重试策略；登录密码不会写入档案。',
                  style='Muted.TLabel').pack(anchor=tk.W, pady=(2, 12))

        manage = ttk.LabelFrame(self, text='配置档案', padding=12, style='Card.TLabelframe')
        manage.pack(fill=tk.X)
        self.name_var = tk.StringVar()
        self.combo = ttk.Combobox(manage, textvariable=self.name_var, width=30, state='normal')
        self.combo.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(manage, text='应用', command=self.apply_profile).pack(side=tk.LEFT, padx=3)
        ttk.Button(manage, text='保存当前配置', command=self.save_profile).pack(side=tk.LEFT, padx=3)
        ttk.Button(manage, text='删除', command=self.delete_profile).pack(side=tk.LEFT, padx=3)

        preview = ttk.LabelFrame(self, text='当前档案内容', padding=10, style='Card.TLabelframe')
        preview.pack(fill=tk.BOTH, expand=True, pady=(12, 0))
        self.preview = scrolledtext.ScrolledText(preview, state=tk.DISABLED, height=20,
                                                 font=('Cascadia Mono', 10), wrap=tk.WORD)
        self.preview.configure(bg=UI['surface_soft'], fg=UI['text'], relief=tk.FLAT,
                               highlightthickness=1, highlightbackground=UI['border'],
                               selectbackground=UI['primary_soft'], padx=10, pady=8)
        self.preview.pack(fill=tk.BOTH, expand=True)
        self.combo.bind('<<ComboboxSelected>>', lambda _e: self.show_preview())

    def _current_config(self):
        nh, jm = self.gui.nhentai_tab, self.gui.jm_tab
        return {
            'nh': {'proxy': self.gui.nh_proxy_var.get(), 'output': self.gui.nh_output_var.get(),
                   'quality': self.gui.nh_quality_var.get(), 'retry': self.gui.nh_retry_var.get(),
                   'workers': self.gui.nh_workers_var.get(), 'speed': self.gui.nh_speed_mode_var.get(),
                   'stealth': self.gui.nh_stealth_var.get(),
                   'browser_priority': self.gui.nh_browser_priority_var.get(),
                   'pause': self.gui.nh_pause_var.get()},
            'jm': {'proxy': jm.jm_proxy_var.get(), 'output': jm.jm_dir_var.get(),
                   'mode': jm.jm_mode_var.get(), 'image_threads': jm.jm_image_threads_var.get(),
                   'photo_threads': jm.jm_photo_threads_var.get(), 'retry': jm.jm_retry_var.get(),
                   'delay': jm.jm_delay_var.get(), 'antiblock': jm.jm_antiblock_var.get(),
                   'browser_priority': jm.jm_browser_priority_var.get(),
                   'pause': jm.jm_pause_var.get()},
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
            (self.gui.nh_pause_var, nh.get('pause')),
            (self.gui.jm_tab.jm_proxy_var, jm.get('proxy')),
            (self.gui.jm_tab.jm_dir_var, jm.get('output')), (self.gui.jm_tab.jm_mode_var, jm.get('mode')),
            (self.gui.jm_tab.jm_image_threads_var, jm.get('image_threads')),
            (self.gui.jm_tab.jm_photo_threads_var, jm.get('photo_threads')),
            (self.gui.jm_tab.jm_retry_var, jm.get('retry')), (self.gui.jm_tab.jm_delay_var, jm.get('delay')),
            (self.gui.jm_tab.jm_antiblock_var, jm.get('antiblock')),
            (self.gui.jm_tab.jm_browser_priority_var, jm.get('browser_priority')),
            (self.gui.jm_tab.jm_pause_var, jm.get('pause')),
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
        self.root.minsize(1180, 700)

        self.capsule = None
        self._cf_monitor_id = None
        self._closing = False
        self._close_deadline = 0.0
        self._close_poll_id = None
        self._ui_queue = queue.Queue()
        self._tab_indicator_images = {}

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
        try:
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
                except Exception as exc:
                    try:
                        self.nhentai_tab.log_text.configure(state=tk.NORMAL)
                        self.nhentai_tab.log_text.insert(
                            tk.END, f'[UI 回调异常] {type(exc).__name__}: {exc}\n', 'error')
                        self.nhentai_tab.log_text.configure(state=tk.DISABLED)
                    except Exception:
                        pass
        finally:
            self._ui_pump_id = self.root.after(30, self._drain_ui_queue)

    def on_close(self):
        if self._closing:
            return
        self._closing = True
        self._close_deadline = time.monotonic() + 8.0
        self.nhentai_tab._stop_requested = True
        if self.nhentai_tab.crawler:
            self.nhentai_tab.crawler.stop()
        self.jm_tab._jm_stop_event.set()
        if self.capsule:
            try:
                self.capsule.on_close()
            except Exception:
                self.capsule = None
        for after_id in (self._cf_monitor_id, getattr(self, '_ui_pump_id', None)):
            if after_id:
                try:
                    self.root.after_cancel(after_id)
                except tk.TclError:
                    pass
        try:
            self.root.title('正在安全退出...')
        except tk.TclError:
            pass
        self._wait_for_workers_before_close()

    def _active_download_threads(self):
        threads = []
        for thread in (
                getattr(self.nhentai_tab, 'download_thread', None),
                getattr(self.jm_tab, '_jm_thread', None)):
            if thread is not None and thread.is_alive():
                threads.append(thread)
        return threads

    def _wait_for_workers_before_close(self):
        if self._active_download_threads() and time.monotonic() < self._close_deadline:
            try:
                self._close_poll_id = self.root.after(100, self._wait_for_workers_before_close)
                return
            except tk.TclError:
                pass
        self._finalize_close()

    def _finalize_close(self):
        self._close_poll_id = None
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def setup_ui(self):
        style = ttk.Style(self.root)
        if 'clam' in style.theme_names():
            style.theme_use('clam')
        self.root.configure(bg=UI['bg'])
        self._configure_styles(style)

        # Notebook 标签栏
        self.notebook = ttk.Notebook(self.root, style='App.TNotebook')
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=14, pady=(12, 6))

        # NHentai 主页面与附属合集
        self._build_tab_indicator_images()
        self.nhentai_tab = NHentaiPanel(self.notebook, self)
        self.notebook.add(self.nhentai_tab, text='  NHentai  ',
                          image=self._tab_indicator_images['idle'], compound=tk.LEFT)

        self.collection_tab = JMCollectionPanel(self.notebook, self)
        self.notebook.add(self.collection_tab, text='合集', padding=(8, 6))

        # JM Comic 主页面与附属每周必看
        self.jm_tab = JMComicPanel(self.notebook, self)
        self.notebook.add(self.jm_tab, text='  JM Comic  ',
                          image=self._tab_indicator_images['idle'], compound=tk.LEFT)

        self.weekly_tab = WeeklyPanel(self.notebook, self)
        self.notebook.add(self.weekly_tab, text='每周必看', padding=(8, 6))

        self.history_tab = HistoryPanel(self.notebook, self)
        self.notebook.add(self.history_tab, text='  下载历史  ')

        self.profile_tab = ProfilePanel(self.notebook, self)
        self.notebook.add(self.profile_tab, text='  配置档案  ')

        # 底部状态栏
        status_frame = ttk.Frame(self.root, style='StatusBar.TFrame', padding=(16, 7))
        status_frame.pack(fill=tk.X, padx=14, pady=(0, 10))

        self.status_var = tk.StringVar(value='就绪')
        ttk.Label(status_frame, text='\u25cf', foreground=UI['success'],
                  style='StatusBar.TLabel').pack(side=tk.LEFT, padx=(0, 6))
        ttk.Label(status_frame, textvariable=self.status_var,
                  style='StatusBar.TLabel').pack(side=tk.LEFT)

        ttk.Label(status_frame, text=f'合集: {get_collection_desc()}',
                  style='StatusBar.TLabel').pack(side=tk.RIGHT)

    def _build_tab_indicator_images(self):
        for name, color in (('idle', '#7F313B'), ('running', '#42D392')):
            image = tk.PhotoImage(width=10, height=10)
            image.put(UI['bg'], to=(0, 0, 10, 10))
            rows = {
                1: (4, 6), 2: (2, 8), 3: (1, 9), 4: (1, 9),
                5: (1, 9), 6: (1, 9), 7: (2, 8), 8: (4, 6),
            }
            for y, (start, end) in rows.items():
                image.put(color, to=(start, y, end, y + 1))
            self._tab_indicator_images[name] = image

    def set_download_tab_running(self, engine, running):
        if self._closing:
            return
        tab = self.nhentai_tab if engine == 'nh' else self.jm_tab
        try:
            self.notebook.tab(
                tab, image=self._tab_indicator_images['running' if running else 'idle'])
        except tk.TclError:
            pass

    @staticmethod
    def _configure_styles(style):
        style.configure('.', font=FONT_UI, background=UI['bg'], foreground=UI['text'])
        style.configure('TFrame', background=UI['bg'])
        style.configure('Toolbar.TFrame', background=UI['surface'])
        style.configure('StatusBar.TFrame', background=UI['surface'])
        style.configure('TLabel', background=UI['bg'], foreground=UI['text'])
        style.configure('Muted.TLabel', foreground=UI['muted'], background=UI['bg'], font=FONT_SMALL)
        style.configure('Section.TLabel', foreground=UI['text'], background=UI['bg'], font=FONT_SECTION)
        style.configure('Toolbar.Muted.TLabel', foreground=UI['muted'], background=UI['surface'],
                        font=FONT_SMALL)
        style.configure('Toolbar.Section.TLabel', foreground=UI['text'], background=UI['surface'],
                        font=FONT_SECTION)
        style.configure('PageTitle.TLabel', foreground=UI['text'], background=UI['bg'],
                        font=('Microsoft YaHei UI', 18, 'bold'))
        style.configure('Pill.TLabel', foreground=UI['primary'], background=UI['primary_soft'],
                        padding=(10, 4), font=('Cascadia Mono', 8, 'bold'))
        style.configure('StatusBar.TLabel', foreground=UI['muted'], background=UI['surface'], font=FONT_SMALL)

        style.configure('TButton', padding=(10, 6), background=UI['surface_alt'],
                        foreground=UI['text'], borderwidth=0, focusthickness=0)
        style.map('TButton', background=[('active', '#E1E4EB'), ('pressed', '#D8DBE4')],
                  foreground=[('disabled', '#A4A7AF')])
        style.configure('Accent.TButton', background=UI['primary'], foreground='white',
                        padding=(14, 7), font=('Microsoft YaHei UI', 9, 'bold'))
        style.map('Accent.TButton', background=[('active', UI['primary_hover']),
                                               ('pressed', '#4E5FCB'),
                                               ('disabled', '#B8BEE5')],
                  foreground=[('disabled', '#F1F2FA')])
        style.configure('Danger.TButton', background='#FBEAEC', foreground=UI['danger'],
                        padding=(12, 7), font=('Microsoft YaHei UI', 9, 'bold'))
        style.map('Danger.TButton', background=[('active', '#F6DADD'),
                                               ('pressed', '#F1CDD1')])

        style.configure('Card.TLabelframe', background=UI['surface'], bordercolor=UI['border'],
                        relief='solid', borderwidth=1)
        style.configure('Card.TLabelframe.Label', background=UI['surface'], foreground=UI['text'],
                        font=('Microsoft YaHei UI', 9, 'bold'))
        style.configure('TLabelframe', background=UI['surface'], bordercolor=UI['border'])
        style.configure('TLabelframe.Label', background=UI['surface'], foreground=UI['text'],
                        font=('Microsoft YaHei UI', 9, 'bold'))

        style.configure('App.TNotebook', background=UI['bg'], borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure('App.TNotebook.Tab', background=UI['surface_alt'], foreground=UI['muted'],
                        padding=(18, 9), borderwidth=0, font=('Microsoft YaHei UI', 9, 'bold'))
        style.map('App.TNotebook.Tab', background=[('selected', UI['surface']),
                                                  ('active', UI['primary_soft'])],
                  foreground=[('selected', UI['primary']), ('active', UI['primary'])])

        for tree_style in ('Treeview', 'Files.Treeview', 'History.Treeview'):
            style.configure(tree_style, background=UI['surface'], fieldbackground=UI['surface'],
                            foreground=UI['text'], rowheight=29, borderwidth=0, font=FONT_UI)
            style.map(tree_style, background=[('selected', UI['primary_soft'])],
                      foreground=[('selected', UI['primary'])])
        style.configure('Treeview.Heading', background=UI['surface_alt'], foreground=UI['muted'],
                        relief='flat', padding=(8, 7), font=('Microsoft YaHei UI', 8, 'bold'))
        style.map('Treeview.Heading', background=[('active', '#E2E5ED')])

        style.configure('Quiet.Horizontal.TProgressbar', troughcolor=UI['surface_alt'],
                        background=UI['primary'], bordercolor=UI['surface_alt'],
                        lightcolor=UI['primary'], darkcolor=UI['primary'], thickness=8)
        style.configure('TEntry', fieldbackground=UI['surface_soft'], foreground=UI['text'],
                        bordercolor=UI['border'], padding=5)
        style.configure('TCombobox', fieldbackground=UI['surface_soft'], foreground=UI['text'],
                        bordercolor=UI['border'], padding=4)
        style.configure('TSpinbox', fieldbackground=UI['surface_soft'], foreground=UI['text'],
                        bordercolor=UI['border'], padding=4)

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
    root.after(550, app.nhentai_tab.refresh_covers)

    root.mainloop()


if __name__ == '__main__':
    main()
