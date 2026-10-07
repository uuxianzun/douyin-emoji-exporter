"""
抖音表情包导出工具 - 启动入口

这个文件承担全部中文输出，start.bat 只负责用英文做最小化的环境引导。
原因：Windows 的 cmd 解析 .bat 时使用系统 ANSI 代码页（中文系统是 GBK），
bat 里写中文极易乱码甚至导致语法错误。把中文交给 Python 输出最稳妥。

本文件还会在启动前做一次环境自检，缺什么自动补什么，
因此除了初始的 Python 之外，用户无需手动安装任何东西。
"""
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ============ 镜像源配置 ============
# 优先用国内源加速；都失败时回退到官方源
PIP_MIRRORS = [
    ("清华", "https://pypi.tuna.tsinghua.edu.cn/simple"),
    ("阿里云", "https://mirrors.aliyun.com/pypi/simple"),
    ("中科大", "https://pypi.mirrors.ustc.edu.cn/simple"),
]

# Playwright 浏览器内核镜像（npmmirror 提供的官方同步）
PLAYWRIGHT_MIRROR = "https://cdn.npmmirror.com/binaries/playwright"

# Python 便携版下载源（多个备选，按顺序尝试）
PYTHON_MIRRORS = [
    ("华为云", "https://mirrors.huaweicloud.com/python/{ver}/python-{ver}-embed-amd64.zip"),
    ("官方", "https://www.python.org/ftp/python/{ver}/python-{ver}-embed-amd64.zip"),
]

# pip 引导脚本（get-pip.py）来源
GETPIP_URLS = [
    ("官方", "https://bootstrap.pypa.io/get-pip.py"),
    ("阿里云", "https://mirrors.aliyun.com/pypi/get-pip.py"),
]

# 目标 Python 版本（便携版用）
PORTABLE_PY_VERSION = "3.11.9"

# 便携式运行时存放位置（放在项目内，便于"彻底清理"一并删除）
PORTABLE_DIR = ROOT / ".runtime"


