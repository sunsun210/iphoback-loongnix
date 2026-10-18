# -*- coding: utf-8 -*-
"""备份引擎：扫描设备相册 → 生成计划 → 增量复制 → 落库索引。

设计要点（针对龙芯桌面 / 2K3000 这类性能有限的机器）：

* **只读**：全程不给设备写入任何东西，AFC 以只读方式打开文件。
* **增量**：索引库里记录每个文件的 (大小, mtime) 与指纹，已备份的直接跳过。
* **安全落盘**：先写 `.iphoback-part` 临时文件，落盘后 fsync 再改名，
  保证拔线/断电不会留下半个文件污染备份。
* **断点可续**：中断后重新开始会复用临时文件的大小，从断点继续。
* **不阻塞 UI**：全部跑在 QThread 里，通过信号回报进度。
"""

import datetime as dt
import json
import os
import re
import sqlite3
import threading
import time

from . import metadata as md
from .platform_lnx import ensure_dir, free_space, human_size, sanitize_component

CHUNK = 256 * 1024
PART_SUFFIX = ".iphoback-part"
INDEX_NAME = ".iphoback-index.db"

MONTH_DIRS = {
    "year": "%Y",
    "month": "%Y-%m",
    "day": "%Y-%m-%d",
}


# --------------------------------------------------------------------------
# 进度回调
# --------------------------------------------------------------------------
class Progress:
    def __init__(self, on_event):
        self.on_event = on_event or (lambda *a: None)

    def emit(self, kind, **kw):
        kw["kind"] = kind
        try:
            self.on_event(kw)
        except Exception:
            pass


# --------------------------------------------------------------------------
# 扫描结果
# --------------------------------------------------------------------------
class ScanResult:
    def __init__(self):
        self.items = []            # [Asset]
        self.total_bytes = 0
        self.dirs_scanned = 0
        self.warnings = []
        self.duplicates = 0
        self.live_pairs = 0
        self.device_names = []


class Asset:
    """一个待备份的实体（照片 / 视频 / 实况视频 / 其它文件）。"""

    __slots__ = ("afc_path", "name", "size", "mtime", "taken", "kind",
                 "is_live_video", "base_name", "sidecars", "date_source")

    def __init__(self, afc_path, name, size, mtime):
        self.afc_path = afc_path
        self.name = name
        self.size = size
        self.mtime = mtime
        self.taken = None
        self.kind = md.media_type(name)
        self.is_live_video = False
        self.base_name = md.canonical_stem(name)
        self.sidecars = []
        self.date_source = "none"

    def __repr__(self):
        return "<Asset %s %s>" % (self.name, human_size(self.size))


