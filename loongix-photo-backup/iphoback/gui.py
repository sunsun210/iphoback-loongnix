# -*- coding: utf-8 -*-
"""iPhoBack 主窗口（PyQt5）。

布局（从上到下）：
  1. 顶栏：应用名 + 设备状态灯
  2. 设备卡：型号 / iOS / 照片数 / 容量 / 后端
  3. 选项区：备份位置、归档方式、备份范围
  4. 进度区：当前文件、总进度条、速度、失败计数
  5. 按钮区：扫描 → 备份 → 取消 / 打开目录
  6. 日志区：时间线
"""

import os
import threading
import time

from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QFont, QPixmap, QIcon
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QGridLayout, QFileDialog, QComboBox, QLineEdit, QCheckBox,
    QProgressBar, QGroupBox, QTextEdit, QMessageBox, QFrame, QSizePolicy,
)

from . import engine as eng
from . import metadata as md
from .afc import DeviceError, device_udids, backend_report
from .platform_lnx import (
    APP_NAME, APP_VERSION, human_size, human_duration, open_path,
    notify, platform_summary, find_tool, INSTALL_CMD,
)

STATE_STYLE = {
    "ok": "color:#2e9e5b;",
    "warn": "color:#c07f00;",
    "err": "color:#c0392b;",
    "dim": "color:#7a7a7a;",
}

DEVICE_ALIASES = {
    "iPhone14,2": "iPhone 13 mini", "iPhone14,3": "iPhone 13 Pro",
    "iPhone14,4": "iPhone 13 mini", "iPhone14,5": "iPhone 13",
    "iPhone14,6": "iPhone SE (3rd)", "iPhone14,7": "iPhone 14",
    "iPhone14,8": "iPhone 14 Plus", "iPhone15,2": "iPhone 14 Pro",
    "iPhone15,3": "iPhone 14 Pro Max", "iPhone15,4": "iPhone 15",
    "iPhone15,5": "iPhone 15 Plus", "iPhone16,1": "iPhone 15 Pro",
    "iPhone16,2": "iPhone 15 Pro Max", "iPhone17,3": "iPhone 16",
    "iPhone17,4": "iPhone 16 Plus", "iPhone17,5": "iPhone 16 Pro",
    "iPhone17,2": "iPhone 16 Pro Max",
}


