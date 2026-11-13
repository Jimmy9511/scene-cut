"""公共异常类型。"""

from __future__ import annotations


class Cancelled(RuntimeError):
    """用户取消了当前作业（检测/导出）。"""