# --------------------------------------------------------------------------
# 扫描器
# --------------------------------------------------------------------------
class Scanner:
    """遍历设备 DCIM 目录，建立文件清单，并补齐拍摄时间。

    模式：
      camera  —— 只扫 DCIM（相机胶卷），速度快，兼容性最好；
      deep    —— 额外扫 PhotoData 等共享目录，可能含缩略图与重复文件。
    """

    MAX_DEPTH = 4
    # 这些目录即使带点也要进去（元数据在这里）；
    # 下面这些是无用隐藏项，直接跳过，省掉大量无谓的 stat。
    SKIP_NAMES = {".thumbnails", ".Trash", ".DS_Store", ".tmp", ".fcpcache"}

    def __init__(self, client, progress=None, mode="camera"):
        self.client = client
        self.progress = progress or Progress(None)
        self.mode = mode

    # -- 目录发现 ------------------------------------------------------
    def _root_dirs(self):
        roots = []
        for base in ("/DCIM",):
            if self.client.exists(base):
                roots.append(base)
        if self.mode == "deep":
            for base in ("/PhotoData", "/DCIM/.thumbnails"):
                if self.client.exists(base) and base != "/DCIM/.thumbnails":
                    roots.append(base)
        return roots

    def scan(self, cancel=None):
        result = ScanResult()
        roots = self._root_dirs()
        if not roots:
            raise RuntimeError("设备上没有找到 DCIM 目录；请确认已点「信任」并且手机上存在照片。")

        seen = {}          # (canonical_stem, size) -> Asset，用于去重
        for root in roots:
            self._walk(root, 0, result, seen, cancel)

        self._resolve_dates(result, cancel)
        self._pair_live(result)
        result.device_names = [a.name for a in result.items]
        return result

    def _walk(self, path, depth, result, seen, cancel):
        if cancel and cancel.is_set():
            return
        if depth > self.MAX_DEPTH:
            return
        try:
            names = self.client.listdir(path)
        except Exception as exc:
            result.warnings.append("跳过目录 %s：%s" % (path, exc))
            return
        result.dirs_scanned += 1
        self.progress.emit("scan_dir", path=path, dirs=result.dirs_scanned,
                           files=len(result.items))
        for name in names:
            if cancel and cancel.is_set():
                return
            # 注意：不能一概跳过 "." 开头的条目，DCIM/.MISC/PhotoData 里存的是
            # 拍摄时间元数据，跳过了就只能退回文件 mtime，归档日期会错。
            if name in (".", "..") or name in self.SKIP_NAMES:
                continue
            child = path.rstrip("/") + "/" + name
            try:
                info = self.client.stat(child)
            except Exception:
                continue
            if info.get("mode") == "S_IFDIR" or self.client.is_dir(child):
                self._walk(child, depth + 1, result, seen, cancel)
                continue
            kind = md.media_type(name)
            if kind == "other" and not md.ext_of(name) in (".aaE", ".plist", ".xmp"):
                continue
            if md.ext_of(name) in (".plist", ".xmp", ".aae"):
                continue
            asset = Asset(child, name, info.get("size", 0), info.get("mtime", 0))
            key = (asset.base_name, asset.size)
            if key in seen:
                result.duplicates += 1
                continue
            seen[key] = asset
            result.items.append(asset)
            result.total_bytes += asset.size
        time.sleep(0)      # 让出 GIL，UI 线程能刷新

    # -- 拍摄时间 ------------------------------------------------------
    def _resolve_dates(self, result, cancel):
        """批量读取 PLIST，得到权威拍摄时间。"""
        plist_dirs = ["/DCIM/.MISC/PhotoData"]
        found_plists = []
        for d in plist_dirs:
            try:
                if not self.client.exists(d):
                    continue
                names = self.client.listdir(d)
            except Exception:
                continue
            found_plists.extend((d, n) for n in names if md.ext_of(n) == ".plist")

        meta = {}
        if found_plists:
            self.progress.emit("scan_meta", total=len(found_plists), done=0)
            for i in range(0, len(found_plists), 200):
                if cancel and cancel.is_set():
                    break
                chunk = [n for _, n in found_plists[i:i + 200]]
                meta.update(_load_plists(self.client, found_plists, chunk))
                self.progress.emit("scan_meta", total=len(found_plists),
                                   done=min(i + 200, len(found_plists)))

        for asset in result.items:
            m = meta.get(asset.base_name)
            if m and m.get("taken"):
                asset.taken = m["taken"]
                asset.date_source = "plist"
                if m.get("hdr"):
                    asset.sidecars.append("hdr")
                if m.get("live_pair"):
                    asset.sidecars.append("live:" + md.canonical_stem(m["live_pair"]))
            if asset.taken is None and asset.mtime:
                asset.taken = dt.datetime.fromtimestamp(asset.mtime)
                asset.date_source = "mtime"

        # 二次校正：PLIST 时间可疑（例如与文件 mtime 差得离谱、或是"今天"却来自旧文件）时，
        # 抽查文件内嵌 EXIF / QuickTime 时间。只抽查，避免整机扫描时读全部文件头。
        self._refine_dates(result, cancel)

    def _refine_dates(self, result, cancel, sample_limit=400):
        suspicious = [a for a in result.items
                      if a.date_source == "plist" and _date_suspicious(a)]
        if not suspicious:
            return
        self.progress.emit("scan_refine", total=min(len(suspicious), sample_limit))
        for a in suspicious[:sample_limit]:
            if cancel and cancel.is_set():
                return
            try:
                got = _read_embedded_date(self.client, a)
            except Exception:
                got = None
            if got:
                a.taken = got
                a.date_source = "exif"

    # -- 实况照片配对 ---------------------------------------------------
    @staticmethod
    def _pair_live(result):
        by_stem = {}
        for a in result.items:
            if md.is_video(a.name):
                by_stem.setdefault(a.base_name, []).append(a)
        for a in result.items:
            if md.is_photo(a.name) and a.base_name in by_stem:
                for v in by_stem[a.base_name]:
                    v.is_live_video = True
                    v.taken = v.taken or a.taken
                    result.live_pairs += 1


