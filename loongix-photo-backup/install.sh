#!/bin/bash
# iPhoBack 安装脚本 —— Loongnix Desktop 25.1 (LoongArch)
# 用法：sudo bash install.sh        安装依赖 + 桌面入口
#       bash install.sh --local     只创建桌面入口（跳过 apt）

set -e

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${PREFIX:-/opt/iphoback}"
BIN="/usr/local/bin/iphoback"
DESKTOP="/usr/share/applications/iphoback.desktop"

if [ "$(uname -m)" != "loongarch64" ]; then
    echo "提示：当前架构为 $(uname -m)，本程序为纯 Python 实现，理论上也能运行，"
    echo "      但依赖包名称按 Loongnix 25.1（loongarch64）编写，请自行确认。"
fi

# 1. 系统依赖（均来自 Loongnix 官方源，无需 pip）
if [ "$1" != "--local" ]; then
    echo "==> 安装系统依赖（libimobiledevice / PyQt5 / ffmpeg）…"
    apt-get update -qq || true
    apt-get install -y --no-install-recommends \
        libimobiledevice-utils usbmuxd ifuse \
        python3-pyqt5 python3-pyqt5.qtsvg \
        python3-pil ffmpeg gvfs-backends
fi

# 2. 程序文件
echo "==> 安装程序到 $PREFIX …"
mkdir -p "$PREFIX"
cp -r "$APP_DIR/iphoback" "$APP_DIR/run.py" "$PREFIX/"
chmod +x "$PREFIX/run.py"

# 3. 命令行入口
cat > "$BIN" <<EOF
#!/bin/bash
# iPhoBack 启动器
export PATH="/usr/sbin:\$PATH"
cd "$PREFIX"
exec python3 run.py "\$@"
EOF
chmod +x "$BIN"

# 4. usbmuxd 服务（插上 iPhone 后自动识别）
systemctl enable --now usbmuxd 2>/dev/null || true

# 5. 桌面入口（KDE 开始菜单）
cat > "$DESKTOP" <<'EOF'
[Desktop Entry]
Type=Application
Name=iPhoBack 相册备份
Name[en]=iPhoBack Photo Backup
GenericName=iPhone 照片备份
GenericName[en]=iPhone Photo Backup
Comment=把 iPhone 相册备份到本机（支持 HEIC、实况照片、增量续传）
Comment[en]=Back up your iPhone photo library (HEIC, Live Photos, incremental)
Exec=iphoback
Icon=camera-photo
Terminal=false
Categories=Graphics;Utility;Photography;
Keywords=iphone;照片;相册;备份;backup;photo;
StartupNotify=true
EOF

echo "==> 安装完成。"
echo "    启动方式：开始菜单「iPhoBack 相册备份」，或终端执行 iphoback"
echo
echo "首次使用步骤："
echo "  1. 用数据线连接 iPhone，解锁屏幕"
echo "  2. 手机弹出「信任此电脑？」→ 点信任并输入锁屏密码"
echo "  3. iPhoBack 里点「扫描相册」→「开始备份」"
