# iphoback-loongnix
一个适配 Loongnix Desktop 25.1（龙芯 3A5000/3A6000/2K2000/2K3000，LoongArch 架构）的 iPhone 相册备份软件
基于Loongnix 25.1 操作系统，用WorkBuddy生成（基于 Debian 13 / KDE Plasma 6 / Python 3.13），所有依赖在 loong64 源里都有原生包，因此软件为纯 Python + PyQt5 实现，零编译、零 pip（规避系统 PEP 668 限制）

设备连接：ctypes 直接调用源内 libimobiledevice-1.0-6 C 库（不依赖 Python 绑定包）；失败自动降级到 idevice* 命令行 + ifuse 挂载
依赖：libimobiledevice-utils、usbmuxd、python3-pyqt5、ffmpeg，一条 sudo bash install.sh 全部装好，并创建 KDE 开始菜单入口


功能：
按拍摄时间归档（年/月/日可选），原格式保留（HEIC/MOV/JPG 不做有损转换）
自动为 HEIC 生成 JPG 预览副本（存「预览/」目录，用 ffmpeg/libheif，龙芯看图软件能直接打开）
实况照片成对保留（HEIC + 配对 MOV）
增量备份+ 断点续传（拔线后已传字节落盘，重连从断点继续）


bash

unzip iphoback-loongnix.zip && cd loongix-photo-backup

sudo  ./install.sh    # 开始菜单出现「iPhoBack 相册备份」





首次使用步骤：
  1. 用数据线连接 iPhone，解锁屏幕
  2. 手机弹出「信任此电脑？」→ 点信任并输入锁屏密码
  3. iPhoBack 里点「扫描相册」→「开始备份」
