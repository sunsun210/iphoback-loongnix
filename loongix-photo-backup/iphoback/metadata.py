# -*- coding: utf-8 -*-
"""照片元数据解析。

iPhone 相册的比较麻烦的几件事，都在这里处理：

1. **真实拍摄时间**：文件系统 mtime 不可靠（拷来拷去会变）。优先取
   DCIM/.MISC/PhotoData 下同名 PLIST 的 CreationDate（Apple 自己记的），
   其次读文件内 EXIF/QuickTime 头，最后才退回 mtime。
2. **Live Photo（实况照片）**：HEIC/JPG 与同名 MOV 配对。备份时必须成对保留，
   否则实况效果丢失。
3. **HEIC**：iPhone 默认格式，龙芯桌面上的看图软件不一定认，
   所以额外生成一份 JPEG 预览（原图无损保留）。
4. **重复文件**：从 iCloud 同步回来的会带 IMG_E1234、IMG_O1234 等前缀，
   需要认出来并去重，避免反复备份。
"""

import datetime as dt
import hashlib
import os
import plistlib
import struct

# 常见照片/视频扩展名（全大写出现在 DCIM 里）
PHOTO_EXT = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".dng", ".tif", ".tiff",
             ".gif", ".bmp", ".webp"}
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".avi", ".3gp", ".caf"}
LIVE_EXT = {".mov"}  # 实况照片的视频部分

# 文件名前缀：IMG_1234 IMG_E1234(编辑过) IMG_O1234(原图) PXL_20240101_...
_DUP_PREFIXES = ("IMG_E", "IMG_O", "IMG_F")


def ext_of(name):
    return os.path.splitext(name)[1].lower()


def is_photo(name):
    return ext_of(name) in PHOTO_EXT


def is_video(name):
    return ext_of(name) in VIDEO_EXT


def media_type(name):
    e = ext_of(name)
    if e in PHOTO_EXT:
        return "photo"
    if e in VIDEO_EXT:
        return "video"
    return "other"


def stem(name):
    return os.path.splitext(name)[0]


def canonical_stem(name):
    """把 IMG_E1234 / IMG_O1234 归一成 IMG_1234，用于去重与实况配对。"""
    s = stem(name)
    for p in _DUP_PREFIXES:
        if s.startswith(p):
            return "IMG_" + s[len(p):]
    return s


def is_edited_copy(name):
    return stem(name).startswith("IMG_E")


# --------------------------------------------------------------------------
# 时间解析
# --------------------------------------------------------------------------
_EXIF_TAG_DATETIME_ORIGINAL = 0x9003
_EXIF_TAG_DATETIME = 0x0132


def _parse_exif_datetime(raw):
    if not raw:
        return None
    txt = raw.decode("ascii", "ignore").strip("\x00 ").strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(txt, fmt)
        except ValueError:
            continue
    return None


def read_exif_datetime(fp):
    """从已打开的二进制文件里读 EXIF DateTimeOriginal。失败返回 None。

    手写最小 TIFF/IFD 解析器：不依赖 Pillow / exiftool，
    这样在 Loongnix 上不需要额外装包也能拿到拍摄时间。
    """
    try:
        fp.seek(0)
        head = fp.read(4)
        if head[:2] != b"\xff\xd8":       # 不是 JPEG；HEIC 的 EXIF 在 meta box 里，此处不解析
            return None
        while True:
            marker = fp.read(2)
            if len(marker) < 2 or marker[0] != 0xFF:
                return None
            if marker == b"\xff\xe1":      # APP1
                length = struct.unpack(">H", fp.read(2))[0]
                payload = fp.read(length - 2)
                if payload[:6] != b"Exif\x00\x00":
                    continue
                return _exif_from_tiff(payload[6:])
            if marker in (b"\xff\xda", b"\xff\xd9"):  # 到图像数据了，没有 EXIF
                return None
            length = struct.unpack(">H", fp.read(2))[0]
            fp.seek(length - 2, os.SEEK_CUR)
    except (OSError, struct.error, ValueError):
        return None


