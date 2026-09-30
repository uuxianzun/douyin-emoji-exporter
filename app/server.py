"""
抖音表情包导出工具 - 后端服务

接口设计：
  GET  /                    -> 前端界面
  GET  /api/status          -> 登录状态、已下载数量
  POST /api/login/start     -> 启动浏览器让用户扫码登录
  POST /api/scan            -> 抓取表情列表
  GET  /api/thumb           -> 获取表情缩略图（用于网格预览）
  POST /api/download        -> 下载勾选的表情，返回 zip
  GET  /api/session         -> 查询当前抓取到的表情缓存
"""
import io
import json
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .core import downloader
from .core.config import PORT, HOST, CACHE_DIR, OUTPUT_DIR, DOWNLOADED_FILE
from .core.converter import make_thumbnail, safe_filename
from .core.scraper import get_worker

app = FastAPI(title="抖音表情包导出工具")

STATIC_DIR = Path(__file__).parent / "static"

# 抓取结果缓存（内存），key = emoji id
_scanned: dict[str, dict] = {}
_scan_lock = threading.Lock()
_scan_state = {"running": False, "message": "", "count": 0}
_progress_state = {"done": 0, "total": 0, "ok": 0, "fail": 0}


def emoji_id(url: str) -> str:
    """从 URL 生成稳定 ID，用于去重和记录"""
    base = url.split("?")[0]
    return base.rsplit("/", 1)[-1] or base


def cache_key(url: str) -> str:
    """
    URL -> 安全的缓存文件名。

    必须做两件事：
      1. 剥掉查询串（去掉 ?x-expires=...&x-signature=...）
         —— 抖音的签名参数每次刷新都会变，如果把它算进哈希，
            同一个表情每次刷新都是新的 key，缓存永远命中不了，
            表现为"这次显示签名过期，刷新一下又好了"。
      2. 哈希。因为抖音 URL 含 ':'（如 resize:0:0），
         这个字符在 Windows 上非法，直接用会导致 Permission denied。
    """
    import hashlib
    clean = url.split("?")[0]   # 去掉签名查询串，保证同一张图 key 稳定
    return hashlib.md5(clean.encode("utf-8")).hexdigest()[:20]


RAW_CACHE = CACHE_DIR / "raw"
RAW_CACHE.mkdir(parents=True, exist_ok=True)


# ---------------- 页面 ----------------

@app.get("/", response_class=HTMLResponse)
def index():
    fp = STATIC_DIR / "index.html"
    if not fp.exists():
        return HTMLResponse("<h1>前端文件缺失</h1>", status_code=500)
    return HTMLResponse(fp.read_text(encoding="utf-8"))


# ---------------- 状态 ----------------

@app.get("/api/status")
def status():
    return {
        "logged_in": get_worker().check_login(),
        "scanned_count": len(_scanned),
        "scan_running": _scan_state["running"],
        "scan_message": _scan_state["message"],
        "downloaded_count": len(downloader._load_downloaded()),
    }


@app.get("/api/progress")
def progress():
    return _progress_state


# ---------------- 登录 ----------------

@app.post("/api/login/start")
def login_start():
    """打开浏览器窗口，让用户扫码登录"""
    try:
        get_worker().open_douyin()
    except Exception as e:
        raise HTTPException(500, f"浏览器启动失败：{e}")
    return {"ok": True, "message": "浏览器已打开，请在窗口内扫码登录"}


@app.get("/api/login/check")
def login_check():
    return {"logged_in": get_worker().check_login()}


# ---------------- 抓取 ----------------

