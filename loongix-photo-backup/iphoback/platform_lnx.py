# -*- coding: utf-8 -*-
"""Loongnix 平台适配层。

本文件集中处理 Loongnix Desktop 25.1（Debian 13 / loongarch64 / KDE Plasma 6）上
所有与平台相关的差异，让上层代码保持干净：

* Python 版本（系统 Python 3.13.5，PEP 668 外部管理，不 pip 装包）
* 优先 PyQt5（源内 5.15.11 原生 loong64 包），退化为 PyGObject + GTK3
* 文件管理器 / 双击打开（KDE Dolphin / xdg-open 在 MIPS 之外的表现差异）
* 通知（KDE 通过 notify-send）
* 中文路径、16K 页大小下的大文件 IO
"""

import os
import shutil
import subprocess
import sys

APP_NAME = "iPhoBack"
APP_ID = "iphoback"
APP_VERSION = "1.0.0"

# 龙芯桌面常见默认值
DEFAULT_BACKUP_DIR = os.path.expanduser("~/图片/iPhone备份")


# --------------------------------------------------------------------------
# 桌面环境探测
# --------------------------------------------------------------------------
def desktop_env():
    for key in ("XDG_CURRENT_DESKTOP", "KDE_FULL_SESSION", "DESKTOP_SESSION"):
        val = os.environ.get(key)
        if val:
            return val.split(":")[0].lower()
    return "unknown"


def is_loongnix():
    for path in ("/etc/loongnix_version", "/etc/loongnix-release"):
        if os.path.exists(path):
            return True
    try:
        with open("/etc/os-release", encoding="utf-8") as fh:
            return "loongnix" in fh.read().lower()
    except OSError:
        return False


def platform_summary():
    """给「关于」对话框用的平台信息。"""
    import platform
    machine = platform.machine()
    pretty = ""
    try:
        with open("/etc/os-release", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("PRETTY_NAME="):
                    pretty = line.split("=", 1)[1].strip().strip('"')
                    break
    except OSError:
        pass
    return "%s  %s  Python %d.%d" % (pretty or "Linux", machine,
                                    sys.version_info[0], sys.version_info[1])


# --------------------------------------------------------------------------
# 工具栏
# --------------------------------------------------------------------------
TOOL_DESCRIPTIONS = {
    "idevice_id": "识别连接的 iPhone",
    "ideviceinfo": "读取设备型号与固件版本",
    "idevicepair": "与本机配对（信任此电脑）",
    "idevicebackup2": "完整设备备份",
    "ifuse": "挂载设备文件系统",
    "idevicescreenshot": "设备截图",
}

INSTALL_CMD = "sudo apt install -y libimobiledevice-utils usbmuxd ifuse"


def find_tool(name):
    return shutil.which(name)


def missing_tools(names):
    return [n for n in names if not find_tool(n)]


def run_tool(args, timeout=25):
    """运行 libimobiledevice 命令行工具，统一返回 (code, stdout, stderr)。"""
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout, env=_tool_env())
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except FileNotFoundError:
        return 127, "", "找不到命令：%s" % args[0]
    except subprocess.TimeoutExpired:
        return 124, "", "命令超时（%d 秒）：%s" % (timeout, " ".join(args))
    except OSError as exc:
        return 1, "", str(exc)


def _tool_env():
    """确保即使通过桌面图标启动，也能找到 /usr/sbin 下的 usbmuxd 相关工具。"""
    env = os.environ.copy()
    extra = [p for p in ("/usr/sbin", "/usr/local/sbin", "/sbin") if p not in env.get("PATH", "")]
    if extra:
        env["PATH"] = ":".join(extra) + ":" + env.get("PATH", "")
    return env


def notify(title, body, summary=None):
    """KDE 桌面通知，失败静默（不打断备份）。"""
    if find_tool("notify-send"):
        try:
            subprocess.Popen(["notify-send", "-a", APP_NAME, "-i", "camera-photo",
                              title, summary or body],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except OSError:
            pass
    return False


def open_path(path):
    """用系统默认方式打开目录（Dolphin / 文件管理器）。"""
    if not os.path.exists(path):
        return False
    for cmd in (["xdg-open", path], ["dolphin", path], ["dde-file-manager", path]):
        if find_tool(cmd[0]):
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except OSError:
                continue
    return False


# --------------------------------------------------------------------------
# GUI 工具包选择：PyQt5 优先，GTK3 兜底
# --------------------------------------------------------------------------
def detect_gui_toolkit():
    try:
        from PyQt5 import QtCore  # noqa: F401
        return "qt5"
    except Exception:
        pass
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # noqa: F401
        return "gtk3"
    except Exception:
        return None


def gui_install_hint():
    return ("请先安装图形界面依赖（任选其一）：\n"
            "  sudo apt install python3-pyqt5 python3-pyqt5.qtsvg\n"
            "  sudo apt install python3-gi gir1.2-gtk-3.0")


# --------------------------------------------------------------------------
# 文件系统辅助
# --------------------------------------------------------------------------
def human_size(num):
    if num is None:
        return "-"
    num = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0:
            return ("%d %s" % (num, unit)) if unit == "B" else ("%.1f %s" % (num, unit))
        num /= 1024.0
    return "%.1f PB" % num


def human_duration(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%02d:%02d" % (m, s)


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    return path


def free_space(path):
    """返回目标分区剩余字节数。"""
    probe = path
    while probe and not os.path.exists(probe):
        probe = os.path.dirname(probe)
    try:
        return shutil.disk_usage(probe or "/").free
    except OSError:
        return 0


def same_filesystem(a, b):
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def sanitize_component(name):
    """把设备名等用户可控字符串变成安全目录名（保留中文）。"""
    bad = '/\\:*?"<>|\0'
    cleaned = "".join("_" if ch in bad else ch for ch in name).strip(" .")
    return cleaned or "iPhone"


def long_path_warn(path):
    """ext4 单文件路径上限 4096 字节；中文名按 3 字节算，做提前提醒。"""
    try:
        return len(path.encode("utf-8")) > 3800
    except Exception:
        return False