def setup_console():
    """确保控制台能正确输出中文，并让输出实时显示（不被缓冲）"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass


def line(char="=", n=62):
    print(char * n)


def safe_input(prompt: str = "") -> str:
    """
    读取用户输入，但不会因输入流结束而崩溃。

    正常情况下就是 input()；当输入被重定向（管道/重定向文件）或用户
    按了 Ctrl+C 时，input() 会抛 EOFError / KeyboardInterrupt，
    这里统一吞掉返回空串，让调用方按"默认行为"继续，而不是抛出异常。
    """
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def banner():
    line("=")
    print("  抖音表情包导出工具")
    print("  by UUXIAN  ·  哔哩哔哩：https://space.bilibili.com/30472425")
    line("=")


# ============ 便携式运行时 ============
# 目的：让"纯素"电脑（没装过 Python）也能一键跑起来。
# 思路：下载官方 embeddable 版 Python 到项目内的 .runtime 目录，
#       手工补上 pip，再装依赖。全程不影响系统环境，卸载就是删目录。

def _download(url: str, dest: Path, desc: str = "") -> bool:
    """
    带进度提示的下载。失败返回 False，不抛异常。

    先用 requests（超时控制更好），失败后再退回 urllib 重试一次。
    注意必须**两条独立的路**：requests 可能"能 import 但不可用"
    （包残缺、被代理拦截等），此时它抛的是运行期异常而不是 ImportError。
    老写法把 urllib 兜底放在同一个 try 里，requests 一报错就整体放弃了。
    """
    import urllib.request

    label = desc or dest.name

    # --- 路线 1：requests ---
    try:
        import requests
        with requests.get(url, stream=True, timeout=30) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            last = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = done * 100 // total
                        if pct >= last + 5:
                            last = pct
                            sys.stdout.write(f"\r      {label}  {pct}%")
                            sys.stdout.flush()
        if dest.exists() and dest.stat().st_size > 0:
            print(f"\r      {label}  完成（{_human(done)}）      ")
            return True
    except Exception:
        pass

    # --- 路线 2：urllib 兜底 ---
    try:
        with urllib.request.urlopen(url, timeout=30) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
        print(f"\r      {label}  完成（{_human(dest.stat().st_size)}）      ")
        return True
    except Exception as e:
        print(f"\r      {label}  下载失败：{e}")
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def _portable_python() -> Path:
    """便携版 python.exe 的路径"""
    return PORTABLE_DIR / "python.exe"


def _same_or_inside(child: Path, parent: Path) -> bool:
    """child 是否就是 parent，或者位于 parent 内部（都解析成绝对路径再比）"""
    try:
        c = child.resolve()
        p = parent.resolve()
    except Exception:
        return False
    if c == p:
        return True
    try:
        c.relative_to(p)
        return True
    except ValueError:
        return False


def find_system_python() -> str | None:
    """
    在系统里找一个**独立于本项目**的 Python（不需要它已装依赖，只要能跑起来）。

    找到就返回解释器路径，找不到返回 None。
    优先用 py 启动器，其次 python / python3。

    【为什么必须排除 .runtime】
    Windows 的 CreateProcess 搜索顺序是：
        ① 调用方进程 exe 所在目录 → ② 当前目录 → ③ 系统目录 → ④ PATH
    run.py 是被 .runtime\\python.exe 启动的，所以执行 "python" 时，系统会先在
    .runtime\\ 目录里找到**它自己**。如果不排除，这个函数就会把便携运行时
    误认成"系统 Python"，调用方于是认为"环境已就绪"、跳过 install_portable()
    ——而 pip 恰恰是在那一步装上的，最终表现为一堆 "No module named pip"。
    """
    candidates = [
        ["py", "-3"],
        ["python"],
        ["python3"],
    ]
    for c in candidates:
        try:
            r = subprocess.run(
                c + ["-c", "import sys; print(sys.executable)"],
                capture_output=True, text=True, timeout=20,
            )
            if r.returncode != 0:
                continue
            lines = (r.stdout or "").strip().splitlines()
            if not lines:
                continue
            p = Path(lines[-1])
            if not p.exists():
                continue
            # 便携运行时不属于"系统 Python"，排除掉
            if _same_or_inside(p, PORTABLE_DIR):
                continue
            return str(p)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
    return None


def _portable_usable() -> bool:
    """
    便携运行时是否**真正可用**。

    不只看 python.exe 在不在——必须能跑起来、且标准库完整。
    因为曾出现过"删除到一半"留下的残缺目录：python.exe 还在，
    但 python311.zip（标准库）已被删掉，跑任何 import 都失败。
    """
    py = _portable_python()
    if not py.exists():
        return False
    # 必须能 import 标准库模块，才算完整
    try:
        r = subprocess.run(
            [str(py), "-c", "import sys, os, zipfile, shutil; print(sys.version_info[0])"],
            capture_output=True, text=True, timeout=30,
        )
        return r.returncode == 0
    except Exception:
        return False


def _pip_works(py: Path) -> bool:
    """
    指定解释器的 pip 是否**真的能跑**。

    不能只看 Lib\\site-packages\\pip 目录在不在——曾出现过 pip 目录残缺的情况
    （只剩 _internal / _vendor，缺了 __init__.py 和 __main__.py），
    这时 python -m pip 会直接报 "cannot be directly executed"。
    所以判据必须是实际执行一次 `-m pip --version`。
    """
    try:
        r = subprocess.run(
            [str(py), "-m", "pip", "--version"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
        )
        return r.returncode == 0
    except Exception:
        return False


def portable_ready() -> bool:
    """便携式运行时是否已经装好（完整 + pip 可用）"""
    if not _portable_usable():
        return False
    return _pip_works(_portable_python())


def _remove_portable_dir() -> bool:
    """
    尝试删除 .runtime 目录。返回是否删干净。

    有文件被占用（例如那个 python.exe 正在跑）时会删不掉，
    这时要如实返回 False，不能假装成功。
    """
    if not PORTABLE_DIR.exists():
        return True
    try:
        shutil.rmtree(PORTABLE_DIR)
        return True
    except Exception:
        # 退而求其次：逐个尝试，看还剩什么
        try:
            for item in PORTABLE_DIR.iterdir():
                try:
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
                except Exception:
                    pass
        except Exception:
            pass
        # 再看还剩什么
        try:
            left = list(PORTABLE_DIR.iterdir())
            return len(left) == 0
        except Exception:
            return False


def _configure_pth() -> bool:
    """
    确保 ._pth 启用 site 并包含 Lib\\site-packages。

    embeddable 版默认没有 site，不改就无法用 pip 装的包。
    官方包自带 python3xx._pth；万一缺失（说明状态异常）就补一个标准的。
    """
    pths = list(PORTABLE_DIR.glob("*._pth"))
    if not pths:
        ver = "".join(PORTABLE_PY_VERSION.split(".")[:2])   # 3.11.9 -> 311
        created = PORTABLE_DIR / f"python{ver}._pth"
        try:
            created.write_text(
                f"python{ver}.zip\n.\nimport site\nLib\\site-packages\n",
                encoding="utf-8",
            )
        except Exception as e:
            print(f"  [错误] 无法创建 ._pth 配置：{e}")
            return False
        pths = [created]
    try:
        for pth in pths:
            content = pth.read_text(encoding="utf-8", errors="replace")
            lines = [ln.rstrip("\r") for ln in content.splitlines()]
            # 去掉被注释的 import site，统一改成启用的
            lines = [ln for ln in lines if ln.strip() != "#import site"]
            if "." not in lines:
                lines.insert(0, ".")
            if "import site" not in lines:
                lines.append("import site")
            if "Lib\\site-packages" not in lines:
                lines.append("Lib\\site-packages")
            pth.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True
    except Exception as e:
        print(f"  [错误] 配置失败：{e}")
        return False


def _purge_pip_leftovers() -> None:
    """
    清掉残缺的 pip 目录，避免 get-pip.py 误判"已安装"而直接跳过。
    只动 .runtime\\Lib\\site-packages 下的 pip 相关条目，范围很小。
    """
    sp = PORTABLE_DIR / "Lib" / "site-packages"
    if not sp.is_dir():
        return
    for pat in ("pip", "pip-*.dist-info", "pip-*.egg-info"):
        for p in list(sp.glob(pat)):
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink()
            except Exception:
                pass


def _ensure_pip(py: Path) -> bool:
    """确保指定解释器有可用的 pip；没有就下载 get-pip.py 引导安装"""
    if _pip_works(py):
        print("      pip 已就绪")
        return True

    # 残缺的 pip 会让 get-pip.py 以为"装过了"，先清掉
    _purge_pip_leftovers()

    print("      正在安装包管理器 pip...")
    getpip = PORTABLE_DIR / "get-pip.py"
    got = False
    for name, url in GETPIP_URLS:
        if _download(url, getpip, desc="get-pip.py"):
            got = True
            break
    if not got:
        print("  [错误] pip 引导脚本下载失败。")
        return False

    def _cleanup_script():
        try:
            getpip.unlink(missing_ok=True)
        except Exception:
            pass

    # 依次尝试各镜像源；装完再实测一次，确认真的能用
    for name, mirror in PIP_MIRRORS:
        print(f"      尝试镜像源：{name} ...")
        try:
            subprocess.check_call(
                [str(py), str(getpip), "-i", mirror,
                 "--trusted-host", mirror.split("/")[2],
                 "--no-warn-script-location"],
                stdout=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            print(f"      {name} 不可用，换下一个...")
            continue
        if _pip_works(py):
            _cleanup_script()
            print("      pip 安装完成")
            return True
        print(f"      {name} 装完 pip 仍不可用，换下一个...")

    # 全部镜像失败，回退官方源
    print("      镜像源均不可用，尝试官方源...")
    try:
        subprocess.check_call(
            [str(py), str(getpip), "--no-warn-script-location"],
            stdout=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        pass
    if _pip_works(py):
        _cleanup_script()
        print("      pip 安装完成（官方源）")
        return True

    print("  [错误] pip 安装失败，请检查网络后重试。")
    return False


def _mark_runtime_for_reinstall() -> None:
    """
    留一个标记，让**下次 start.bat 启动时**先删掉 .runtime 再重新下载。

    为什么不在这里直接删或改名：
      - 删除：本进程正跑在 .runtime 里，Windows 不允许删掉正在使用的文件；
      - 改名：虽然目录"能改名"，但当前解释器的 sys.path 里记的还是旧路径，
              改名后它连自己的标准库（python311.zip）都找不到，后续 import 直接崩。
    start.bat 是在启动 Python **之前**执行 rmdir 的，那时没有任何文件占用，
    而且它本来就有"看到标记就删掉重装"的分支，正好复用。
    """
    try:
        PORTABLE_DIR.mkdir(parents=True, exist_ok=True)
        (PORTABLE_DIR / ".cleanup-pending").write_text("1", encoding="utf-8")
    except Exception:
        pass


def install_portable() -> bool:
    """
    安装（或**就地修复**）便携式 Python 运行时。

    步骤：
      1. 下载官方 embeddable zip（约 11MB）—— 仅当解释器不可用时
      2. 解压到 .runtime
      3. 配置 ._pth：追加 Lib\\site-packages 和 import site
      4. 下载 get-pip.py 引导 pip

    【为什么不整体删掉重来】
    本脚本很可能正跑在 .runtime\\python.exe 里，Windows 不允许删除正在被使用的
    文件，"自己删自己"必然失败，还会留下删了一半的残缺目录（更难处理）。
    所以只要 python.exe 本身还能用，就**原地震补** pip，一个文件都不删。
    """
    print()
    print("  正在准备运行环境（首次使用需要一点时间）")
    print()

    # 已装好就直接返回
    if portable_ready():
        print("  便携式运行环境已就绪")
        return True

    py = _portable_python()

    # --- A. 解释器本身可用，只是缺 pip（或 pip 坏了）→ 原地震补 ---
    if py.exists() and _portable_usable():
        print("  检测到运行环境缺少 pip（或 pip 已损坏），正在原地修复...")
        if _configure_pth() and _ensure_pip(py):
            print()
            print("  运行环境修复完成。")
            print()
            return True

        # 原地修不好 → 留标记，让下次 start.bat 删掉重装（那时没有文件占用，最稳）
        _mark_runtime_for_reinstall()
        print()
        print("  [错误] 运行环境修复失败，已安排下次启动时自动重装。")
        print("         请关闭本窗口，然后重新双击 start.bat 即可。")
        print()
        return False

    # --- B. 解释器不可用（缺失 / 标准库残缺）→ 只能清理后重装 ---
    if PORTABLE_DIR.exists():
        print("  检测到上次留下的运行环境，正在清理...")
        if not _remove_portable_dir():
            print()
            print("  [错误] 无法清理旧的运行环境，可能有文件正在被占用。")
            print("         请关闭其它正在运行的本工具窗口，然后重试。")
            print("         如果仍然不行，请手动删除这个文件夹：")
            print(f"         {PORTABLE_DIR}")
            print()
            return False
        print("      已清理")

    PORTABLE_DIR.mkdir(parents=True, exist_ok=True)

    # --- 1. 下载 embeddable zip ---
    print("  [1/4] 下载 Python 运行环境（约 11MB）...")
    zip_path = PORTABLE_DIR / "python-embed.zip"
    ok = False
    ver = PORTABLE_PY_VERSION
    for name, tpl in PYTHON_MIRRORS:
        url = tpl.format(ver=ver)
        print(f"      尝试来源：{name}")
        if _download(url, zip_path, desc=f"Python {ver}"):
            ok = True
            break
    if not ok:
        print()
        print("  [错误] Python 运行环境下载失败，请检查网络后重试。")
        return False

    # --- 2. 解压 ---
    print("  [2/4] 解压...")
    try:
        import zipfile
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(PORTABLE_DIR)
        zip_path.unlink(missing_ok=True)
        print("      完成")
    except Exception as e:
        print(f"  [错误] 解压失败：{e}")
        return False

    py = _portable_python()
    if not py.exists():
        print("  [错误] 解压后找不到 python.exe，文件可能损坏。")
        return False

    # --- 3. 修 ._pth（关键一步）---
    print("  [3/4] 配置运行环境...")
    if not _configure_pth():
        return False
    print("      完成")

    # --- 4. 引导 pip ---
    print("  [4/4] 安装包管理器 pip...")
    if not _ensure_pip(py):
        return False

    print()
    print("  运行环境准备完成。")
    print()
    return True


def _current_python() -> str:
    """
    选择要用的解释器：

    优先用便携式运行时（如果已装好），否则用当前解释器。
    保证依赖检查和安装都作用在同一个解释器上。

    注意：判据是 portable_ready()（完整可用）而不是"文件存在"，
    因为可能留下残缺目录（python.exe 在但标准库没了）。
    """
    if portable_ready():
        return str(_portable_python())
    return sys.executable


def ensure_dependencies() -> bool:
    """检查并自动安装缺失的依赖（优先国内源，失败回退官方源）"""
    py = _current_python()
    required = ["fastapi", "uvicorn", "playwright", "PIL", "requests"]
    probe = {m: m for m in required}

    # 直接问目标解释器"这些包在不在"，比在本进程 import 更准确
    missing = []
    for mod in required:
        try:
            r = subprocess.run(
                [py, "-c", f"import {mod}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
            )
            if r.returncode != 0:
                missing.append(mod)
        except Exception:
            missing.append(mod)

    if not missing:
        print("[2/3] 依赖检查    已就绪")
        return True

    print(f"[2/3] 依赖检查    缺少 {len(missing)} 个，开始安装...")
    print("      使用国内镜像加速")

    pkgs = {
        "fastapi": "fastapi",
        "uvicorn": "uvicorn[standard]",
        "playwright": "playwright",
        "PIL": "pillow",
        "requests": "requests",
    }
    targets = [pkgs[m] for m in missing]

    # 升级 pip（静默，失败不阻塞）
    try:
        subprocess.run(
            [py, "-m", "pip", "install", "--upgrade", "pip", "-q",
             "--no-warn-script-location"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
        )
    except Exception:
        pass

    # 依次尝试各镜像源（带超时，避免在不可用的源上卡太久）
    for name, url in PIP_MIRRORS:
        print(f"      尝试镜像源：{name} ...", flush=True)
        try:
            subprocess.check_call([
                py, "-m", "pip", "install",
                "-i", url,
                "--trusted-host", url.split("/")[2],
                "--timeout", "20",
                "--retries", "1",
                "--no-warn-script-location",
                *targets,
            ])
            print(f"      安装完成（{name}）")
            return True
        except subprocess.CalledProcessError:
            print(f"      {name} 不可用，换下一个...")
            continue

    # 全部镜像失败，回退官方源
    print("      镜像源均不可用，尝试官方源...")
    try:
        subprocess.check_call(
            [py, "-m", "pip", "install", "--no-warn-script-location"] + targets
        )
        print("      安装完成（官方源）")
        return True
    except subprocess.CalledProcessError:
        print()
        print("[错误] 依赖安装失败。请检查网络连接后重试。")
        return False


def ensure_browser() -> bool:
    """检查 Chromium 内核是否就绪（用文件探测，避免每次都启动浏览器）"""
    print("[3/3] 浏览器内核  ", end="", flush=True)

    # Playwright 把内核装在用户目录下，先看有没有
    if _browser_installed():
        print("已就绪")
        return True

    print("未安装，开始下载（约 150MB，请耐心等待）...")
    print("      使用国内镜像加速")

    py = _current_python()

    # 通过环境变量指定镜像源，Playwright 会优先从这里下载
    env = dict(os.environ)
    env["PLAYWRIGHT_DOWNLOAD_HOST"] = PLAYWRIGHT_MIRROR

    try:
        subprocess.check_call(
            [py, "-m", "playwright", "install", "chromium"],
            env=env,
        )
        print("      浏览器内核下载完成")
        return True
    except subprocess.CalledProcessError:
        # 镜像失败则回退官方源
        print("      镜像失败，尝试官方源...")
        try:
            subprocess.check_call(
                [py, "-m", "playwright", "install", "chromium"]
            )
            print("      浏览器内核下载完成")
            return True
        except subprocess.CalledProcessError:
            print("[错误] 浏览器内核下载失败，请检查网络后重试。")
            return False


def _browser_installed() -> bool:
    """
    探测 Chromium 内核是否已安装。

    优先用 Playwright 官方 API 拿可执行路径再判断文件是否存在——
    比手工拼路径可靠得多（内核目录名带版本号，会随版本变化）。
    """
    from pathlib import Path

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            exe = Path(p.chromium.executable_path)
            return exe.exists()
    except Exception:
        return False


def _playwright_dir() -> Path:
    """Playwright 内核的安装目录"""
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "ms-playwright"
    return Path.home() / "AppData" / "Local" / "ms-playwright"


def _dir_size(path: Path) -> int:
    """计算目录总大小（字节）"""
    if not path.exists():
        return 0
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    except Exception:
        pass
    return total


def _human(n: int) -> str:
    """字节数转可读格式"""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ============ 清理功能 ============

def clean_cache() -> None:
    """
    简单清理：清掉缓存和导出产物，保留环境和依赖。

    清掉之后下次用会重新抓取，但不用重装任何东西。
    """
    print()
    line("=")
    print("  简单清理")
    line("=")
    print()
    print("  将清理以下内容：")
    print("    · data/cache/        表情原图与缩略图缓存")
    print("    · output/            已导出的表情文件")
    print()
    print("  以下内容会保留：")
    print("    · 运行环境与依赖      （无需重装）")
    print("    · 浏览器内核          （无需重下）")
    print("    · 登录状态与导出记录")
    print()

    if safe_input("  确认清理？(y/N): ").strip().lower() not in ("y", "yes"):
        print("  已取消。")
        return

    targets = [
        ROOT / "data" / "cache",
        ROOT / "output",
    ]

    freed = 0
    for t in targets:
        if t.exists():
            freed += _dir_size(t)
            try:
                shutil.rmtree(t)
                print(f"  已清理：{t.relative_to(ROOT)}")
            except Exception as e:
                print(f"  清理失败：{t.relative_to(ROOT)} -> {e}")
        # 重建缓存目录，避免程序找不到而报错
        try:
            t.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    print()
    print(f"  清理完成，释放约 {_human(freed)}")
    print("  下次使用会重新抓取，无需重装环境。")
    print()


def clean_all() -> None:
    """
    彻底清理：删掉本工具产生的所有内容。

    注意：包括浏览器内核（在用户目录下）和 Python 环境。
    这个操作不可逆，会二次确认。

    关键：.runtime 目录里可能正跑着本脚本自己（python.exe 被占用），
    这时删不掉。遇到这种情况要如实报告，并把"怎么办"讲清楚，
    而不是留一个残缺目录让下次启动报莫名其妙的错。
    """
    print()
    line("=")
    print("  彻底清理")
    line("=")
    print()
    print("  [警告] 这会删除本工具的全部内容，操作不可恢复！")
    print()
    print("  将被删除：")
    print("    · data/              登录状态、缓存、导出记录")
    print("    · output/            已导出的表情文件")
    print("    · .runtime/          便携式 Python 运行环境与所有依赖")
    print("    · .venv/             旧的虚拟环境（如存在）")
    print("    · 浏览器内核           Playwright 的 Chromium（约 150MB）")
    print()
    print("  说明：如果你电脑上本来就装了 Python，那是系统自带的，")
    print("        本工具不会去动它。这里删的只是工具自己下载的运行时。")
    print()

    # 检测一下当前是不是正跑在 .runtime 里
    running_from_runtime = False
    try:
        running_from_runtime = (
            Path(sys.executable).resolve().parent == PORTABLE_DIR.resolve()
        )
    except Exception:
        pass

    if running_from_runtime:
        print("  注意：当前程序正运行在 .runtime 环境中，")
        print("        它自己的核心文件此刻被占用，无法就地删除。")
        print("        清理会先完成其它部分，稍后自动补删运行环境。")
        print()

    # 先算一下能释放多少
    size = 0
    for p in [ROOT / "data", ROOT / "output", ROOT / ".venv",
              PORTABLE_DIR, _playwright_dir()]:
        size += _dir_size(p)
    print(f"  预计释放：约 {_human(size)}")
    print()
    print("  提示：删除后想再用，需要重新运行 start.bat 完整安装一次。")
    print()

    ans = safe_input("  确认彻底清理？输入 YES 继续: ").strip()
    if ans != "YES":
        print("  已取消。")
        return

    print()

    # 能直接删的先删
    failed = []
    for name, path in [
        ("data 目录", ROOT / "data"),
        ("output 目录", ROOT / "output"),
        (".venv 环境", ROOT / ".venv"),
    ]:
        if path.exists():
            try:
                shutil.rmtree(path)
                print(f"  已删除：{name}")
            except Exception as e:
                print(f"  删除失败：{name} -> {e}")
                failed.append(name)
        else:
            print(f"  跳过（不存在）：{name}")

    # 浏览器内核（在用户目录，不在项目内）
    pw = _playwright_dir()
    if pw.exists():
        try:
            shutil.rmtree(pw)
            print("  已删除：浏览器内核")
        except Exception as e:
            print(f"  删除失败：浏览器内核 -> {e}")
            failed.append("浏览器内核")
    else:
        print("  跳过（不存在）：浏览器内核")

    # 运行环境放最后处理——可能正被自己占用
    print()
    if PORTABLE_DIR.exists():
        if running_from_runtime:
            # 自己删自己删不干净，交给一个延迟的小脚本来补刀
            _deferred_remove_runtime()
        else:
            if _remove_portable_dir():
                print("  已删除：运行环境")
            else:
                print("  删除失败：运行环境（可能有文件被占用）")
                failed.append("运行环境")
    else:
        print("  跳过（不存在）：运行环境")

    print()
    print("=" * 62)
    if failed:
        print("  清理完成，但有部分内容没能删除：")
        for f in failed:
            print(f"    · {f}")
        print()
        print("  请关闭其它正在运行的本工具窗口后，再执行一次清理。")
    else:
        print("  彻底清理完成。")
        print()
        print("  项目文件夹现在只剩程序代码，可以整个删掉了。")
        print("  想再使用，重新运行本程序即可完整安装。")
    print("=" * 62)
    print()


def _deferred_remove_runtime() -> None:
    """
    安排一个"等本进程退出后再删 .runtime"的小任务。

    为什么要这么做：本脚本此刻就跑在 .runtime 里，python.exe / python311.dll
    正被自己占用，Windows 不允许删除正在使用的文件。所以只能：
      ① 起一个独立进程；
      ② 让它先等几秒（等我们退出）；
      ③ 再删目录。

    用 PowerShell 来做更可靠——`timeout` 命令在 stdin 被重定向时会直接报错退出，
    而 PowerShell 的 Start-Sleep 没有这个限制。

    同时往 .runtime 里写一个标记文件，万一延迟删除也失败，
    下次 start.bat 启动时能识别出来并重试。
    """
    rt = str(PORTABLE_DIR)

    # 写一个"待清理"标记，给下次启动兜底
    try:
        (PORTABLE_DIR / ".cleanup-pending").write_text("1", encoding="utf-8")
    except Exception:
        pass

    # PowerShell 命令：先睡 3 秒，再递归强删
    ps = (
        "Start-Sleep -Seconds 3; "
        f"Remove-Item -LiteralPath '{rt}' -Recurse -Force -ErrorAction SilentlyContinue"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print("  运行环境：将在窗口关闭后自动删除（避免文件占用冲突）")
    except Exception as e:
        # PowerShell 不可用就退回 cmd
        try:
            cmd = f'ping -n 4 127.0.0.1 >nul & rmdir /s /q "{rt}"'
            subprocess.Popen(
                ["cmd", "/c", cmd],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print("  运行环境：将在窗口关闭后自动删除（避免文件占用冲突）")
        except Exception:
            print(f"  运行环境：自动删除安排失败 -> {e}")
            print(f"           请手动删除文件夹：{PORTABLE_DIR}")


def _has_pending_cleanup() -> bool:
    """上次的彻底清理是否没删干净（留了标记）"""
    try:
        return (PORTABLE_DIR / ".cleanup-pending").exists()
    except Exception:
        return False


def open_browser_later():
    time.sleep(2.0)
    try:
        webbrowser.open(f"http://127.0.0.1:8765")
    except Exception:
        pass


def _is_tty() -> bool:
    """判断标准输入是不是真实的交互终端（双击 bat 打开时是）"""
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False


def show_menu(wait: int = 10) -> str:
    """
    启动菜单。用户可能只想清理，所以启动时先给选择。

    返回 'start' | 'clean' | 'cleanall' | 'exit'

    如果 stdin 是真实终端，会等待 wait 秒；超时或直接回车则默认"启动"。
    如果 stdin 不是终端（被重定向/自动化调用），不做倒计时，直接按输入处理，
    避免在无人值守时误触发整包下载。
    """
    print("请选择操作：")
    print()
    print(f"  1  启动工具（默认，{wait} 秒后自动开始）")
    print("  2  简单清理    清缓存和导出文件，保留环境，下次免重装")
    print("  3  彻底清理    删除环境、依赖、浏览器内核，全部清空")
    print("  0  退出")
    print()

    ans = ""

    # 真实终端下做带倒计时的等待；每 0.1 秒探测一次按键
    if _is_tty() and wait > 0:
        try:
            import msvcrt  # Windows 专属，用于无回车读取按键

            prompt = "  输入序号后回车（等待 {n} 秒自动开始）: "
            for left in range(wait, 0, -1):
                sys.stdout.write("\r" + prompt.format(n=left).ljust(48))
                sys.stdout.flush()
                # 在这 1 秒内每 0.1 秒看有没有按键
                for _ in range(10):
                    if msvcrt.kbhit():
                        ch = msvcrt.getwch()
                        if ch in ("\x00", "\xe0"):
                            # 方向键等多字节按键，吞掉后续一个字节
                            if msvcrt.kbhit():
                                msvcrt.getwch()
                            continue
                        # 收集这一行，直到回车
                        buf = [] if ch == "\r" else [ch]
                        if ch != "\r":
                            sys.stdout.write(ch)
                            sys.stdout.flush()
                        while True:
                            c2 = msvcrt.getwch()
                            if c2 == "\r":
                                break
                            if c2 == "\x08":  # 退格
                                if buf:
                                    buf.pop()
                                    sys.stdout.write("\b \b")
                                    sys.stdout.flush()
                                continue
                            buf.append(c2)
                            sys.stdout.write(c2)
                            sys.stdout.flush()
                        ans = "".join(buf).strip()
                        print()
                        return _menu_dispatch(ans)
                    time.sleep(0.1)
            # 倒计时走完
            print()
            print("  未做选择，默认启动工具。")
            print()
            return "start"
        except ImportError:
            pass

    # 非终端（管道/自动化）或取不到按键时，用普通输入
    ans = safe_input("  输入序号后回车: ").strip()
    return _menu_dispatch(ans)


def _menu_dispatch(ans: str) -> str:
    """把菜单输入映射成动作"""
    if ans == "2":
        return "clean"
    if ans == "3":
        return "cleanall"
    if ans == "0":
        return "exit"
    return "start"


def _reexec(py: str) -> int:
    """
    用指定解释器重新运行本脚本。

    用环境变量做个标记，让重启后的进程跳过菜单，避免菜单出现两次。
    """
    env = dict(os.environ)
    # 避免中文在子进程里乱码
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(ROOT)
    env["DYB_SKIP_MENU"] = "1"
    try:
        return subprocess.call([py, str(ROOT / "run.py"), *sys.argv[1:]], env=env)
    except KeyboardInterrupt:
        return 0


def _resolve_interpreter() -> str | None:
    """
    决定这次到底用哪个 Python。返回解释器路径。

    优先级：
      1. 已经装好的便携运行时（保证和上次一致，依赖已就位）
      2. 系统里已有的 Python（省去下载 11MB，用户体验更好）
      3. 都没有 → 装便携运行时

    注意：这里只做"选择"，真正的安装交给调用方，方便出错时统一提示。
    """
    # 1. 便携运行时可用就直接用
    if portable_ready():
        return str(_portable_python())

    # 2. 看看系统有没有 Python（用户本机已有的就别重复下载了）
    sys_py = find_system_python()
    if sys_py:
        return sys_py

    # 3. 都没有，需要装便携运行时
    return None


def run_server() -> int:
    """启动 web 服务（正常使用流程）"""
    # 第一步：选一个可用的解释器。
    # 优先用系统已有的 Python（省下载），没有才装便携运行时。
    target = _resolve_interpreter()

    if target is None:
        # 系统里没有 Python，装便携运行时
        if not install_portable():
            print()
            safe_input("按回车键退出...")
            return 1
        target = str(_portable_python())

    # 如果当前解释器和目标不一致，就切换过去再跑一次
    try:
        same = Path(sys.executable).resolve() == Path(target).resolve()
    except Exception:
        same = False
    if not same:
        return _reexec(target)

    print("[1/3] 运行环境    已就绪")
    print()

    if not ensure_dependencies():
        print()
        safe_input("按回车键退出...")
        return 1

    if not ensure_browser():
        print()
        safe_input("按回车键退出...")
        return 1

    from app.core.config import PORT, HOST

    print()
    line("-")
    print(f"  界面地址： http://{HOST}:{PORT}")
    print("  浏览器会自动打开，若无反应请手动访问上面的地址")
    print()
    print("  使用完毕后，可以回到本窗口选择清理功能（见下方提示）")
    print("  ---")
    print("  抖音表情包导出工具  by UUXIAN")
    print("  哔哩哔哩： https://space.bilibili.com/30472425")
    line("-")
    print()

    try:
        import uvicorn
        from app.server import app
    except Exception:
        print("[错误] 程序加载失败，详情如下：")
        line("-")
        traceback.print_exc()
        line("-")
        safe_input("按回车键退出...")
        return 1

    threading.Thread(target=open_browser_later, daemon=True).start()

    try:
        uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
    except OSError as e:
        print()
        print(f"[错误] 端口 {PORT} 无法使用：{e}")
        print("       可能上一次的程序还没关闭，或被其他软件占用。")
        print("       请关闭后重试。")
        print()
        safe_input("按回车键退出...")
        return 1
    except KeyboardInterrupt:
        pass
    except Exception:
        print()
        print("[错误] 服务异常退出，详情如下：")
        line("-")
        traceback.print_exc()
        line("-")
        safe_input("按回车键退出...")
        return 1

    # 服务停止后，留在窗口里让用户选择清理
    print()
    line("=")
    print("  服务已停止")
    line("=")
    print()
    print("  还需要清理吗？")
    print()
    print("  1  不用了，直接退出")
    print("  2  简单清理    清缓存和导出文件，保留环境")
    print("  3  彻底清理    全部删除，下次需重新安装")
    print()
    try:
        ans = safe_input("  输入序号后回车: ").strip()
    except (EOFError, KeyboardInterrupt):
        ans = "1"

    if ans == "2":
        clean_cache()
    elif ans == "3":
        clean_all()

    return 0


def _running_from_portable() -> bool:
    """当前进程是不是正跑在 .runtime 里（不判断好坏，只看位置）"""
    try:
        return Path(sys.executable).resolve().parent == PORTABLE_DIR.resolve()
    except Exception:
        return False


def _running_from_broken_runtime() -> bool:
    """
    检测"是不是正跑在一个**残缺到无法自救**的 .runtime 里"。

    背景：如果 .runtime 删除到一半（例如清理时文件被占用），会留下
    python.exe 还在、但 python311.zip（标准库）没了的残缺目录。
    这种解释器连 import 都会失败，必须尽早切走。

    注意：这里**只**看标准库完整性，故意不含 pip。
    "标准库完整但缺 pip"完全是可自愈的（见 install_portable 的原地修复分支），
    不必也不该走"换解释器重启"这条路——那会白白跳过启动菜单。
    """
    return _running_from_portable() and not _portable_usable()


def _escape_broken_runtime() -> int | None:
    """
    如果当前正跑在残缺的 .runtime 里，就换一个可用解释器重启自己。

    返回 None 表示无需处理（当前解释器正常），否则返回退出码。

    【注意】这里刻意**不再**先删除 .runtime。
    本进程正跑在里面，删不掉；而且"删了一半"只会制造出更难处理的残缺目录。
    install_portable() 现在支持原地重装，直接交给它就好。
    """
    if not _running_from_broken_runtime():
        return None

    print()
    print("  检测到运行环境不完整，正在修复...")
    print()

    # 优先借用系统里已有的 Python，最省事
    healthy = find_system_python()
    if healthy:
        print("  已找到可用的 Python，正在切换...")
        print()
        return _reexec(healthy)

    # 系统里也没有，就地重装便携运行时（它会自己处理残缺目录）
    if not install_portable():
        print()
        safe_input("按回车键退出...")
        return 1
    return _reexec(str(_portable_python()))


def main():
    setup_console()

    # 支持命令行直接指定，便于自动化
    args = sys.argv[1:]
    if "--clean" in args:
        banner()
        clean_cache()
        safe_input("按回车键退出...")
        return 0
    if "--clean-all" in args:
        banner()
        clean_all()
        safe_input("按回车键退出...")
        return 0

    banner()

    # 上次彻底清理如果没删干净（留了标记），这里补一刀。
    # 但绝不能删"自己正跑在里面的"那个 .runtime —— Windows 删不掉正在使用的
    # 文件，硬删只会留下"删了一半"的残缺目录，反而更难处理。
    if _has_pending_cleanup() and not _running_from_portable():
        if _remove_portable_dir():
            print("  已清理上次残留的运行环境。")
            print()

    # 最先处理：如果正跑在一个残缺的 .runtime 里，先把自己换到健康解释器。
    # 必须在任何 import 之外能跑——这段只用标准库。
    escaped = _escape_broken_runtime()
    if escaped is not None:
        return escaped

    # 从便携式解释器重启回来的情况：跳过菜单直接启动
    if os.environ.get("DYB_SKIP_MENU") == "1":
        return run_server()

    action = show_menu()

    if action == "exit":
        return 0
    if action == "clean":
        clean_cache()
        safe_input("按回车键退出...")
        return 0
    if action == "cleanall":
        clean_all()
        safe_input("按回车键退出...")
        return 0

    return run_server()


if __name__ == "__main__":
    sys.exit(main())
