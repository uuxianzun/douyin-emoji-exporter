"""
抖音表情抓取模块

采用「半自动」策略：
- 用 Playwright 打开真实浏览器窗口，让用户自己扫码登录
- 登录态持久化到 browser_profile，下次免登录
- 用户手动打开私信表情面板后，脚本遍历所有 frame 读取 img 标签
- 通过 URL 特征识别表情，自动排除头像等噪音

为什么不走纯协议（a_bogus 签名）：
签名算法随抖音前端迭代而失效，维护成本高且不可控；
半自动方案不碰风控，长期可用性最好。

【重要架构约束】
Playwright 的同步 API 有线程亲和性：上下文必须在创建它的线程里操作，
否则会抛 TargetClosedError。而 FastAPI 的同步接口跑在线程池里，
每次调用可能落到不同线程。因此这里用「专用工作线程 + 任务队列」，
把所有浏览器操作串行化到同一条线程上。
"""
import queue
import threading
import time
from typing import Callable

from .config import DOUYIN_HOME, PROFILE_DIR

# 在页面里执行的提取脚本：遍历所有 img，按 URL 特征筛表情
#
# 识别逻辑说明：
# 1. 抖音「收藏表情」的 URL 域名是 *-im-emoticon-sign.byteimg.com，
#    其中的 im-emoticon 是强特征，命中即判定为表情
# 2. 通用关键词命中作为兜底
# 3. 必须排除的噪音：
#    - twemoji / unicode emoji（如 twemoji/72x72/26f0.png）是标准 Unicode 表情图标，
#      不是用户收藏的自定义表情，但在 URL 里含 emoji 关键词，会误命中
#    - 头像、封面、视频封面等
EXTRACT_JS = """
(() => {
    const out = [];
    const seen = new Set();
    const keywords = ['emoticon', 'emotion', 'sticker', 'emoji', 'expression'];

    // 噪音排除：命中任一即丢弃
    const badWords = [
        'avatar', 'user_avatar', 'profile', 'cover', 'poster',
        'twemoji', 'unicode', '/emoji/', 'emoji-sprite'
    ];

    for (const img of document.querySelectorAll('img')) {
        const src = img.src || img.getAttribute('data-src') || '';
        if (!src || !src.startsWith('http')) continue;
        if (seen.has(src)) continue;

        const low = src.toLowerCase();
        if (badWords.some(b => low.includes(b))) continue;

        // 强特征：抖音 IM 表情专用域名
        const isImEmoticon = low.includes('im-emoticon') || low.includes('im_emoticon');

        // 兜底：通用关键词
        const hitKeyword = keywords.some(k => low.includes(k));

        if (!isImEmoticon && !hitKeyword) continue;

        seen.add(src);
        out.push({
            src: src,
            w: img.naturalWidth || 0,
            h: img.naturalHeight || 0,
            strong: isImEmoticon
        });
    }
    return out;
})()
"""


