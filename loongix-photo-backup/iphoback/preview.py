# -*- coding: utf-8 -*-
"""HEIC → JPEG 预览生成。

Loongnix 桌面自带的看图软件对 HEIC 支持不稳，备份时顺手生成一份
同名 JPG（放 `预览/` 子目录），日常浏览用；原图永远保持原样。

实现选择：系统 ffmpeg（Loongnix 源里是 7.1.3，带 libheif 解码）。
不用 Pillow——龙芯源里的 python3-pil 不带 HEIF 插件，装 pillow-heif
又得碰 PEP 668，不值得。
"""

import os
import shutil
import subprocess

from .platform_lnx import find_tool

PREVIEW_DIR = "预览"
PREVIEW_SUFFIX = ".jpg"


def ffmpeg_available():
    return bool(find_tool("ffmpeg"))


def make_preview(src, dest_root, rel_path, quality=2, progress=None):
    """为一张 HEIC 生成 JPG 预览。返回 (created, out_path 或错误信息)。"""
    stem = os.path.splitext(rel_path)[0]
    out = os.path.join(dest_root, PREVIEW_DIR, stem + PREVIEW_SUFFIX)
    if os.path.exists(out):
        return False, out
    if not ffmpeg_available():
        return False, "no-ffmpeg"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".tmp.jpg"
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
           "-i", src, "-map", "0:v:0", "-frames:v", "1",
           "-q:v", str(quality), tmp]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0 or not os.path.exists(tmp):
            return False, (proc.stderr or "ffmpeg 退出码 %d" % proc.returncode).strip()[:200]
        os.replace(tmp, out)
        # 预览文件与原图时间一致，方便文件管理器按时间排序
        st = os.stat(src)
        os.utime(out, (st.st_atime, st.st_mtime))
        return True, out
    except subprocess.TimeoutExpired:
        return False, "ffmpeg 超时"
    except OSError as exc:
        return False, str(exc)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def batch_previews(dest_root, rel_paths, progress=None, cancel=None, oneway=False):
    """批量生成预览。rel_paths 是已备份的 HEIC 相对路径列表。"""
    if not ffmpeg_available():
        if progress:
            progress({"kind": "preview_skip",
                      "reason": "未安装 ffmpeg，跳过 JPG 预览生成（sudo apt install ffmpeg）"})
        return {"made": 0, "skip": len(rel_paths), "failed": 0}
    made = failed = 0
    for i, rel in enumerate(rel_paths):
        if cancel and cancel.is_set():
            break
        src = os.path.join(dest_root, rel)
        ok, info = make_preview(src, dest_root, rel)
        if ok:
            made += 1
        else:
            failed += 1
        if progress and (i % 10 == 0 or i == len(rel_paths) - 1):
            progress({"kind": "preview_progress", "done": i + 1, "total": len(rel_paths)})
    if progress:
        progress({"kind": "preview_done", "made": made, "failed": failed})
    return {"made": made, "skip": 0, "failed": failed}
