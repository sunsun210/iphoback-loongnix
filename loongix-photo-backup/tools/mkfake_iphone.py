#!/usr/bin/env python3
"""iPhoBack 虚拟 iPhone 相册生成器（测试用）。

在指定目录下伪造 iPhone 的 DCIM 结构 + Apple PLIST 元数据 + iTunes 备份式清单，
让备份引擎可以在没有真实设备（也没有 libimobiledevice）的环境里跑完整流程。

用法：
    python3 tools/mkfake_iphone.py /tmp/fake_iphone --count 40
"""

import argparse
import datetime as dt
import os
import random
import struct
import wave
import zlib

try:
    from PIL import Image, ImageDraw
    HAS_PIL = True
except ImportError:  # 兜底：手写最小 PNG
    HAS_PIL = False


def _dt(days_ago):
    return dt.datetime.now() - dt.timedelta(days=days_ago, hours=random.randint(0, 23),
                                            minutes=random.randint(0, 59))


def make_jpeg(path, size, hue):
    w, h = size
    if HAS_PIL:
        img = Image.new("RGB", (w, h), hue)
        d = ImageDraw.Draw(img)
        for i in range(0, w, max(8, w // 12)):
            d.line([(i, 0), (w - i, h)], fill=(255, 255, 255), width=2)
        d.rectangle([6, 6, w - 6, h - 6], outline=(255, 255, 255), width=3)
        d.text((12, h // 2), "iPhoBack test", fill=(0, 0, 0))
        img.save(path, "JPEG", quality=88)
    else:
        _minimal_jpeg(path)


def _minimal_jpeg(path):
    """一个 1x1 灰色 JPEG，避免依赖 Pillow。"""
    data = bytes.fromhex(
        "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707"
        "070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c"
        "1c28372e3031343536351f293a3e3a333c36353435ffc0000b080001000101011100"
        "ffc4001f0000010501010101010100000000000000000102030405060708090a0b"
        "ffc400b5100002010303020403050504040000017d01020300041105122131410613"
        "516107227114328191a1082342b1c11552d1f02433627282090a161718191a252627"
        "28292a3435363738393a434445464748494a535455565758595a636465666768696a"
        "737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aa"
        "b2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7"
        "e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fefa0fffd9")
    with open(path, "wb") as fh:
        fh.write(data)


def make_heic(path):
    """伪造的 HEIC 文件：带 ftypheic 魔数，便于引擎识别扩展名与类型。"""
    with open(path, "wb") as fh:
        fh.write(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00heicmif1heic")
        fh.write(b"\x00" * 400)


def make_mov(path, seconds=1):
    """伪造的 QuickTime 容器（带 ftypqt 头）。"""
    with open(path, "wb") as fh:
        fh.write(b"\x00\x00\x00\x14ftypqt  \x00\x00\x02\x00qt  ")
        fh.write(b"\x00" * (900 * seconds))


def make_png(path, w=64, h=40):
    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload +
                struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))
    raw = b"".join(b"\x00" + b"\x80\xa0\xff" * w for _ in range(h))
    png = (b"\x89PNG\r\n\x1a\n" +
           chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
           chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(png)


def make_wav(path, seconds=1):
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        wf.writeframes(b"\x00\x00" * 8000 * seconds)


def write_plist(path, filename, taken, media_type="Photo", live=None, hdr=False):
    """模拟 iPhone 的真实 Apple PLIST（XML plist）。"""
    iso = taken.strftime("%Y-%m-%dT%H:%M:%SZ")   # Apple plist <date> 规范要求末尾带 Z
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
        '<plist version="1.0">', '<dict>',
        '\t<key>AdjustmentRenderType</key><integer>0</integer>',
        '\t<key>AssetIdentifier</key>',
        f'\t<string>{"F1C2E9A0-1234-4A5B-9C7D-" + filename[:12].upper()}</string>',
        '\t<key>CreationDate</key>', f'\t<date>{iso}</date>',
        '\t<key>OriginalFilename</key>', f'\t<string>{filename}</string>',
        '\t<key>MediaType</key>', f'\t<string>{media_type}</string>',
        '\t<key>Orientation</key><integer>1</integer>',
    ]
    if hdr:
        parts += ['\t<key>HDRGainMapVersion</key><integer>131072</integer>']
    if live:
        parts += ['\t<key>HasAdjustments</key><false/>',
                  '\t<key>PrivateComputedAssetAttributes</key><dict>',
                  '\t\t<key>LivePhoto</key><dict>',
                  '\t\t\t<key>still-image-time</key><real>0.5</real>',
                  '\t\t\t<key>pairedVideoAsset</key>',
                  f'\t\t\t<string>{live}</string>',
                  '\t\t</dict>', '\t</dict>']
    parts += ['</dict>', '</plist>']
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dest")
    ap.add_argument("--count", type=int, default=40)
    ap.add_argument("--seed", type=int, default=20250820)
    args = ap.parse_args()
    random.seed(args.seed)

    root = os.path.join(args.dest, "DCIM", "100APPLE")
    os.makedirs(root, exist_ok=True)
    lo, hi = 1, max(1, args.count // 3) + 1

    def ls(d):
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def free():
        """模拟 iPhone 的文件编号分配：取当前已用的最大编号 + 1。"""
        used = ls(root)
        nums = []
        for f in used:
            part = f.split("_")[-1].split(".")[0]
            if part.isdigit():
                nums.append(int(part))
        n = (max(nums) + 1) if nums else 1
        while True:
            name = "IMG_%04d" % n
            if not any(f.startswith(name + ".") for f in used):
                return name
            n += 1

    def info_dir():
        p = os.path.join(os.path.dirname(root), ".MISC", "PhotoData")
        os.makedirs(p, exist_ok=True)
        return p

    def add_asset(fmt="jpeg", kind="photo", live=True, duplicate=False, hdr=False):
        """kind: photo / video / live_mov / png / audio"""
        base = free()
        inst = [d for d in ls(root) if d.startswith("DCIM_") and d[5:].isdigit()]
        inst = inst[-1] if inst else ""
        sub = "PhotoData" if kind in ("live_mov", "png", "audio") else ""

        if kind == "photo":
            ext = ".HEIC" if fmt == "heic" else ".JPG"
            fn = base + ext
            target = os.path.join(root, inst, sub, fn)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if fmt == "heic":
                make_heic(target)
            else:
                make_jpeg(target, random.choice([(1600, 1200), (2000, 1500)]),
                          random.choice([(230, 140, 90), (90, 140, 200), (140, 200, 150)]))
            taken = _dt(random.randint(2, 400))
            write_plist(os.path.join(info_dir(), os.path.splitext(fn)[0] + ".PLIST"),
                        fn, taken, "Photo", hdr=hdr)
            if live and ext == ".HEIC":
                mov = base + ".MOV"
                make_mov(os.path.join(root, inst, sub, mov), 1)
                write_plist(os.path.join(info_dir(), os.path.splitext(mov)[0] + ".PLIST"),
                            mov, taken, "Video")
            return fn
        if kind == "video":
            fn = base + ".MOV"
            target = os.path.join(root, inst, sub, fn)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            make_mov(target, 2)
            write_plist(os.path.join(info_dir(), os.path.splitext(fn)[0] + ".PLIST"),
                        fn, _dt(random.randint(2, 400)), "Video")
            return fn
        if kind in ("png", "audio"):
            ext = ".PNG" if kind == "png" else ".CAF"
            fn = base + ext
            target = os.path.join(root, inst, sub, fn)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            (make_png if kind == "png" else make_wav)(target)
            return fn
        return base

    total = args.count
    made = 0
    while made < total:
        roll = random.random()
        if roll < 0.55:
            add_asset("heic", "photo")
        elif roll < 0.78:
            add_asset("jpeg", "photo")
        elif roll < 0.92:
            add_asset("jpeg", "video")
        else:
            add_asset("jpeg", "photo", live=False, hdr=True)
        if random.random() < 0.08:  # 重复文件：测试增量去重
            add_asset("heic", "photo")
        made += 1

    # 截图型图片（非相机）
    if random.random() < 0.6:
        add_asset("jpeg", "png")
        add_asset("jpeg", "png")
        add_asset("jpeg", "audio")
        add_asset("heic", "live_mov")

    print("已生成虚拟 iPhone 相册：", root)
    print("文件数：", sum(len(f) for _, _, f in os.walk(os.path.join(args.dest, "DCIM"))))


if __name__ == "__main__":
    main()