@app.post("/api/scan")
def scan(scroll_rounds: int = Query(15, ge=1, le=60)):
    """
    抓取表情列表。因为要操作浏览器，放在后台线程执行，前端轮询进度。

    scroll_rounds 是滚动上限；实际滚动会在"内容不再增加"时提前停止，
    所以这个值给大一点没坏处（表情多的用户也不会漏），表情少的会自己早点收工。
    """
    if _scan_state["running"]:
        return {"ok": False, "message": "正在抓取中，请稍候"}

    def worker():
        _scan_state.update(running=True, message="准备中...", count=0)
        try:
            items = get_worker().extract(
                scroll_rounds=scroll_rounds,
                on_progress=lambda m: _scan_state.update(message=m),
            )
            with _scan_lock:
                _scanned.clear()
                for it in items:
                    _scanned[emoji_id(it["url"])] = it
            _scan_state.update(count=len(_scanned), message="抓取完成")

            # 关键步骤：立即通过浏览器把原图抓到本地缓存。
            # 抖音的表情 URL 是临时签名链接，必须趁新鲜时取下来。
            if items:
                _scan_state.update(message="正在缓存表情原图...")
                prefetch_raw([it["url"] for it in items],
                             on_progress=lambda m: _scan_state.update(message=m))
                _scan_state.update(message="抓取完成")
        except Exception as e:
            _scan_state.update(message=f"抓取出错：{e}")
        finally:
            _scan_state["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "message": "已开始抓取"}


def prefetch_raw(urls: list[str], on_progress=None):
    """
    通过浏览器上下文批量抓取原图并写入本地缓存。

    价值：抖音表情 URL 是临时签名链接（含 x-signature / x-expires），
    脱离浏览器用 requests 拉很容易 403。趁抓取瞬间在页面内 fetch 下来，
    后续预览和下载都从本地读，彻底摆脱签名时效问题。

    会做一次重试：并发抓取时偶发的网络抖动/瞬时失败，第二轮单独补一次，
    能显著降低"抓完仍有个别图片显示签名过期"的概率。
    """
    import base64

    log = on_progress or (lambda m: None)
    todo = [u for u in urls if not (RAW_CACHE / cache_key(u)).exists()]
    if not todo:
        log(f"原图已全部缓存（{len(urls)} 个）")
        return

    log(f"正在缓存 {len(todo)} 个表情原图...")

    def _fetch_and_store(batch: list[str]) -> int:
        try:
            results = get_worker().fetch_via_browser(batch, on_progress=log)
        except Exception as e:
            log(f"缓存失败：{e}")
            return 0
        got = 0
        for r in results:
            if r.get("ok") and r.get("data"):
                try:
                    data = base64.b64decode(r["data"])
                    if len(data) > 100:
                        (RAW_CACHE / cache_key(r["url"])).write_bytes(data)
                        got += 1
                except Exception:
                    continue
        return got

    got = _fetch_and_store(todo)

    # 补漏：把仍没缓存的单独再试一次（并发下的瞬时失败很常见）
    missing = [u for u in todo if not (RAW_CACHE / cache_key(u)).exists()]
    if missing:
        log(f"有 {len(missing)} 个未缓存成功，正在重试...")
        time.sleep(0.6)
        got += _fetch_and_store(missing)

    log(f"原图缓存完成：{got}/{len(todo)}")


def _load_raw(url: str):
    """读取本地原图缓存，没有则返回 None"""
    fp = RAW_CACHE / cache_key(url)
    if fp.exists():
        try:
            return fp.read_bytes()
        except OSError:
            return None
    return None

@app.get("/api/session")
def get_session():
    """返回抓取到的表情列表 + 是否已下载过的标记"""
    done = downloader._load_downloaded()
    items = []
    for eid, it in _scanned.items():
        items.append({
            "id": eid,
            "url": it["url"],
            "downloaded": it["url"] in done,
            "width": it.get("width", 0),
            "height": it.get("height", 0),
        })
    return {"items": items, "total": len(items)}


# ---------------- 缩略图 ----------------

@app.get("/api/thumb")
def thumb(url: str = Query(...)):
    """
    缩略图：本地原图缓存 -> 回源 -> 占位图。

    缓存命中率高的关键是 cache_key 必须与签名参数无关（见 cache_key 注释），
    否则每次刷新都算成新图，缓存形同虚设。
    """
    import requests as rq

    key = cache_key(url)
    thumb_file = CACHE_DIR / f"{key}.thumb"

    if thumb_file.exists():
        return _png_response(thumb_file.read_bytes())

    # 优先用抓取时缓存的原始图
    raw_file = RAW_CACHE / key
    if raw_file.exists():
        try:
            data, fmt = make_thumbnail(raw_file.read_bytes())
            try:
                thumb_file.write_bytes(data)
            except OSError:
                pass
            return _png_response(data)
        except Exception:
            pass

    # 本地没有：先尝试让浏览器重新取一次（能拿到新鲜的签名链接）
    if _refresh_raw(url):
        raw_file = RAW_CACHE / key
        if raw_file.exists():
            try:
                data, fmt = make_thumbnail(raw_file.read_bytes())
                try:
                    thumb_file.write_bytes(data)
                except OSError:
                    pass
                return _png_response(data)
            except Exception:
                pass

    # 最后才用 requests 直接回源（签名失效时多半会失败）
    try:
        r = rq.get(url, headers=downloader.HEADERS, timeout=25)
        r.raise_for_status()
        data, fmt = make_thumbnail(r.content)
        try:
            thumb_file.write_bytes(data)
        except OSError:
            pass
        return _png_response(data)
    except Exception:
        return _placeholder_response()


def _refresh_raw(url: str) -> bool:
    """
    本地缓存缺失时，让浏览器重新抓一次原图（会顺带拿到新的签名链接）。

    这是"刷新页面图片又出现"的正解：图片本身没坏，只是签名过期了。
    只要浏览器还在，就请它再取一份并落到本地缓存，之后就一直能用了。
    """
    key = cache_key(url)
    if (RAW_CACHE / key).exists():
        return True
    try:
        results = get_worker().fetch_via_browser([url])
    except Exception:
        return False
    import base64
    for r in results or []:
        if r.get("ok") and r.get("data"):
            try:
                data = base64.b64decode(r["data"])
                if len(data) > 100:
                    (RAW_CACHE / cache_key(r["url"])).write_bytes(data)
                    return True
            except Exception:
                continue
    return False


def _png_response(data: bytes) -> Response:
    media = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    return Response(content=data, media_type=media,
                    headers={"Cache-Control": "public, max-age=86400"})


_PLACEHOLDER_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120">'
    '<rect width="120" height="120" fill="#24262f"/>'
    '<text x="60" y="58" font-size="13" fill="#8b8e9c" '
    'text-anchor="middle" font-family="sans-serif">签名已过期</text>'
    '<text x="60" y="76" font-size="11" fill="#5f6270" '
    'text-anchor="middle" font-family="sans-serif">请重新抓取</text></svg>'
)


