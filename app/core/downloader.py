"""
下载与打包模块

- 下载表情原图，转成微信可用格式
- 记录已下载的表情，支持增量导出（第二次只下新增的）
- 打包 zip 供下载
"""
import io
import json
import zipfile
from pathlib import Path

import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import DOWNLOADED_FILE, CACHE_DIR
from .converter import convert_for_wechat, safe_filename

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.douyin.com/",
}
TIMEOUT = 30


def _load_downloaded() -> set[str]:
    if DOWNLOADED_FILE.exists():
        try:
            return set(json.loads(DOWNLOADED_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def _save_downloaded(items: set[str]):
    DOWNLOADED_FILE.write_text(
        json.dumps(sorted(items), ensure_ascii=False, indent=0), encoding="utf-8"
    )


def mark_downloaded(urls: list[str]):
    """记录已下载的 URL，供下次增量筛选"""
    cur = _load_downloaded()
    cur.update(urls)
    _save_downloaded(cur)


def filter_new(urls: list[str]) -> tuple[list[str], list[str]]:
    """区分新增和已下载过的，返回 (新增, 已下载)"""
    done = _load_downloaded()
    new, old = [], []
    for u in urls:
        (old if u in done else new).append(u)
    return new, old


def fetch_one(index: int, url: str, local_loader=None) -> dict:
    """
    下载单个表情并转换格式。

    local_loader: 可选的本地取图函数。有本地缓存时优先使用，
                  避免签名 URL 过期导致的 403。
    """
    try:
        raw = None

        # 优先本地缓存
        if local_loader is not None:
            try:
                raw = local_loader(url)
            except Exception:
                raw = None

        # 本地没有再走网络
        if not raw:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if resp.status_code != 200:
                return {"ok": False, "index": index, "url": url,
                        "error": f"HTTP {resp.status_code}"}
            raw = resp.content

        if len(raw) < 100:
            return {"ok": False, "index": index, "url": url, "error": "内容为空"}

        content, ext = convert_for_wechat(raw)
        name = f"emoji_{index:04d}.{ext}"
        return {
            "ok": True,
            "index": index,
            "url": url,
            "name": name,
            "content": content,
            "ext": ext,
            "is_gif": ext == "gif",
        }
    except Exception as e:
        return {"ok": False, "index": index, "url": url,
                "error": f"{type(e).__name__}: {e}"}


def download_batch(
    urls: list[str],
    on_progress=None,
    workers: int = 8,
    local_loader=None,
) -> dict:
    """
    并发下载一批表情，返回结果汇总。

    on_progress(done, total, ok, fail) 用于回传进度。
    local_loader: 本地取图函数，优先于网络请求。
    """
    total = len(urls)
    results = []
    ok = fail = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(fetch_one, i + 1, u, local_loader): i
            for i, u in enumerate(urls)
        }
        for done, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            results.append(r)
            if r["ok"]:
                ok += 1
            else:
                fail += 1
            if on_progress:
                on_progress(done, total, ok, fail)

    results.sort(key=lambda r: r.get("index", 0))
    return {"total": total, "ok": ok, "fail": fail, "items": results}


def build_zip(results: list[dict], zip_name: str = "douyin_emoji.zip") -> bytes:
    """把下载结果打包成 zip 字节流（内存中完成，不落临时文件）"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for r in results:
            if r.get("ok") and r.get("content"):
                zf.writestr(r["name"], r["content"])
    return buf.getvalue()


def save_to_disk(results: list[dict], out_dir: Path) -> list[str]:
    """把结果保存到本地目录"""
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for r in results:
        if r.get("ok") and r.get("content"):
            fp = out_dir / safe_filename(r["name"])
            try:
                fp.write_bytes(r["content"])
                saved.append(str(fp))
            except OSError:
                continue
    return saved