def _date_suspicious(asset):
    """PLIST 时间是否可疑：未来时间，或与文件 mtime 相差超过 400 天。"""
    if not asset.taken:
        return True
    now = dt.datetime.now()
    if asset.taken > now + dt.timedelta(days=1):
        return True
    if asset.mtime:
        mtime = dt.datetime.fromtimestamp(asset.mtime)
        if abs((asset.taken - mtime).days) > 400:
            return True
    return False


def _read_embedded_date(client, asset):
    """读文件头部，拿 EXIF DateTimeOriginal / QuickTime 创建时间。"""
    import io
    ext = md.ext_of(asset.name)
    want = 256 * 1024 if ext in (".mov", ".mp4", ".m4v") else 64 * 1024
    buf = bytearray()
    for chunk in client.read_file(asset.afc_path):
        buf.extend(chunk)
        if len(buf) >= want:
            break
    if not buf:
        return None
    bio = io.BytesIO(bytes(buf))
    if ext in (".mov", ".mp4", ".m4v"):
        return md.read_mov_creation_time(bio)
    return md.read_exif_datetime(bio)


def _parse_plist_bytes(raw):
    """解析 PLIST；标准解析失败时退化为正则提取。

    真机上偶尔会遇到半截写入 / 非标准 <date> 的 PLIST，
    这时不能整条丢掉——丢掉就只能退回 mtime，归档日期会错。
    """
    import plistlib
    try:
        data = plistlib.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return _plist_regex_fallback(raw)


_DATE_RE = re.compile(rb"<key>CreationDate</key>\s*<date>([^<]+)</date>")
_STR_RE = re.compile(rb"<key>(%s)</key>\s*<string>([^<]*)</string>")
_HDR_RE = re.compile(rb"<key>HDRGainMapVersion</key>")
_LIVE_RE = re.compile(
    rb"<key>pairedVideoAsset</key>\s*<string>([^<]+)</string>")


def _plist_regex_fallback(raw):
    out = {}
    m = _DATE_RE.search(raw)
    if m:
        txt = m.group(1).decode("ascii", "ignore").strip().rstrip("Z")
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
            try:
                out["CreationDate"] = dt.datetime.strptime(txt, fmt)
                break
            except ValueError:
                continue
    live = _LIVE_RE.search(raw)
    if live:
        val = live.group(1).decode("utf-8", "replace")
        out["PrivateComputedAssetAttributes"] = {"LivePhoto": {"pairedVideoAsset": val}}
    if _HDR_RE.search(raw):
        out["HDRGainMapVersion"] = 131072
    return out or None


def _load_plists(client, listing, names):
    """读取一批 PLIST 并解析（独立函数便于测试替换）。"""
    out = {}
    dir_of = {}
    for d, n in listing:
        dir_of[n] = d
    for name in names:
        d = dir_of.get(name)
        if not d:
            continue
        try:
            raw = b"".join(client.read_file(d.rstrip("/") + "/" + name))
        except Exception:
            continue
        data = _parse_plist_bytes(raw)
        if not data:
            continue
        key = md.canonical_stem(md.stem(name))
        taken = data.get("CreationDate")
        if isinstance(taken, dt.datetime):
            if taken.tzinfo is not None:
                taken = taken.astimezone().replace(tzinfo=None)
        else:
            taken = None
        live = None
        priv = data.get("PrivateComputedAssetAttributes") or {}
        if isinstance(priv, dict):
            lp = priv.get("LivePhoto") or {}
            if isinstance(lp, dict):
                live = lp.get("pairedVideoAsset")
        out[key] = {"taken": taken, "hdr": bool(data.get("HDRGainMapVersion")),
                    "live_pair": live}
    return out