class Worker(QThread):
    """扫描 / 备份都放这里，UI 线程只收信号。"""

    event = pyqtSignal(dict)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs

    def run(self):
        try:
            self.fn(self.event.emit, *self.args, **self.kwargs)
        except Exception as exc:
            self.event.emit({"kind": "fatal", "error": str(exc)})


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.client = None
        self.scan = None
        self.plan = None
        self.worker = None
        self.cancel_flag = threading.Event()
        self._t0 = 0
        self._init_ui()
        self._load_backends()
        # 插上设备时 usbmuxd 会通知，但简单起见用定时轮询（2 秒一次足够）
        self.device_timer = QTimer(self)
        self.device_timer.timeout.connect(self._poll_device)
        self.device_timer.start(2000)
        QTimer.singleShot(300, self._poll_device)

    # ------------------------------------------------------------------ UI
    def _init_ui(self):
        self.setWindowTitle("%s — iPhone 相册备份（Loongnix 版）" % APP_NAME)
        self.resize(860, 640)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(10)

        # -- 顶栏
        top = QHBoxLayout()
        title = QLabel(APP_NAME)
        f = QFont(); f.setPointSize(15); f.setBold(True)
        title.setFont(f)
        sub = QLabel("v%s · 适配 Loongnix Desktop 25.1（LoongArch）" % APP_VERSION)
        sub.setStyleSheet(STATE_STYLE["dim"])
        self.dot = QLabel("●")
        self.dot.setStyleSheet(STATE_STYLE["dim"])
        self.dev_state = QLabel("正在检测设备…")
        top.addWidget(title)
        top.addSpacing(6)
        top.addWidget(sub)
        top.addStretch()
        top.addWidget(self.dot)
        top.addWidget(self.dev_state)
        root.addLayout(top)

        line = QFrame(); line.setFrameShape(QFrame.HLine); line.setFrameShadow(QFrame.Sunken)
        root.addWidget(line)

        # -- 设备信息卡
        card = QGroupBox("设备")
        grid = QGridLayout(card)
        grid.setVerticalSpacing(4)
        self.lbl_name = self._val_label()
        self.lbl_ios = self._val_label()
        self.lbl_photos = self._val_label()
        self.lbl_backend = self._val_label()
        for col, (cap, w) in enumerate([
                ("名称", self.lbl_name), ("系统", self.lbl_ios),
                ("照片", self.lbl_photos), ("连接方式", self.lbl_backend)]):
            cap_lbl = QLabel(cap)
            cap_lbl.setStyleSheet(STATE_STYLE["dim"])
            grid.addWidget(cap_lbl, 0, col)
            grid.addWidget(w, 1, col)
        root.addWidget(card)

        # -- 选项
        opt = QGroupBox("备份设置")
        form = QGridLayout(opt)
        form.addWidget(QLabel("备份到"), 0, 0)
        self.ed_dest = QLineEdit()
        btn_pick = QPushButton("选择…")
        btn_pick.clicked.connect(self._pick_dest)
        form.addWidget(self.ed_dest, 0, 1)
        form.addWidget(btn_pick, 0, 2)

        form.addWidget(QLabel("按时间归档"), 1, 0)
        self.cb_layout = QComboBox()
        self.cb_layout.addItem("按月（2025-09）", "month")
        self.cb_layout.addItem("按年（2025）", "year")
        self.cb_layout.addItem("按日（2025-09-08）", "day")
        form.addWidget(self.cb_layout, 1, 1)

        self.ck_video = QCheckBox("包含视频（含实况照片的动态部分）")
        self.ck_video.setChecked(True)
        self.ck_preview = QCheckBox("为 HEIC 生成 JPG 预览（需要 ffmpeg，原图保持原样）")
        self.ck_preview.setChecked(True)
        self.ck_verify = QCheckBox("传输后校验哈希（更慢更保险）")
        form.addWidget(self.ck_video, 2, 1)
        form.addWidget(self.ck_preview, 3, 1)
        form.addWidget(self.ck_verify, 4, 1)
        root.addWidget(opt)

        # -- 进度
        prog = QGroupBox("进度")
        pv = QVBoxLayout(prog)
        self.lbl_current = QLabel("—")
        self.bar = QProgressBar(); self.bar.setRange(0, 100)
        row = QHBoxLayout()
        self.lbl_phase = QLabel("空闲")
        self.lbl_speed = QLabel("")
        self.lbl_stat = QLabel("")
        row.addWidget(self.lbl_phase)
        row.addStretch()
        row.addWidget(self.lbl_speed)
        row.addSpacing(16)
        row.addWidget(self.lbl_stat)
        pv.addWidget(self.lbl_current)
        pv.addWidget(self.bar)
        pv.addLayout(row)
        root.addWidget(prog)

        # -- 日志
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        f2 = QFont("monospace"); f2.setStyleHint(QFont.TypeWriter); f2.setPointSize(9)
        self.log.setFont(f2)
        root.addWidget(self.log, 1)

        # -- 按钮
        btns = QHBoxLayout()
        self.btn_refresh = QPushButton("刷新设备")
        self.btn_refresh.clicked.connect(self._poll_device_force)
        self.btn_scan = QPushButton("扫描相册")
        self.btn_scan.clicked.connect(self._start_scan)
        self.btn_go = QPushButton("开始备份")
        self.btn_go.clicked.connect(self._start_backup)
        self.btn_stop = QPushButton("取消")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._cancel)
        self.btn_open = QPushButton("打开备份目录")
        self.btn_open.clicked.connect(self._open_dest)
        btns.addWidget(self.btn_refresh)
        btns.addWidget(self.btn_scan)
        btns.addWidget(self.btn_go)
        btns.addWidget(self.btn_stop)
        btns.addStretch()
        btns.addWidget(self.btn_open)
        root.addLayout(btns)

        self.statusBar().showMessage("就绪")

    @staticmethod
    def _val_label():
        lbl = QLabel("—")
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        return lbl

    def _log(self, msg, style=""):
        ts = time.strftime("%H:%M:%S")
        if style:
            self.log.append('<span style="%s">[%s] %s</span>' % (style, ts, msg))
        else:
            self.log.append("[%s] %s" % (ts, msg))

    # ------------------------------------------------------------- 后端状态
    def _load_backends(self):
        rep = backend_report()
        parts = []
        if rep["ctypes"]:
            parts.append("libimobiledevice 直连")
        elif rep["ideviceinfo"]:
            parts.append("命令行工具")
        else:
            parts.append("未找到 libimobiledevice")
        if rep["ifuse"]:
            parts.append("ifuse 可用")
        self.lbl_backend.setText(" · ".join(parts))
        if not (rep["ctypes"] or rep["ideviceinfo"]):
            self._log("缺少 libimobiledevice：%s" % INSTALL_CMD, STATE_STYLE["warn"])

    # ------------------------------------------------------------- 设备检测
    def _poll_device_force(self):
        self._log("手动刷新设备…")
        self._poll_device()

    def _poll_device(self):
        if self.worker:                      # 正在干活，别打扰
            return
        try:
            udids = device_udids()
        except Exception:
            udids = []
        if not udids:
            if getattr(self, "_had_device", False):
                self._log("设备已断开。", STATE_STYLE["warn"])
            self._had_device = False
            self.dot.setStyleSheet(STATE_STYLE["dim"])
            self.dev_state.setText("未检测到 iPhone")
            if self.client:
                try:
                    self.client.close()
                except Exception:
                    pass
                self.client = None
            self.btn_scan.setEnabled(False)
            return
        self._had_device = True
        if self.client:
            return                            # 已连接
        try:
            self.client = eng.open_device(udids[0]) if hasattr(eng, "open_device") else None
        except Exception:
            self.client = None
        if self.client is None:
            from .afc import open_device
            try:
                self.client = open_device(udids[0])
                if hasattr(self.client, "connect"):
                    self.client.connect()
            except DeviceError as exc:
                self.client = None
                self.dot.setStyleSheet(STATE_STYLE["err"])
                self.dev_state.setText(str(exc))
                if exc.hint:
                    self._log(exc.hint, STATE_STYLE["warn"])
                return
            except Exception as exc:
                self.client = None
                self.dot.setStyleSheet(STATE_STYLE["err"])
                self.dev_state.setText("连接失败：%s" % exc)
                return
        info = self.client.info() if hasattr(self.client, "info") else {}
        name = info.get("device_name") or DEVICE_ALIASES.get(info.get("product_type"), "iPhone")
        ios = info.get("ios_version") or "?"
        self.dot.setStyleSheet(STATE_STYLE["ok"])
        self.dev_state.setText("已连接")
        self.lbl_name.setText(name)
        self.lbl_ios.setText("iOS " + ios)
        self.btn_scan.setEnabled(True)
        self._log("已连接：%s（iOS %s，UDID %s…）" % (name, ios, udids[0][:8]), STATE_STYLE["ok"])
        if not self.ed_dest.text():
            self.ed_dest.setText(eng.default_dest(name))
        self.btn_open.setEnabled(True)

    # ------------------------------------------------------------- 扫描
    def _start_scan(self):
        if not self.client:
            QMessageBox.warning(self, APP_NAME, "请先连接 iPhone（数据线 + 解锁 + 点信任）。")
            return
        self._busy(True, "扫描中…")
        self._log("开始扫描设备相册…")
        mode = "camera"
        dest = self._dest()
        self.cancel_flag.clear()
        self.worker = Worker(self._do_scan, self.client, dest, mode)
        self.worker.event.connect(self._on_event)
        self.worker.start()

    def _do_scan(self, emit, client, dest, mode):
        index = eng.IndexDb(dest)
        try:
            scanner = eng.Scanner(client, progress=eng.Progress(emit), mode=mode)
            res = scanner.scan(cancel=self.cancel_flag)
            emit({"kind": "scan_done", "files": len(res.items),
                  "bytes": res.total_bytes, "live": res.live_pairs,
                  "dupes": res.duplicates, "warnings": res.warnings[:5]})
        finally:
            index.close()

    def _on_scan_done(self, ev):
        self.scan = getattr(self, "_pending_scan", None)
        self.lbl_photos.setText("%d 个文件（%s）" % (ev["files"], human_size(ev["bytes"])))
        note = []
        if ev.get("live"):
            note.append("实况 %d 组" % ev["live"])
        if ev.get("dupes"):
            note.append("重复 %d" % ev["dupes"])
        self._log("扫描完成：%d 个文件，共 %s%s"
                  % (ev["files"], human_size(ev["bytes"]),
                     "，" + "，".join(note) if note else ""), STATE_STYLE["ok"])
        for w in ev.get("warnings", []):
            self._log("警告：%s" % w, STATE_STYLE["warn"])
        if ev["files"]:
            self.btn_go.setEnabled(True)

    # ------------------------------------------------------------- 备份
    def _start_backup(self):
        dest = self._dest()
        if not dest:
            QMessageBox.warning(self, APP_NAME, "请选择备份目录。")
            return
        self._busy(True, "准备备份…")
        self._log("目标目录：%s" % dest)
        self.cancel_flag.clear()
        self._t0 = time.time()
        self.worker = Worker(self._do_backup, self.client, dest)
        self.worker.event.connect(self._on_event)
        self.worker.start()

    def _do_backup(self, emit, client, dest):
        udid = getattr(client, "udid", "unknown")
        index = eng.IndexDb(dest)
        try:
            scanner = eng.Scanner(client, progress=eng.Progress(emit))
            res = scanner.scan(cancel=self.cancel_flag)
            engine = eng.BackupEngine(client, dest, progress=eng.Progress(emit),
                                      cancel=self.cancel_flag)
            engine.index = index
            plan = eng.build_plan(
                res, dest, index, udid,
                layout=self._layout(),
                include_videos=self.ck_video.isChecked(),
                include_live=self.ck_video.isChecked())
            todo = [p for p in plan if p.action != "skip"]
            emit({"kind": "plan", "copy": len(todo),
                  "skip": len(plan) - len(todo),
                  "bytes": sum(p.asset.size - p.resume_from for p in todo)})
            ok, msg = eng.preflight(dest, res.total_bytes)
            if not ok:
                emit({"kind": "fatal", "error": msg})
                return
            engine.run(plan, udid=udid, verify_hash=self.ck_verify.isChecked())
            engine.index = None      # 避免双关
            if self.ck_preview.isChecked():
                emit({"kind": "phase", "text": "生成 JPG 预览…"})
                engine2 = eng.BackupEngine(client, dest, progress=eng.Progress(emit),
                                           cancel=self.cancel_flag)
                engine2.index = index
                engine2.make_previews(udid=udid, layout=self._layout())
        finally:
            index.close()

    def _on_event(self, ev):
        kind = ev.get("kind")
        if kind == "scan_dir":
            self.lbl_phase.setText("扫描 %s" % ev.get("path", ""))
        elif kind == "scan_meta":
            self.lbl_phase.setText("读取拍摄时间 %d/%d" % (ev.get("done", 0), ev.get("total", 0)))
        elif kind == "scan_refine":
            self.lbl_phase.setText("校准可疑时间戳…")
        elif kind == "scan_done":
            self._pending_scan = True
            self._on_scan_done(ev)
        elif kind == "plan":
            self.bar.setRange(0, 100)
            self.lbl_stat.setText("%d 个待备份 / 跳过 %d / %s"
                                  % (ev["copy"], ev["skip"], human_size(ev["bytes"])))
            self._log("待备份 %d 个（%s），跳过已备份 %d 个"
                      % (ev["copy"], human_size(ev["bytes"]), ev["skip"]))
        elif kind == "progress":
            size = ev.get("size") or 1
            pct = int(ev.get("done", 0) * 100 / size)
            self.bar.setValue(pct)
            self.lbl_current.setText("%s  %s / %s"
                                     % (ev.get("name", ""), human_size(ev.get("done", 0)),
                                        human_size(size)))
        elif kind == "file_done":
            total = ev.get("total_bytes") or 1
            pct = int(ev.get("bytes", 0) * 100 / total)
            self.bar.setValue(pct)
            speed = ev.get("bytes", 0) / max(0.001, time.time() - self._t0)
            self.lbl_speed.setText(human_size(speed) + "/s")
            self.lbl_stat.setText("%d / %d 个文件" % (ev.get("files", 0), ev.get("total_files", 0)))
            self.lbl_current.setText(ev.get("path", ""))
        elif kind == "file_failed":
            self._log("失败：%s（%s）" % (ev.get("name"), ev.get("error")), STATE_STYLE["err"])
        elif kind == "phase":
            self.lbl_phase.setText(ev.get("text", ""))
        elif kind == "preview_progress":
            self.lbl_phase.setText("生成预览 %d/%d" % (ev.get("done", 0), ev.get("total", 0)))
        elif kind == "preview_skip":
            self._log("提示：%s" % ev.get("reason", ""), STATE_STYLE["warn"])
        elif kind == "preview_done":
            self._log("JPG 预览：已生成 %d 张（存于「预览/」子目录），失败 %d"
                      % (ev.get("made", 0), ev.get("failed", 0)))
        elif kind == "aborted":
            self._log("连续失败，已中止（设备可能已断开）：最后错误 %s"
                      % ev.get("reason", ""), STATE_STYLE["err"])
        elif kind == "done":
            self._on_done(ev)
        elif kind == "cancelled":
            self._log("已取消（保留已完成部分）", STATE_STYLE["warn"])
            self._busy(False)
        elif kind == "fatal":
            self._log("错误：%s" % ev.get("error"), STATE_STYLE["err"])
            QMessageBox.critical(self, APP_NAME, str(ev.get("error")))
            self._busy(False)
        elif kind == "resume_file":
            self._log("断点续传：%s（从 %s 处继续）" % (ev.get("name"), human_size(ev.get("offset", 0))),
                      STATE_STYLE["warn"])

    def _on_done(self, st):
        self.bar.setValue(100 if not st.get("aborted") else self.bar.value())
        self.lbl_phase.setText("完成")
        mins = human_duration(st.get("elapsed", 0))
        speed = human_size(st.get("speed", 0)) + "/s"
        text = "新增 %d 个（%s），跳过 %d，失败 %d，用时 %s（%s）" % (
            st.get("files", 0), human_size(st.get("bytes", 0)),
            st.get("skipped", 0), st.get("failed", 0), mins, speed)
        self._log(text, STATE_STYLE["ok" if not st.get("failed") else "warn"])
        self.statusBar().showMessage(text)
        notify(APP_NAME, "备份完成", text)
        self._busy(False)
        if st.get("failed") or st.get("aborted"):
            QMessageBox.warning(self, APP_NAME,
                                "备份完成（有异常，请查看日志）\n\n" + text)
        return text

    # ------------------------------------------------------------- 杂项
    def _dest(self):
        return self.ed_dest.text().strip() or None

    def _layout(self):
        return self.cb_layout.currentData() or "month"

    def _pick_dest(self):
        d = QFileDialog.getExistingDirectory(self, "选择备份目录", self.ed_dest.text() or os.path.expanduser("~"))
        if d:
            self.ed_dest.setText(d)

    def _open_dest(self):
        d = self._dest()
        if d and not open_path(d):
            QMessageBox.information(self, APP_NAME, "无法打开文件管理器，目录在：\n%s" % d)

    def _cancel(self):
        self.cancel_flag.set()
        self._log("正在取消…（等待当前文件收尾）", STATE_STYLE["warn"])

    def _busy(self, flag, phase=""):
        self.btn_scan.setEnabled(not flag and bool(self.client))
        self.btn_go.setEnabled(not flag and bool(getattr(self, "_pending_scan", False)))
        self.btn_stop.setEnabled(flag)
        if flag:
            self.lbl_phase.setText(phase or "工作中…")
        else:
            self.lbl_phase.setText("空闲")

    def closeEvent(self, ev):
        self.device_timer.stop()
        self.cancel_flag.set()
        if self.worker and self.worker.isRunning():
            self.worker.wait(3000)
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
        ev.accept()


def main():
    import sys
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("Loongnix")
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