class BrowserWorker:
    """
    专用浏览器工作线程。

    所有 Playwright 操作通过 submit() 投递到这条线程串行执行，
    保证上下文与线程绑定的正确性。
    """

    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._pw = None
        self._context = None
        self._ready = threading.Event()
        self._start_lock = threading.Lock()

    # ---------- 线程生命周期 ----------

    def ensure_started(self):
        """惰性启动工作线程"""
        with self._start_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()
                self._ready.wait(timeout=10)

    def _run(self):
        """工作线程主循环"""
        import asyncio
        from playwright.sync_api import sync_playwright

        # 每条线程独立的事件循环，避免与 FastAPI 的 loop 冲突
        asyncio.set_event_loop(asyncio.new_event_loop())
        try:
            self._pw = sync_playwright().start()
        except Exception as e:
            self._ready.set()
            print(f"[browser] Playwright 启动失败: {e}")
            return

        self._ready.set()

        while True:
            task = self._queue.get()
            if task is None:  # 退出信号
                break
            fn, args, kwargs, result_box = task
            try:
                result_box["result"] = fn(*args, **kwargs)
            except Exception as e:
                result_box["error"] = e
            finally:
                result_box["done"].set()

        self._cleanup()

    def _cleanup(self):
        try:
            if self._context:
                self._context.close()
        except Exception:
            pass
        self._context = None
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = None

    def submit(self, fn, *args, timeout=120, **kwargs):
        """把操作投递到工作线程执行，阻塞等待结果"""
        self.ensure_started()
        box = {"done": threading.Event(), "result": None, "error": None}
        self._queue.put((fn, args, kwargs, box))
        if not box["done"].wait(timeout=timeout):
            raise TimeoutError("浏览器操作超时")
        if box["error"]:
            raise box["error"]
        return box["result"]

    # ---------- 实际业务操作（均在工作线程内执行） ----------

    def _do_open(self, visible=True):
        if self._context is None:
            self._context = self._pw.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=not visible,
                viewport={"width": 1280, "height": 860},
                args=["--disable-blink-features=AutomationControlled"],
            )
        pages = self._context.pages
        page = pages[0] if pages else self._context.new_page()
        try:
            if "douyin.com" not in (page.url or ""):
                page.goto(DOUYIN_HOME, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        try:
            page.bring_to_front()
        except Exception:
            pass
        return True

    def _do_check_login(self):
        if self._context is None:
            return False
        try:
            cookies = self._context.cookies()
        except Exception:
            # 上下文已失效，重置以便下次重新拉起
            self._context = None
            return False
        return any(
            c.get("name") == "sessionid" and c.get("value") for c in cookies
        )

    def _do_extract(self, scroll_rounds, on_progress):
        log = on_progress or (lambda m: None)

        if self._context is None or not self._context.pages:
            self._do_open(visible=True)
        pages = [
            p for p in self._context.pages
            if "douyin.com" in (p.url or "")
        ]
        page = pages[0] if pages else self._context.pages[0]

        log("正在滚动加载表情面板，请稍候...")

        # 自适应滚动：每滚一轮数一下当前页面上的 img 数量，
        # 如果连续两轮数量不再增加，说明已经到底了，提前收工。
        # 这样表情少的时候（比如十几张）不用傻等固定的 8 轮。
        prev_count = -1
        still_rounds = 0
        for i in range(scroll_rounds):
            try:
                page.mouse.move(640, 430)
                page.mouse.wheel(0, 600)
            except Exception:
                pass
            # 等待新内容渲染；用 0.25 秒 + 一次快速探测，比固定 0.4 秒快
            time.sleep(0.25)
            try:
                cnt = page.evaluate(
                    "() => document.querySelectorAll('img').length"
                )
            except Exception:
                cnt = prev_count
            if cnt <= prev_count:
                still_rounds += 1
                if still_rounds >= 2:
                    log(f"已滚动 {i + 1} 轮，内容不再增加，停止加载")
                    break
            else:
                still_rounds = 0
            prev_count = cnt

        # 回到顶部（读图不依赖位置，这步只为视觉上复位）
        try:
            page.mouse.wheel(0, -scroll_rounds * 600)
        except Exception:
            pass
        time.sleep(0.3)

        log("正在提取表情链接...")
        collected: dict[str, dict] = {}
        targets = [page] + [p for p in self._context.pages if p is not page]
        for p in targets:
            for frame in p.frames:
                try:
                    items = frame.evaluate(EXTRACT_JS)
                except Exception:
                    continue
                for it in items or []:
                    src = it.get("src")
                    if src and src not in collected:
                        collected[src] = {
                            "url": src,
                            "width": it.get("w", 0),
                            "height": it.get("h", 0),
                        }

        log(f"提取完成，共发现 {len(collected)} 个表情")
        return list(collected.values())

    def _do_fetch_via_browser(self, urls: list[str], on_progress=None):
        """
        在浏览器上下文里直接抓取图片字节。

        为什么必须这样做：
        抖音表情 URL 是带 x-signature/x-expires 的**临时签名链接**，
        脱离浏览器上下文用 requests 去拉，很容易 403（签名与会话绑定 + 有时效）。
        而在页面内用 fetch 走浏览器自己的网络栈，天然带上正确 Cookie 和 Referer，
        是最可靠的取图方式。
        """
        log = on_progress or (lambda m: None)
        if self._context is None or not self._context.pages:
            raise RuntimeError("浏览器未启动")

        pages = [p for p in self._context.pages if "douyin.com" in (p.url or "")]
        page = pages[0] if pages else self._context.pages[0]

        # 在页面内并发抓取。
        # 原来是一个一个 await（串行），17 张要等十几秒；
        # 改成 Promise.all 并发，同样的图通常 2-4 秒就能拿完。
        # base64 转换放主线程串行做，避免一次性占满内存。
        js = """
        async (urls) => {
            const one = async (u) => {
                try {
                    const resp = await fetch(u, { credentials: 'include' });
                    if (!resp.ok) return { url: u, ok: false, status: resp.status };
                    const buf = await resp.arrayBuffer();
                    return { url: u, ok: true, buf: buf };
                } catch (e) {
                    return { url: u, ok: false, error: String(e) };
                }
            };
            const raw = await Promise.all(urls.map(one));
            const results = [];
            for (const r of raw) {
                if (!r.ok) { results.push(r); continue; }
                try {
                    const bytes = new Uint8Array(r.buf);
                    let bin = '';
                    const chunk = 0x8000;
                    for (let i = 0; i < bytes.length; i += chunk) {
                        bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
                    }
                    results.push({ url: r.url, ok: true, data: btoa(bin) });
                } catch (e) {
                    results.push({ url: r.url, ok: false, error: String(e) });
                }
            }
            return results;
        }
        """

        out = []
        # 每批大一点 + 批内并发，减少往返次数
        batch_size = 12
        for i in range(0, len(urls), batch_size):
            batch = urls[i:i + batch_size]
            try:
                res = page.evaluate(js, batch)
            except Exception as e:
                log(f"批次抓取异常：{e}")
                res = []
            for r in res or []:
                out.append(r)
            log(f"已获取 {len(out)}/{len(urls)} 个表情原始数据")
        return out

    def fetch_via_browser(self, urls, on_progress=None):
        return self.submit(
            self._do_fetch_via_browser, list(urls), on_progress, timeout=900
        )

    def _do_close(self):
        self._cleanup()
        return True

    # ---------- 对外接口（线程安全） ----------

    def open_douyin(self):
        return self.submit(self._do_open, visible=True, timeout=90)

    def check_login(self) -> bool:
        try:
            return bool(self.submit(self._do_check_login, timeout=20))
        except Exception:
            return False

    def extract(self, scroll_rounds=15, on_progress=None):
        return self.submit(
            self._do_extract, scroll_rounds, on_progress, timeout=600
        )

    def close(self):
        try:
            self.submit(self._do_close, timeout=15)
        except Exception:
            pass


# 全局单例
_worker: BrowserWorker | None = None
_worker_lock = threading.Lock()


def get_worker() -> BrowserWorker:
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = BrowserWorker()
        return _worker
