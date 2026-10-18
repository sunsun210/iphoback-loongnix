# -*- coding: utf-8 -*-
"""iPhone 文件系统访问层（AFC）。

三级后端，自动降级：
  1. ctypes 直接调用系统 libimobiledevice C 库（Loongnix 源内 libimobiledevice-1.0-6）
     —— 最快，无需额外安装 Python 绑定，也不受 PEP 668 限制。
  2. libimobiledevice 命令行工具（idevice_id / ideviceinfo / idevicepair）。
  3. ifuse 挂载后用普通文件系统接口读取（需要 FUSE 权限）。

对外统一暴露 IAfc 接口，UI 与引擎只依赖这个接口。
"""

import ctypes
import ctypes.util
import os
import plistlib
import shutil
import subprocess
import tempfile
import time

from .platform_lnx import find_tool, run_tool


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------
class DeviceError(Exception):
    """设备层错误，message 已转成中文可读文案。"""

    def __init__(self, message, hint=""):
        super().__init__(message)
        self.hint = hint


# --------------------------------------------------------------------------
# 接口
# --------------------------------------------------------------------------
class IAfc:
    #: 是否支持 read_file(path, offset=N)，用于断点续传时跳过已下载字节
    supports_offset = False

    def exists(self, path):
        raise NotImplementedError

    def is_dir(self, path):
        raise NotImplementedError

    def stat(self, path):
        """返回 {'size': int, 'mtime': int}"""
        raise NotImplementedError

    def listdir(self, path):
        raise NotImplementedError

    def read_file(self, path):
        """生成器，逐块产出 bytes。"""
        raise NotImplementedError

    def close(self):
        pass


# --------------------------------------------------------------------------
# 后端 1：ctypes 绑定
# --------------------------------------------------------------------------
class _AfcStatStruct(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint64),
        ("mtime", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
    ]


def _load_libimobile():
    candidates = ["libimobiledevice-1.0.so.6", "libimobiledevice-1.0.so.0"]
    found = ctypes.util.find_library("imobiledevice-1.0")
    if found:
        candidates.insert(0, found)
    for name in candidates:
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    return None


def ctypes_available():
    return _load_libimobile() is not None


