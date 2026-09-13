"""测试基础路径的唯一来源，不依赖调用者当前目录。"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