def _placeholder_response() -> Response:
    """回源失败时给个占位图，避免前端一整片空白"""
    return Response(content=_PLACEHOLDER_SVG.encode("utf-8"),
                    media_type="image/svg+xml")


# ---------------- 下载 ----------------

class DownloadReq(BaseModel):
    urls: list[str]
    force: bool = False   # True = 忽略已下载记录，强制重新下载
    mode: str = "zip"     # "zip" = 打包下载；"single" = 直接下载单张图片


@app.post("/api/download")
def download(req: DownloadReq):
    """
    下载勾选的表情。

    mode="zip"    打包成 zip 返回（批量场景）
    mode="single" 直接返回单张图片字节流（保存为文件，不打包）

    前端在做"多张逐张下载"时，会对每一张分别发一次 mode="single" 请求，
    所以这里不对数量做限制。
    """
    if not req.urls:
        raise HTTPException(400, "没有选择任何表情")

    urls = req.urls

    # 单张模式：直接返回图片，不走 zip，也不做增量过滤
    if req.mode == "single":
        if len(urls) != 1:
            raise HTTPException(400, "直接下载模式一次只能处理一张图片")
        return _download_single(urls[0])

    if not req.force:
        urls, skipped = downloader.filter_new(urls)
        if not urls:
            raise HTTPException(400, f"选中的 {len(skipped)} 个表情都已下载过")

    _progress_state.update(done=0, total=len(urls), ok=0, fail=0)

    # 尽量从本地原图缓存取，命中率高时几乎不依赖网络
    missing = [u for u in urls if not (RAW_CACHE / cache_key(u)).exists()]
    if missing:
        try:
            prefetch_raw(missing)
        except Exception:
            pass

    def on_progress(done, total, ok, fail):
        _progress_state.update(done=done, total=total, ok=ok, fail=fail)

    result = downloader.download_batch(
        urls,
        on_progress=on_progress,
        local_loader=lambda u: _load_raw(u),
    )

    ok_items = [r for r in result["items"] if r["ok"]]
    downloader.mark_downloaded([r["url"] for r in ok_items])

    # 同时落盘一份，方便本地直接查看
    downloader.save_to_disk(ok_items, OUTPUT_DIR)

    zip_bytes = downloader.build_zip(ok_items)
    filename = f"douyin_emoji_{time.strftime('%Y%m%d_%H%M%S')}.zip"

    # 把统计放进响应头，前端可读
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Ok-Count": str(result["ok"]),
        "X-Fail-Count": str(result["fail"]),
        "Access-Control-Expose-Headers": "X-Ok-Count, X-Fail-Count, Content-Disposition",
    }
    return Response(content=zip_bytes, media_type="application/zip", headers=headers)


# ---------------- 杂项 ----------------

@app.post("/api/reset-records")
def reset_records():
    """清空已下载记录，下次可全量导出"""
    DOWNLOADED_FILE.write_text("[]", encoding="utf-8")
    return {"ok": True}


def _download_single(url: str):
    """
    下载单张表情，直接返回图片字节流（不打包 zip）。

    用于「只想存这一张」的场景：浏览器会直接存成一个图片文件，
    比先下 zip 再解压方便得多。
    """
    from .core.converter import convert_for_wechat

    raw = _load_raw(url)
    if not raw:
        # 本地没有就现取一次
        try:
            prefetch_raw([url])
            raw = _load_raw(url)
        except Exception:
            pass

    if not raw:
        try:
            import requests as rq
            r = rq.get(url, headers=downloader.HEADERS, timeout=25)
            r.raise_for_status()
            raw = r.content
        except Exception as e:
            raise HTTPException(500, f"获取图片失败：{type(e).__name__}")

    content, ext = convert_for_wechat(raw)
    downloader.mark_downloaded([url])

    # 文件名用内容哈希，避免重名又便于识别
    import hashlib
    name = f"emoji_{hashlib.md5(url.encode()).hexdigest()[:8]}.{ext}"
    media = "image/gif" if ext == "gif" else "image/png"

    return Response(
        content=content,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Ok-Count": "1",
            "X-Fail-Count": "0",
            "Access-Control-Expose-Headers":
                "X-Ok-Count, X-Fail-Count, Content-Disposition",
        },
    )


@app.on_event("shutdown")
def _shutdown():
    get_worker().close()


def main():
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
