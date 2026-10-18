# iPhoBack — iPhone 相册备份（Loongnix Desktop 25.1 专版）

在龙芯 LoongArch 电脑（3A5000 / 3A6000 / 2K2000 / 2K3000）上，用数据线把
iPhone 相册备份到本机。纯 Python + PyQt5 实现，全部依赖来自 Loongnix 官方源，
**无需 pip、无需越狱、无需 iTunes**。

## 功能

| 功能 | 说明 |
|---|---|
| 相机胶卷备份 | 读取 iPhone 的 DCIM 目录，按拍摄时间归档到 `年-月` 文件夹 |
| 原格式保留 | HEIC / JPG / MOV / PNG / CAF 原样复制，不做有损转换 |
| HEIC 预览 | 自动为 HEIC 生成同名 JPG（存「预览/」目录），龙芯看图软件直接能开 |
| 实况照片 | HEIC 与配对的 MOV 成组保留，实况效果不丢 |
| 增量备份 | SQLite 索引记录已备份文件，第二次起只备份新增内容 |
| 断点续传 | 拔线 / 断电后，已传输字节落盘保留，重连后从断点继续 |
| 拍摄时间 | 优先 PLIST 元数据 → EXIF → 文件时间，跨天/跨年归档不错位 |
| 安全落盘 | 先写 `.iphoback-part` 临时文件，fsync 后原子改名，不留半个文件 |

## 安装（在 Loongnix 25.1 上）

```bash
sudo bash install.sh
```

脚本会从官方源安装：`libimobiledevice-utils`、`usbmuxd`、`ifuse`、
`python3-pyqt5`、`ffmpeg` 等，并创建开始菜单入口「iPhoBack 相册备份」。

## 使用

1. 数据线连接 iPhone，解锁屏幕
2. 手机弹出「信任此电脑？」→ 点**信任**，输入锁屏密码
3. 启动 iPhoBack（开始菜单，或终端 `iphoback`）
4. 选好备份位置 → **扫描相册** → **开始备份**

## 目录结构（备份产物）

```
图片/iPhone备份/iPhone/
├── 2025-09/           ← 按拍摄年月归档，原文件原格式
│   ├── IMG_0001.HEIC
│   ├── IMG_0001.MOV   ← 实况照片的视频部分
│   └── IMG_0002.JPG
├── 预览/              ← HEIC 的 JPG 副本（可选，日常浏览用）
│   └── 2025-09/IMG_0001.jpg
└── .iphoback-index.db ← 增量索引（别删，删了下次会全量重扫）
```

## 常见问题

- **检测不到设备**：确认 `sudo systemctl status usbmuxd` 在运行；数据线要支持数据传输（不能用纯充电线）；手机必须解锁并点过「信任」。
- **点了信任还是连不上**：拔掉数据线重插一次，或重启 usbmuxd：`sudo systemctl restart usbmuxd`。
- **想跳过 JPG 预览**：取消勾选「为 HEIC 生成 JPG 预览」即可，纯备份更快。
- **备份到移动硬盘**：直接在「备份到」里选择挂载点即可；程序会自动检查剩余空间。

## 开发者

```bash
# 生成一台“虚拟 iPhone”（无真机也可跑通全流程）
python3 tools/mkfake_iphone.py /tmp/fake_iphone --count 40

# 用虚拟设备启动 GUI（演示/测试）
IPHOBACK_FAKE_DEVICE=/tmp/fake_iphone python3 run.py

# 命令行跑引擎测试
python3 -m tools.fake_afc /tmp/fake_iphone
```

架构：`afc.py`（设备连接，ctypes 直调 libimobiledevice，CLI/ifuse 降级）
→ `metadata.py`（PLIST/EXIF/实况配对/指纹）
→ `engine.py`（扫描/计划/增量/续传/索引）
→ `preview.py`（HEIC→JPG）
→ `gui.py`（PyQt5 界面）。平台差异集中在 `platform_lnx.py`。

## 许可

MIT。libimobiledevice 为 LGPL-2.1，本项目只通过其公开 ABI/CLI 调用。