class CtypesAfc(IAfc):
    """通过 libimobiledevice C 库直接读写设备文件系统。"""

    supports_offset = True

    def __init__(self, udid=None, label="iPhoBack"):
        lib = _load_libimobile()
        if lib is None:
            raise DeviceError("未找到 libimobiledevice 运行库",
                              "请执行：sudo apt install libimobiledevice-1.0-6")
        self.lib = lib
        self._setup_proto()
        self.udid = udid or self._first_device()
        if not self.udid:
            raise DeviceError("没有检测到已连接的 iPhone",
                              "请用数据线连接设备、解锁屏幕，并在手机上点「信任」。")
        self.label = label
        self.afc = ctypes.c_void_p()
        self.lockdownd = ctypes.c_void_p()

    # -- 函数签名 ------------------------------------------------------
    def _setup_proto(self):
        L = self.lib
        L.idevice_new.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_char_p]
        L.idevice_new.restype = ctypes.c_int
        L.idevice_free.argtypes = [ctypes.c_void_p]
        L.idevice_get_udid.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_char_p)]
        L.idevice_get_udid.restype = ctypes.c_int
        L.idevice_free_string = getattr(L, "idevice_free_string", None)
        if L.idevice_free_string is not None:
            L.idevice_free_string.argtypes = [ctypes.c_char_p]

        L.lockdownd_client_new_with_handshake.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_char_p]
        L.lockdownd_client_new_with_handshake.restype = ctypes.c_int
        L.lockdownd_client_free.argtypes = [ctypes.c_void_p]
        L.lockdownd_get_value.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
        L.lockdownd_get_value.restype = ctypes.c_int
        L.plist_to_xml.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_uint32)]
        L.plist_to_xml.restype = ctypes.c_int
        L.plist_free.argtypes = [ctypes.c_void_p]
        L.plist_free.restype = ctypes.c_void_p
        L.free.argtypes = [ctypes.c_void_p]

        L.afc_client_new.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        L.afc_client_new.restype = ctypes.c_int
        L.afc_client_free.argtypes = [ctypes.c_void_p]
        L.afc_read_directory.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.POINTER(ctypes.c_char_p))]
        L.afc_read_directory.restype = ctypes.c_int
        L.afc_get_file_info.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.POINTER(ctypes.c_char_p))]
        L.afc_get_file_info.restype = ctypes.c_int
        L.afc_file_open.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(ctypes.c_uint64)]
        L.afc_file_open.restype = ctypes.c_int
        L.afc_file_read.argtypes = [
            ctypes.c_void_p, ctypes.c_uint64, ctypes.c_char_p, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32)]
        L.afc_file_read.restype = ctypes.c_int
        L.afc_file_seek.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_int64,
                                    ctypes.c_int]
        L.afc_file_seek.restype = ctypes.c_int
        L.afc_file_close.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        L.afc_file_close.restype = ctypes.c_int

    def _first_device(self):
        out = ctypes.c_char_p()
        L = self.lib
        # idevice_get_device_list 在部分版本里是宏，优先用 idevice_new + UDID 探测
        try:
            L.idevice_get_device_list.argtypes = [
                ctypes.POINTER(ctypes.POINTER(ctypes.c_char_p)), ctypes.POINTER(ctypes.c_int)]
            L.idevice_get_device_list.restype = ctypes.c_int
            lst = ctypes.POINTER(ctypes.c_char_p)()
            count = ctypes.c_int()
            if L.idevice_get_device_list(ctypes.byref(lst), ctypes.byref(count)) == 0:
                if count.value > 0:
                    udid = lst[0].decode() if lst[0] else None
                    try:
                        L.idevice_device_list_free.argtypes = [ctypes.POINTER(ctypes.c_char_p)]
                        L.idevice_device_list_free(lst)
                    except AttributeError:
                        pass
                    if udid:
                        return udid
                else:
                    try:
                        L.idevice_device_list_free.argtypes = [ctypes.POINTER(ctypes.c_char_p)]
                        L.idevice_device_list_free(lst)
                    except AttributeError:
                        pass
                return None
        except Exception:
            pass
        return None

    def _plist_value(self, domain, key):
        """读 Lockdown 里的值，返回 Python 对象。"""
        val = ctypes.c_void_p()
        rc = self.lib.lockdownd_get_value(self.lockdownd, domain, key, ctypes.byref(val))
        if rc != 0 or not val:
            return None
        try:
            xml = ctypes.c_char_p()
            length = ctypes.c_uint32()
            if self.lib.plist_to_xml(val, ctypes.byref(xml), ctypes.byref(length)) != 0:
                return None
            raw = ctypes.string_at(xml, length.value)
            return plistlib.loads(raw)
        except Exception:
            return None
        finally:
            self.lib.plist_free(val)

    def connect(self):
        """建立 lockdown + AFC 会话。"""
        dev = ctypes.c_void_p()
        if self.lib.idevice_new(ctypes.byref(dev), self.udid.encode()) != 0:
            raise DeviceError("无法打开设备 %s" % self.udid[:8],
                              "请确认设备已解锁、数据线连接正常，并已点「信任此电脑」。")
        self.device = dev
        rc = self.lib.lockdownd_client_new_with_handshake(
            dev, ctypes.byref(self.lockdownd), self.label.encode())
        if rc != 0:
            raise DeviceError("与设备握手失败（错误码 %d）" % rc,
                              "请在 iPhone 屏幕上点「信任」，然后重新点击「刷新设备」。")
        if self.lib.afc_client_new(dev, self.lockdownd, ctypes.byref(self.afc)) != 0:
            raise DeviceError("无法打开设备文件服务（AFC）",
                              "这是首次连接的常见问题，请拔插数据线后重试。")
        return self

    def info(self):
        """返回设备信息 dict（中文字段名由上层映射）。"""
        keys = {
            "DeviceName": "device_name",
            "ProductType": "product_type",
            "ProductVersion": "ios_version",
            "BuildVersion": "build",
            "SerialNumber": "serial",
            "PhoneNumber": "phone",
            "UniqueDeviceID": "udid",
            "HardwareModel": "hardware",
        }
        out = {"udid": self.udid}
        for k, name in keys.items():
            v = self._plist_value(None, k)
            if v is not None:
                out[name] = v
        return out

    # -- AFC 文件接口 --------------------------------------------------
    def _info_struct(self, path):
        info = ctypes.POINTER(ctypes.c_char_p)()
        if self.lib.afc_get_file_info(self.afc, path.encode("utf-8"), ctypes.byref(info)) != 0:
            return None
        if not info:
            return None
        result = {}
        i = 0
        while info[i] is not None:
            key = info[i].decode("utf-8", "replace")
            val = info[i + 1]
            result[key] = val.decode("utf-8", "replace") if val else ""
            i += 2
        # afc_get_file_info 返回的 char** 由调用方 free()，条目是库内字符串
        try:
            self.lib.free(ctypes.cast(info, ctypes.c_void_p))
        except Exception:
            pass
        return result

    def exists(self, path):
        return self._info_struct(path) is not None

    def is_dir(self, path):
        info = self._info_struct(path)
        return bool(info) and info.get("st_ifmt") == "S_IFDIR"

    def stat(self, path):
        info = self._info_struct(path)
        if not info:
            raise DeviceError("设备上找不到 %s" % path)
        size = int(info.get("st_size") or 0)
        mtime = 0
        raw = info.get("st_mtime", "")
        if raw and raw.isdigit():
            mtime = int(raw)
            if mtime > 10 ** 11:          # 纳秒
                mtime //= 10 ** 9
        return {"size": size, "mtime": mtime, "mode": info.get("st_ifmt", "")}

    def listdir(self, path):
        out = ctypes.POINTER(ctypes.c_char_p)()
        if self.lib.afc_read_directory(self.afc, path.encode("utf-8"), ctypes.byref(out)) != 0:
            raise DeviceError("无法读取设备目录 %s" % path,
                              "如果刚插上设备，请在手机上点「信任」后重试。")
        names = []
        i = 0
        while out[i] is not None:
            name = out[i].decode("utf-8", "replace")
            if name not in (".", ".."):
                names.append(name)
            i += 1
        return names

    def read_file(self, path, offset=0):
        """逐块读取；offset>0 时用于断点续传。"""
        handle = ctypes.c_uint64()
        if self.lib.afc_file_open(self.afc, path.encode("utf-8"), 1, ctypes.byref(handle)) != 0:
            raise DeviceError("无法打开设备文件 %s" % path)
        buf_size = 256 * 1024
        buf = ctypes.create_string_buffer(buf_size)
        got = ctypes.c_uint32()
        try:
            if offset:
                try:
                    if self.lib.afc_file_seek(handle, offset, 0, 0) != 0:
                        raise DeviceError("seek 失败")
                except AttributeError:
                    raise DeviceError("当前 libimobiledevice 版本不支持断点续传")
            while True:
                rc = self.lib.afc_file_read(handle, buf, buf_size, ctypes.byref(got))
                if rc != 0:
                    raise DeviceError("读取中断：%s" % path)
                if got.value == 0:
                    break
                yield buf.raw[:got.value]
        finally:
            self.lib.afc_file_close(handle)

    def close(self):
        if getattr(self, "afc", None):
            self.lib.afc_client_free(self.afc)
            self.afc = None
        if getattr(self, "lockdownd", None):
            self.lib.lockdownd_client_free(self.lockdownd)
            self.lockdownd = None
        if getattr(self, "device", None):
            self.lib.idevice_free(self.device)
            self.device = None