def _exif_from_tiff(tiff):
    try:
        if tiff[:2] == b"II":
            endian = "<"
        elif tiff[:2] == b"MM":
            endian = ">"
        else:
            return None
        offset = struct.unpack(endian + "I", tiff[4:8])[0]
        count = struct.unpack(endian + "H", tiff[offset:offset + 2])[0]
        best = None
        for i in range(count):
            base = offset + 2 + i * 12
            entry = tiff[base:base + 12]
            if len(entry) < 12:
                break
            tag, typ, cnt = struct.unpack(endian + "HHI", entry[:8])
            if tag == 0x8769:  # ExifIFD 指针
                sub_off = struct.unpack(endian + "I", entry[8:12])[0]
                sub_count = struct.unpack(endian + "H", tiff[sub_off:sub_off + 2])[0]
                for j in range(sub_count):
                    sb = sub_off + 2 + j * 12
                    s_entry = tiff[sb:sb + 12]
                    if len(s_entry) < 12:
                        break
                    s_tag, s_typ, s_cnt = struct.unpack(endian + "HHI", s_entry[:8])
                    if s_tag == _EXIF_TAG_DATETIME_ORIGINAL and s_cnt >= 19:
                        val_off = struct.unpack(endian + "I", s_entry[8:12])[0]
                        got = _parse_exif_datetime(tiff[val_off:val_off + 19])
                        if got:
                            return got
            elif tag == _EXIF_TAG_DATETIME and cnt >= 19:
                val_off = struct.unpack(endian + "I", entry[8:12])[0]
                got = _parse_exif_datetime(tiff[val_off:val_off + 19])
                if got and not best:
                    best = got
        return best
    except (struct.error, IndexError, ValueError):
        return None


def read_mov_creation_time(fp):
    """QuickTime/MP4 的 creation_time（1904 纪元）。"""
    try:
        fp.seek(0)
        head = fp.read(32)
        if len(head) < 24 or head[4:8] != b"ftyp":
            return None
        # mvhd 通常紧跟 ftyp/moov，做一次浅扫描
        fp.seek(0)
        blob = fp.read(4096)
        idx = blob.find(b"mvhd")
        if idx < 0:
            return None
        start = idx + 4
        version = blob[start]
        if version == 0:
            seconds = struct.unpack(">I", blob[start + 4:start + 8])[0]
        else:
            seconds = struct.unpack(">Q", blob[start + 4:start + 12])[0]
        if seconds <= 0:
            return None
        return dt.datetime(1904, 1, 1) + dt.timedelta(seconds=seconds)
    except (OSError, struct.error, IndexError):
        return None


# --------------------------------------------------------------------------
# Apple PLIST 元数据
# --------------------------------------------------------------------------
class PhotoMeta:
    """单张照片/视频的元数据。"""

    __slots__ = ("filename", "taken", "media_type", "paired", "hdr",
                 "source", "live_pair", "size", "afc_path")

    def __init__(self, filename):
        self.filename = filename
        self.taken = None
        self.media_type = media_type(filename)
        self.paired = False
        self.hdr = False
        self.source = "fs"        # fs / plist / exif / mov
        self.live_pair = None
        self.size = 0
        self.afc_path = None

    def __repr__(self):
        return "<PhotoMeta %s %s %s>" % (
            self.filename, self.taken and self.taken.isoformat(), self.source)


def load_plist_metadata(read_bytes, names):
    """解析一批 PLIST 文件，返回 {stem: {taken, media_type, live_pair, hdr}}。

    read_bytes(path) -> bytes
    """
    out = {}
    for name in names:
        if ext_of(name) != ".plist":
            continue
        try:
            raw = read_bytes(name)
        except Exception:
            continue
        try:
            data = plistlib.loads(raw)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        key = canonical_stem(stem(name))
        taken = data.get("CreationDate")
        if isinstance(taken, dt.datetime):
            # Apple 存的是 UTC，转本地时区，避免晚上拍的照片跨天
            if taken.tzinfo is not None:
                taken = taken.astimezone().replace(tzinfo=None)
            else:
                taken = taken + (dt.datetime.now() - dt.datetime.utcnow())
        else:
            taken = None
        live_pair = None
        priv = data.get("PrivateComputedAssetAttributes") or {}
        if isinstance(priv, dict):
            lp = priv.get("LivePhoto") or {}
            if isinstance(lp, dict):
                live_pair = lp.get("pairedVideoAsset")
        out[key] = {
            "taken": taken,
            "media_type": data.get("MediaType") or "Photo",
            "live_pair": live_pair,
            "hdr": bool(data.get("HDRGainMapVersion")),
            "original_name": data.get("OriginalFilename") or stem(name),
        }
    return out


# --------------------------------------------------------------------------
# 校验与去重
# --------------------------------------------------------------------------
def file_fingerprint(path, sample=256 * 1024):
    """快速指纹：大小 + 首尾采样哈希。用于增量备份跳过已备份文件。"""
    try:
        size = os.path.getsize(path)
        h = hashlib.blake2b(digest_size=12)
        h.update(str(size).encode())
        with open(path, "rb") as fh:
            h.update(fh.read(sample))
            if size > sample * 2:
                fh.seek(-sample, os.SEEK_END)
                h.update(fh.read(sample))
        return h.hexdigest()
    except OSError:
        return None


def full_hash(path, chunk=1024 * 1024):
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def sniff_kind(path):
    """按文件魔数判断真实类型，避免扩展名撒谎导致后端转换失败。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return "unknown"
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftyphevc", b"ftypmsf1"):
        return "heic"
    if head[4:8] == b"ftyp":
        return "mov" if b"qt" in head[8:16] else "mp4"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"caff":
        return "caf"
    return "unknown"
