#!/bin/bash
set -e

SERVICE_NAME="mimo-proxy"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
LOCAL_SERVICE_FILE="./mimo-proxy.service"
PROXY_PY="./mimo_proxy.py"
PYTHON_PATH="/root/.local/share/mise/installs/python/3.12.13/bin/python"

show_usage() {
    echo "MiMo Proxy 管理脚本"
    echo "==================="
    echo "用法: $0 <命令>"
    echo ""
    echo "命令列表:"
    echo "  start          - 启动代理服务（前台运行）"
    echo "  stop           - 停止 systemd 服务"
    echo "  restart        - 重启 systemd 服务"
    echo "  status         - 查看服务状态"
    echo "  enable         - 启用开机自启"
    echo "  disable        - 禁用开机自启"
    echo "  install        - 安装 systemd 服务"
    echo "  uninstall      - 卸载 systemd 服务（删除配置）"
    echo "  logs          - 查看服务日志"
    echo "  help          - 显示此帮助信息"
    echo ""
    echo "示例:"
    echo "  $0 start              # 前台启动代理"
    echo "  $0 install            # 安装为 systemd 服务"
    echo "  $0 enable             # 启用开机自启"
    echo "  $0 status             # 查看状态"
    echo "  $0 uninstall          # 完全卸载"
}

cmd_start() {
    echo "🚀 启动 MiMo Proxy（前台模式）..."
    export PATH="/root/.local/share/mise/installs/python/3.12.13/bin:$PATH"
    exec "$PYTHON_PATH" "$PROXY_PY"
}

cmd_stop() {
    echo "⏹️  停止服务..."
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        sudo systemctl stop "$SERVICE_NAME"
        echo "✅ 服务已停止"
    else
        echo "ℹ️  服务未运行"
    fi
}

cmd_restart() {
    echo "🔄 重启服务..."
    sudo systemctl restart "$SERVICE_NAME"
    echo "✅ 服务已重启"
}

cmd_status() {
    echo "📊 服务状态:"
    systemctl status "$SERVICE_NAME"
}

cmd_enable() {
    echo "🔧 启用开机自启..."
    sudo systemctl enable "$SERVICE_NAME"
    echo "✅ 已设置开机自启"
}

cmd_disable() {
    echo "🔧 禁用开机自启..."
    sudo systemctl disable "$SERVICE_NAME"
    echo "✅ 已禁用开机自启"
}

cmd_install() {
    echo "📦 安装 systemd 服务..."
    
    if [ ! -f "$LOCAL_SERVICE_FILE" ]; then
        echo "❌ 错误: 未找到 $LOCAL_SERVICE_FILE"
        exit 1
    fi
    
    sudo cp "$LOCAL_SERVICE_FILE" "$SERVICE_FILE"
    sudo systemctl daemon-reload
    echo "✅ 服务配置已安装"
    
    read -p "是否立即启动服务？(y/N): " choice
    if [ "$choice" = "y" ] || [ "$choice" = "Y" ]; then
        sudo systemctl start "$SERVICE_NAME"
        echo "✅ 服务已启动"
    fi
    
    read -p "是否启用开机自启？(y/N): " choice
    if [ "$choice" = "y" ] || [ "$choice" = "Y" ]; then
        sudo systemctl enable "$SERVICE_NAME"
        echo "✅ 已设置开机自启"
    fi
}

cmd_uninstall() {
    echo "🗑️  卸载服务..."
    
    read -p "确认要卸载 MiMo Proxy 服务吗？这将删除 systemd 配置。(y/N): " choice
    if [ "$choice" != "y" ] && [ "$choice" != "Y" ]; then
        echo "取消操作"
        exit 0
    fi
    
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        sudo systemctl stop "$SERVICE_NAME"
        echo "✅ 服务已停止"
    fi
    
    sudo systemctl disable "$SERVICE_NAME"
    echo "✅ 已禁用开机自启"
    
    if [ -f "$SERVICE_FILE" ]; then
        sudo rm "$SERVICE_FILE"
        echo "✅ 已删除 $SERVICE_FILE"
    fi
    
    sudo systemctl daemon-reload
    sudo systemctl reset-failed
    echo "✅ 服务已完全卸载"
}

cmd_logs() {
    echo "📋 查看服务日志（按 Ctrl+C 退出）..."
    journalctl -u "$SERVICE_NAME" -f
}

case "$1" in
    start)
        cmd_start
        ;;
    stop)
        cmd_stop
        ;;
    restart)
        cmd_restart
        ;;
    status)
        cmd_status
        ;;
    enable)
        cmd_enable
        ;;
    disable)
        cmd_disable
        ;;
    install)
        cmd_install
        ;;
    uninstall)
        cmd_uninstall
        ;;
    logs)
        cmd_logs
        ;;
    help|--help|-h)
        show_usage
        ;;
    *)
        echo "❌ 未知命令: $1"
        show_usage
        exit 1
        ;;
esac
