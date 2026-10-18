#!/usr/bin/env python3
"""AFC 客户端模拟器（测试用）。

在不连接真机、不安装 libimobiledevice 的情况下，用一个本地目录冒充 iPhone
的 AFC 文件系统，供 iphoback 的扫描/备份引擎跑完整流程。

    from tools.fake_afc import FakeAfcClient
    client = FakeAfcClient("/tmp/fake_iphone")
"""

import os
import shutil


class AfcError(Exception):
    pass


class FakeAfcClient:
    """实现 engine.afc.IAfc 接口的子集。"""

    supports_offset = True

    def __init__(self, root, rate=0.0):
        self.root = root
        self.rate = rate          # 每 1KB 休眠秒数，模拟 USB 速率
        self.bytes_read = 0
        self.closed = False

    # --- 路径解析 -----------------------------------------------------
    def _p(self, afc_path):
        if self.closed:
            raise AfcError("client closed")
        rel = afc_path.strip("/").replace("/", os.sep)
        return os.path.join(self.root, rel) if rel else self.root

    # --- 接口实现 -----------------------------------------------------
    def exists(self, path):
        return os.path.exists(self._p(path))

    def is_dir(self, path):
        return os.path.isdir(self._p(path))

    def stat(self, path):
        st = os.stat(self._p(path))
        return {"size": st.st_size, "mtime": int(st.st_mtime),
                "mtime_ns": st.st_mtime_ns, "mode": st.st_mode}

    def listdir(self, path):
        try:
            return sorted(os.listdir(self._p(path)))
        except (FileNotFoundError, NotADirectoryError):
            raise AfcError("directory not found: %s" % path)

    def read_file(self, path, offset=0):
        with open(self._p(path), "rb") as fh:
            if offset:
                fh.seek(offset)
            while True:
                chunk = fh.read(256 * 1024)
                if not chunk:
                    break
                self.bytes_read += len(chunk)
                yield chunk

    def close(self):
        self.closed = True


class BrokenAfcClient(FakeAfcClient):
    """模拟 USB 在传输 N 个媒体文件后断开，用于测试断点续传。

    只对媒体文件（照片/视频）计数；读取 PLIST 等小元数据不计入，
    否则扫描阶段就会挂掉，测不到真正的续传路径。
    """

    MEDIA_EXT = (".jpg", ".jpeg", ".heic", ".png", ".mov", ".mp4", ".m4v", ".caf")

    def __init__(self, root, fail_after_files=3, fail_at_bytes=None):
        super().__init__(root)
        self.fail_after_files = fail_after_files
        self.fail_at_bytes = fail_at_bytes   # 第 N 个文件的第 X 字节处断线
        self._n = 0

    def read_file(self, path, offset=0):
        if not path.lower().endswith(self.MEDIA_EXT):
            for chunk in super().read_file(path, offset):
                yield chunk
            return
        self._n += 1
        if self._n > self.fail_after_files:
            raise AfcError("simulated USB disconnect")
        sent = offset
        for chunk in super().read_file(path, offset):
            if self.fail_at_bytes is not None and self._n == self.fail_after_files:
                room = self.fail_at_bytes - sent
                if room <= 0:
                    raise AfcError("simulated USB disconnect (mid-file)")
                if len(chunk) > room:
                    yield chunk[:room]
                    raise AfcError("simulated USB disconnect (mid-file)")
            sent += len(chunk)
            yield chunk


if __name__ == "__main__":
    import sys
    c = FakeAfcClient(sys.argv[1] if len(sys.argv) > 1 else "/tmp/fake_iphone")
    for p in ("/DCIM", "/DCIM/100APPLE", "/PhotoData"):
        print(p, "exists=", c.exists(p))
    print("top:", c.listdir("/DCIM")[:6])