# --------------------------------------------------------------------------
# 后端 2：命令行工具 + ifuse 挂载
# --------------------------------------------------------------------------
class CliAfc(IAfc):
    """用 idevice* 工具链；文件读取依赖 ifuse 挂载点。"""

    supports_offset = True

    def __init__(self, udid=None, mountpoint=None):
        self.udid = udid or self._detect_udid()
        self.mountpoint = mountpoint
        self._mounted_here = False

    @staticmethod
    def _detect_udid():
        if not find_tool("idevice_id"):
            raise DeviceError("未安装 libimobiledevice 工具",
                              "请执行：sudo apt install libimobiledevice-utils usbmuxd")
        code, out, err = run_tool(["idevice_id", "-l"])
        if code != 0:
            raise DeviceError("无法列出设备：%s" % (err or "未知错误"),
                              "请确认 usbmuxd 服务已启动：sudo systemctl start usbmuxd")
        udids = [l.strip() for l in out.splitlines() if l.strip()]
        if not udids:
            raise DeviceError("没有检测到已连接的 iPhone",
                              "请用数据线连接、解锁屏幕，并在手机上点「信任」。")
        return udids[0]

    def info(self):
        out = {"udid": self.udid}
        code, text, _ = run_tool(["ideviceinfo", "-u", self.udid])
        if code != 0:
            return out
        mapping = {"DeviceName": "device_name", "ProductType": "product_type",
                   "ProductVersion": "ios_version", "BuildVersion": "build",
                   "SerialNumber": "serial", "PhoneNumber": "phone"}
        for line in text.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                key = mapping.get(k.strip())
                if key:
                    out[key] = v.strip()
        return out

    def mount(self):
        """用 ifuse 挂载设备，返回挂载点。"""
        if not find_tool("ifuse"):
            raise DeviceError("未安装 ifuse", "请执行：sudo apt install ifuse")
        if self.mountpoint and os.path.ismount(self.mountpoint):
            return self.mountpoint
        mnt = tempfile.mkdtemp(prefix="iphoback-")
        code, _, err = run_tool(["ifuse", "--udid", self.udid, mnt], timeout=60)
        if code != 0:
            shutil.rmtree(mnt, ignore_errors=True)
            raise DeviceError("挂载设备失败：%s" % (err or "未知错误"),
                              "请确认当前用户在 fuse 组：sudo usermod -aG fuse $USER，然后重新登录。")
        self.mountpoint = mnt
        self._mounted_here = True
        return mnt

    def _p(self, path):
        if not self.mountpoint:
            self.mount()
        rel = path.strip("/").replace("/", os.sep)
        return os.path.join(self.mountpoint, rel) if rel else self.mountpoint

    def exists(self, path):
        return os.path.exists(self._p(path))

    def is_dir(self, path):
        return os.path.isdir(self._p(path))

    def stat(self, path):
        st = os.stat(self._p(path))
        return {"size": st.st_size, "mtime": int(st.st_mtime)}

    def listdir(self, path):
        try:
            return sorted(os.listdir(self._p(path)))
        except OSError as exc:
            raise DeviceError("无法读取设备目录 %s：%s" % (path, exc))

    def read_file(self, path, offset=0):
        fp = self._p(path)
        with open(fp, "rb") as fh:
            if offset:
                fh.seek(offset)
            while True:
                chunk = fh.read(256 * 1024)
                if not chunk:
                    break
                yield chunk

    def close(self):
        if self._mounted_here and self.mountpoint:
            run_tool(["fusermount", "-u", self.mountpoint], timeout=15)
            shutil.rmtree(self.mountpoint, ignore_errors=True)
            self.mountpoint = None
            self._mounted_here = False