# --------------------------------------------------------------------------
# 索引库
# --------------------------------------------------------------------------
class IndexDb:
    """备份索引（SQLite），放在备份根目录下，便于整盘搬走后仍然可用。"""

    def __init__(self, root):
        self.path = os.path.join(root, INDEX_NAME)
        ensure_dir(root)
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS assets (
                key         TEXT PRIMARY KEY,   -- 设备UDID|规范化文件名|大小
                src_name    TEXT,
                rel_path    TEXT,
                size        INTEGER,
                mtime       INTEGER,
                taken       TEXT,
                fingerprint TEXT,
                backed_at   TEXT,
                sha         TEXT
            );
            CREATE TABLE IF NOT EXISTS runs (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                started   TEXT, finished TEXT,
                files     INTEGER, bytes INTEGER,
                skipped   INTEGER, failed INTEGER,
                note      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_assets_taken ON assets(taken);
        """)
        self.conn.commit()

    def has(self, key):
        with self.lock:
            cur = self.conn.execute("SELECT 1 FROM assets WHERE key=?", (key,))
            return cur.fetchone() is not None

    def get(self, key):
        with self.lock:
            cur = self.conn.execute(
                "SELECT rel_path, size, mtime, fingerprint, sha FROM assets WHERE key=?", (key,))
            row = cur.fetchone()
        if not row:
            return None
        return {"rel_path": row[0], "size": row[1], "mtime": row[2],
                "fingerprint": row[3], "sha": row[4]}

    def put(self, key, src_name, rel_path, size, mtime, taken, fingerprint, sha=None):
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO assets"
                "(key,src_name,rel_path,size,mtime,taken,fingerprint,backed_at,sha)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (key, src_name, rel_path, size, mtime,
                 taken.isoformat() if taken else None, fingerprint,
                 dt.datetime.now().isoformat(timespec="seconds"), sha))
            self.conn.commit()

    def forget(self, key):
        with self.lock:
            self.conn.execute("DELETE FROM assets WHERE key=?", (key,))
            self.conn.commit()

    def add_run(self, started, finished, files, nbytes, skipped, failed, note=""):
        with self.lock:
            self.conn.execute(
                "INSERT INTO runs(started,finished,files,bytes,skipped,failed,note)"
                " VALUES(?,?,?,?,?,?,?)",
                (started, finished, files, nbytes, skipped, failed, note))
            self.conn.commit()

    def stats(self):
        with self.lock:
            n, total = self.conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(size),0) FROM assets").fetchone()
            last = self.conn.execute(
                "SELECT finished, files, bytes FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return {"files": n, "bytes": total, "last_run": last}

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 计划
# --------------------------------------------------------------------------
class PlanItem:
    __slots__ = ("asset", "action", "rel_path", "reason", "resume_from")

    def __init__(self, asset, action, rel_path, reason="", resume_from=0):
        self.asset = asset
        self.action = action        # copy / skip / resume
        self.rel_path = rel_path
        self.reason = reason
        self.resume_from = resume_from


def build_plan(scan, dest_root, index, device_udid, layout="month",
               include_videos=True, only_newer_than=None, include_live=True,
               cancel=None):
    """把扫描结果变成可执行计划。"""
    fmt = MONTH_DIRS.get(layout, "%Y-%m")
    items = []
    for a in scan.items:
        if a.kind == "video" and not include_videos:
            continue
        if a.is_live_video and not include_live:
            continue
        if only_newer_than and a.taken and a.taken < only_newer_than:
            continue
        rel_dir = a.taken.strftime(fmt) if a.taken else "未知日期"
        rel_path = os.path.join(rel_dir, a.name)
        key = make_key(device_udid, a)
        entry = index.get(key)
        target = os.path.join(dest_root, rel_path)
        ensure_dir(os.path.dirname(target))

        if entry and os.path.exists(target):
            try:
                cur_size = os.path.getsize(target)
            except OSError:
                cur_size = -1
            if cur_size == a.size and entry.get("rel_path") == rel_path:
                items.append(PlanItem(a, "skip", rel_path, "已备份"))
                continue
            if cur_size == a.size:
                # 内容一致但归档日期变了（例如时间戳修正过）→ 不算重复下载
                items.append(PlanItem(a, "skip", rel_path, "已备份（路径更新）"))
                index.put(key, a.name, rel_path, a.size, a.mtime, a.taken,
                          entry.get("fingerprint"))
                continue

        part = target + PART_SUFFIX
        resume = 0
        if os.path.exists(part):
            try:
                resume = os.path.getsize(part)
            except OSError:
                resume = 0
            if resume > a.size:
                resume = 0
                try:
                    os.unlink(part)
                except OSError:
                    pass
        if resume and resume == a.size:
            items.append(PlanItem(a, "resume", rel_path, "临时文件已完整"))
        elif resume:
            items.append(PlanItem(a, "resume", rel_path, "从断点续传", resume))
        else:
            items.append(PlanItem(a, "copy", rel_path))
        if cancel and cancel.is_set():
            break
    return items


def make_key(udid, asset):
    return "%s|%s|%d" % (udid or "unknown", asset.base_name, asset.size)


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------
class BackupEngine:
    #: 连续失败多少次判定为设备断开并中止
    MAX_CONSECUTIVE_FAIL = 3

    def __init__(self, client, dest_root, progress=None, cancel=None):
        self.client = client
        self.dest_root = dest_root
        self.progress = progress or Progress(None)
        self.cancel = cancel or threading.Event()
        self.index = IndexDb(dest_root)
        self.stats = {"files": 0, "bytes": 0, "skipped": 0, "failed": 0,
                      "total_files": 0, "total_bytes": 0, "started": time.time()}
        self.errors = []

    def run(self, plan, udid=None, verify_hash=False):
        self.stats["total_files"] = sum(1 for p in plan if p.action != "skip")
        self.stats["total_bytes"] = sum(p.asset.size - p.resume_from
                                        for p in plan if p.action != "skip")
        started_at = dt.datetime.now().isoformat(timespec="seconds")
        done_bytes = 0
        done_files = 0

        for item in plan:
            if self.cancel.is_set():
                self.progress.emit("cancelled", **self.stats)
                break
            if item.action == "skip":
                self.stats["skipped"] += 1
                continue
            try:
                n = self._copy_one(item, udid, verify_hash)
                done_bytes += n
                done_files += 1
                self.stats["files"] += 1
                self.stats["bytes"] += n
                self.progress.emit(
                    "file_done", name=item.asset.name, path=item.rel_path,
                    bytes=done_bytes, total_bytes=self.stats["total_bytes"],
                    files=done_files, total_files=self.stats["total_files"],
                    index=self.stats["files"])
            except InterruptedError:
                self.progress.emit("cancelled", **self.stats)
                break
            except Exception as exc:
                self.stats["failed"] += 1
                self.stats["consecutive_fail"] = self.stats.get("consecutive_fail", 0) + 1
                self.errors.append("%s: %s" % (item.asset.name, exc))
                self.progress.emit("file_failed", name=item.asset.name, error=str(exc),
                                   failed=self.stats["failed"])
                # 连续失败通常意味着设备已断开/被锁定，继续跑只会把剩下的文件
                # 全试一遍并留下大量垃圾临时文件，直接中止更符合预期。
                if self.stats["consecutive_fail"] >= self.MAX_CONSECUTIVE_FAIL:
                    self.stats["aborted"] = True
                    self.progress.emit("aborted", reason=str(exc),
                                       failed=self.stats["failed"])
                    break
            else:
                self.stats["consecutive_fail"] = 0

        elapsed = max(0.001, time.time() - self.stats["started"])
        self.stats["elapsed"] = elapsed
        self.stats["speed"] = self.stats["bytes"] / elapsed if self.stats["bytes"] else 0
        self.index.add_run(started_at, dt.datetime.now().isoformat(timespec="seconds"),
                           self.stats["files"], self.stats["bytes"],
                           self.stats["skipped"], self.stats["failed"],
                           note="cancelled" if self.cancel.is_set() else "ok")
        self.progress.emit("done", **self.stats)
        return self.stats

    def make_previews(self, udid=None, layout="month"):
        """为备份中的 HEIC 生成 JPG 预览（原图不动，存「预览/」子目录）。"""
        from . import preview as pv
        rels = [row[0] for row in self.index.conn.execute(
            "SELECT rel_path FROM assets WHERE rel_path LIKE ? OR rel_path LIKE ?",
            ("%.HEIC", "%.heic"))]
        if not rels:
            return None
        return pv.batch_previews(self.dest_root, rels,
                                 progress=self.progress.on_event, cancel=self.cancel)

    def _copy_one(self, item, udid, verify_hash):
        asset = item.asset
        target = os.path.join(self.dest_root, item.rel_path)
        ensure_dir(os.path.dirname(target))
        part = target + PART_SUFFIX

        if item.action == "resume" and item.resume_from >= asset.size and asset.size > 0:
            os.replace(part, target)
            self._index_put(item, udid, target, verify_hash)
            return asset.size

        written = item.resume_from if item.action == "resume" else 0
        if written:
            self.progress.emit("resume_file", name=asset.name, offset=written)

        mode = "ab" if written else "wb"
        hasher = None
        if verify_hash:
            import hashlib
            hasher = hashlib.blake2b(digest_size=16)
            if written:
                with open(part, "rb") as fh:
                    while True:
                        b = fh.read(1024 * 1024)
                        if not b:
                            break
                        hasher.update(b)

        with open(part, mode) as fh:
            # 断点续传：后端支持 offset 就直接跳过已下载字节；否则重读并丢弃前缀
            use_offset = written and getattr(self.client, "supports_offset", False)
            stream = (self.client.read_file(asset.afc_path, offset=written)
                      if use_offset else self.client.read_file(asset.afc_path))
            skip = 0 if use_offset else written
            received = written
            if use_offset:
                self.progress.emit("resume_file", name=asset.name, offset=written)
            try:
                for chunk in stream:
                    if self.cancel.is_set():
                        raise InterruptedError()
                    if skip:
                        if len(chunk) <= skip:
                            skip -= len(chunk)
                            continue
                        chunk = chunk[skip:]
                        skip = 0
                    fh.write(chunk)
                    fh.flush()          # 立即落盘：USB 掉线/拔线时已收字节不能丢
                    received += len(chunk)
                    if hasher is not None:
                        hasher.update(chunk)
                    self.progress.emit("progress", name=asset.name, done=received,
                                       size=asset.size)
            finally:
                # 关键：无论正常结束、取消还是 USB 掉线，都要把已收到的字节刷到磁盘，
                # 否则临时文件是 0 字节，"断点续传"等于从头再来。
                try:
                    fh.flush()
                    os.fsync(fh.fileno())
                except OSError:
                    pass

        written = received
        if written == 0:
            # 一个字节都没收到：这不是"断点"，是连接失败，别留空文件污染目录
            try:
                os.unlink(part)
            except OSError:
                pass
        if asset.size and written != asset.size:
            # 大小对不上：保留临时文件，下次续传；但本次算失败
            raise IOError("大小校验不符（期望 %d，实际 %d）" % (asset.size, written))

        os.replace(part, target)
        self._index_put(item, udid, target, verify_hash, hasher)
        return written

    def _index_put(self, item, udid, target, verify_hash, hasher=None):
        asset = item.asset
        fp = md.file_fingerprint(target)
        sha = hasher.hexdigest() if hasher is not None else None
        try:
            src_mtime = int(asset.taken.timestamp()) if asset.taken else asset.mtime
        except (OverflowError, OSError, ValueError):
            src_mtime = asset.mtime
        self.index.put(make_key(udid, asset), asset.name, item.rel_path,
                       asset.size, src_mtime, asset.taken, fp, sha)
        # 时间戳尽量保留（便于其它软件按拍摄时间排序）
        if asset.taken:
            try:
                ts = asset.taken.timestamp()
                os.utime(target, (ts, ts))
            except (OSError, OverflowError, ValueError):
                pass

    def close(self):
        self.index.close()


# --------------------------------------------------------------------------
# 备份前检查
# --------------------------------------------------------------------------
def _prepend(first, rest):
    yield first
    for chunk in rest:
        yield chunk


def preflight(dest_root, total_bytes):
    """返回 (ok, message)。检查磁盘空间与写权限。"""
    probe = dest_root
    while probe and not os.path.exists(probe):
        probe = os.path.dirname(probe)
    if not probe:
        return False, "备份目录无效：%s" % dest_root
    if not os.access(probe, os.W_OK):
        return False, "没有写入权限：%s" % probe
    free = free_space(dest_root)
    need = total_bytes * 1.05 + 16 * 1024 * 1024
    if free < need:
        return False, ("磁盘空间不足：需要约 %s，%s 上只剩 %s"
                       % (human_size(need), probe, human_size(free)))
    return True, ""


def default_dest(device_name):
    from .platform_lnx import DEFAULT_BACKUP_DIR
    return os.path.join(DEFAULT_BACKUP_DIR, sanitize_component(device_name or "iPhone"))
