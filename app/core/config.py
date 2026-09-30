"""全局配置"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# 数据目录
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"
CACHE_DIR = DATA_DIR / "cache"
PROFILE_DIR = DATA_DIR / "browser_profile"

for _d in (DATA_DIR, OUTPUT_DIR, CACHE_DIR, PROFILE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# 已下载记录（增量导出用）
DOWNLOADED_FILE = DATA_DIR / "downloaded.json"

# 抖音相关
DOUYIN_HOME = "https://www.douyin.com/"

# 图片 URL 识别特征
EMOJI_URL_KEYWORDS = ("emoticon", "emotion", "sticker", "emoji", "expression")

# 动态表情 URL 里常见的特征，用于标记是否可能是动图
ANIMATED_HINTS = ("animate", "animation", "dynamic", "gif")

# 服务配置
HOST = "127.0.0.1"
PORT = 8765