# --------------------------------------------------------------------------
# 工厂
# --------------------------------------------------------------------------
def device_udids():
    """尽可能拿到已连接设备的 UDID 列表。"""
    if os.environ.get("IPHOBACK_FAKE_DEVICE"):
        return ["FAKE-UDID-0001"]
    udids = []
    if find_tool("idevice_id"):
        code, out, _ = run_tool(["idevice_id", "-l"], timeout=10)
        if code == 0:
            udids = [l.strip() for l in out.splitlines() if l.strip()]
    if not udids and ctypes_available():
        try:
            probe = CtypesAfc()
            if probe.udid:
                udids = [probe.udid]
        except DeviceError:
            pass
    return udids


def open_device(udid=None, prefer="auto"):
    """打开设备，自动在 ctypes / CLI 后端之间降级。"""
    # 测试/演示钩子：IPHOBACK_FAKE_DEVICE=虚拟设备目录
    fake = os.environ.get("IPHOBACK_FAKE_DEVICE")
    if fake:
        try:
            from tools.fake_afc import FakeAfcClient
            return FakeAfcClient(fake)
        except Exception:
            pass
    errors = []
    if prefer in ("auto", "ctypes") and ctypes_available():
        try:
            client = CtypesAfc(udid)
            client.connect()
            return client
        except DeviceError as exc:
            errors.append(str(exc))
        except Exception as exc:      # 库版本差异导致的结构体问题
            errors.append("C 库后端不可用：%s" % exc)
    if prefer in ("auto", "cli"):
        try:
            return CliAfc(udid)
        except DeviceError as exc:
            errors.append(str(exc))
    raise DeviceError(errors[-1] if errors else "无法连接设备",
                      "请确认：① 已安装 libimobiledevice-utils；② usbmuxd 正在运行；"
                      "③ 手机已解锁并点了「信任」。")


def backend_report():
    """给「设备与依赖」面板用。"""
    return {
        "ctypes": ctypes_available(),
        "idevice_id": bool(find_tool("idevice_id")),
        "ideviceinfo": bool(find_tool("ideviceinfo")),
        "idevicepair": bool(find_tool("idevicepair")),
        "ifuse": bool(find_tool("ifuse")),
        "usbmuxd": bool(find_tool("usbmuxd")),
    }
