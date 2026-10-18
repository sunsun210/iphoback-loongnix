#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""iPhoBack 启动器。用法：python3 run.py 或 iphoback 命令。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    # KDE/Wayland 下更稳的渲染回退
    if not os.environ.get("QT_QPA_PLATFORM"):
        os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "0")
    from iphoback.gui import main
    main()


if __name__ == "__main__":
    main()
