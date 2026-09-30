"""
WebP -> GIF 高保真转换引擎

核心要解决的问题：
1. 抖音表情原始格式是 WebP（静态 + 动态都有），微信不认动态 WebP
2. 直接 WebP 转 GIF 会出现「黑边」（透明通道丢失填成黑色）和「偏色」
3. 静态表情不该转 GIF，转成 PNG 画质更好、体积更小

解决手法（参考成熟项目经验）：
- 提取 Alpha 通道做透明掩模（阈值 128），把透明区域映射到调色板索引 255
- 用 Image.ADAPTIVE 自适应量化 255 色，给透明预留 1 个索引
- disposal=2（每帧后恢复背景）避免动态重叠残影
"""
import io

from PIL import Image, ImageSequence

ALPHA_THRESHOLD = 128


def detect_format(content: bytes) -> str:
    """嗅探真实格式，比看扩展名可靠"""
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp"
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if content[:2] == b"\xff\xd8":
        return "jpg"
    return "unknown"


# Windows 文件名保留字符（NTFS 不允许，冒号还会被当作数据流分隔符）
_INVALID_FS_CHARS = '<>:"/\\|?*'


def safe_filename(name: str, max_len: int = 120) -> str:
    """
    把任意字符串转成跨平台安全的文件名。

    Windows 关键坑：
    - 不允许 <>:"/\\|?* 这些字符，冒号会触发 Permission denied
    - 结尾的点和空格也会导致写入失败
    - 路径总长有限制，需截断
    """
    out = []
    for ch in name:
        if ch in _INVALID_FS_CHARS or ord(ch) < 32:
            out.append("_")
        else:
            out.append(ch)
    cleaned = "".join(out).rstrip(". ")
    if not cleaned:
        cleaned = "unnamed"
    return cleaned[:max_len]


def is_animated(content: bytes) -> bool:
    """判断是否为动态图（仅对 webp/gif 有效）"""
    try:
        img = Image.open(io.BytesIO(content))
        n = getattr(img, "n_frames", 1)
        return bool(getattr(img, "is_animated", False)) and n > 1
    except Exception:
        return False


def _webp_animated_to_gif(content: bytes) -> bytes:
    """动态 WebP -> GIF，带 Alpha 掩模防止黑边"""
    img = Image.open(io.BytesIO(content))

    frames = []
    durations = []

    for frame in ImageSequence.Iterator(img):
        rgba = frame.convert("RGBA")

        # 1. alpha 通道 -> 透明掩模（半透明区域也算透明，避免边缘发黑）
        alpha = rgba.getchannel("A")
        mask = Image.eval(alpha, lambda a: 255 if a <= ALPHA_THRESHOLD else 0)

        # 2. RGBA -> RGB -> 调色板，255 色，给透明留出索引 255
        p_frame = rgba.convert("RGB").convert(
            "P", palette=Image.ADAPTIVE, colors=255
        )
        # 3. 把透明区域涂成索引 255
        p_frame.paste(255, mask)

        frames.append(p_frame)
        durations.append(frame.info.get("duration", 100) or 100)

    out = io.BytesIO()
    frames[0].save(
        out,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        transparency=255,
        disposal=2,
        optimize=True,
    )
    return out.getvalue()


def convert_for_wechat(content: bytes, target_ext: str = "gif") -> tuple[bytes, str]:
    """
    转成微信可直接发送的格式。

    返回 (字节内容, 扩展名)

    - 动态图 -> GIF（真正能动的表情）
    - 静态图 -> PNG（画质最优，微信静态表情也能用）
    - 已经是 GIF 的 -> 原样返回
    """
    fmt = detect_format(content)

    # 已经是 GIF，直接放行，无损
    if fmt == "gif":
        return content, "gif"

    # 动态 webp -> gif
    if fmt == "webp" and is_animated(content):
        try:
            return _webp_animated_to_gif(content), "gif"
        except Exception:
            # 转失败就退回静态处理，不让整体流程挂掉
            pass

    # 静态图（png/jpg/webp 静态）-> PNG
    try:
        img = Image.open(io.BytesIO(content))
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
        out = io.BytesIO()
        img.save(out, format="PNG", optimize=True)
        return out.getvalue(), "png"
    except Exception:
        # 完全解不开就原样返回，至少不丢数据
        return content, fmt if fmt != "unknown" else "bin"


def make_thumbnail(content: bytes, max_side: int = 320) -> tuple[bytes, str]:
    """
    生成缩略图，用于网页网格预览（省流量、加载快）。

    注意：抖音表情原始图可达数 MB，直接丢给浏览器会很卡，
    所以这里统一压到 max_side 以内。
    """
    img = Image.open(io.BytesIO(content))

    # 处理 Palette/透明等特殊模式，避免后续转换报错
    if img.mode == "P":
        img = img.convert("RGBA")
    elif img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGBA")

    img.thumbnail((max_side, max_side), Image.LANCZOS)

    out = io.BytesIO()
    if img.mode == "RGBA":
        img.save(out, format="PNG", optimize=True)
        return out.getvalue(), "png"
    img.save(out, format="JPEG", quality=82, optimize=True)
    return out.getvalue(), "jpg"
