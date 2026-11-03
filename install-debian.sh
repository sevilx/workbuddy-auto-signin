#!/bin/sh
# install-debian.sh —— 在 Debian/Ubuntu 上为 workbuddy-auto-signin 注册 systemd 定时任务
#
# 由 Windows 侧的 sync-auth.py deploy 自动上传执行；也可以手动运行：
#     sudo bash install-debian.sh /opt/workbuddy-auto-signin
#
# 它只做三件事：生成 run.sh、写两个 systemd unit、启动 timer。
# 凭据文件 auth.json 由 sync-auth.py 推送，本脚本不碰。

set -eu

DIR="${1:-/opt/workbuddy-auto-signin}"
SIGNIN="$DIR/signin.py"
AUTH="$DIR/auth.json"
RUNNER="$DIR/run.sh"
LOG="/var/log/workbuddy-signin.log"

SERVICE="workbuddy-signin"
POLL_SERVICE="workbuddy-signin-poll"

say() { printf '%s\n' "$*"; }
die() { printf '错误：%s\n' "$*" >&2; exit 1; }

# ---- 前置检查 -----------------------------------------------------------
[ "$(id -u)" -eq 0 ] || die "请用 root 运行（sudo bash $0）"
[ -f "$SIGNIN" ] || die "找不到 $SIGNIN（先跑 sync-auth.py deploy）"
command -v systemctl >/dev/null 2>&1 || die "没有 systemd，无法安装定时任务"

PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "没有 python3，请先 apt install python3"
"$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 7) else 1)' \
    || die "python3 版本过低（需 3.7+）：$("$PY" -V 2>&1)"
"$PY" -m py_compile "$SIGNIN" || die "signin.py 语法检查未通过"

say "python : $PY ($("$PY" -V 2>&1))"
say "目录   : $DIR"

# ---- 1. 运行包装 -------------------------------------------------------
cat > "$RUNNER" <<EOF
#!/bin/sh
# 由 install-debian.sh 生成，请勿手改（改动会在下次同步时被覆盖）
set -eu
export WORKBUDDY_AUTH_FILE='$AUTH'
export WORKBUDDY_SIGNIN_LOG='$LOG'
exec $PY '$SIGNIN' "\$@"
EOF
chmod 755 "$RUNNER"
say "已生成 $RUNNER"

# ---- 2. systemd unit ---------------------------------------------------
# systemd 249+ 支持在 OnCalendar 里写时区，这样宿主机是 UTC 也能准点；
# 更老的版本只能用本机时区，届时提示用户自行核对。
SD_VER="$(systemctl --version 2>/dev/null | head -1 | awk '{print $2}')"
TZ_SUFFIX=""
if [ -n "${SD_VER:-}" ] && [ "$SD_VER" -ge 249 ] 2>/dev/null; then
    TZ_SUFFIX=" Asia/Shanghai"
else
    HOST_TZ="$(date +%z)"
    [ "$HOST_TZ" = "+0800" ] || say "提示：systemd $SD_VER 不支持在 OnCalendar 指定时区，且本机时区为 $HOST_TZ，签到时间会按本机时区触发"
fi

cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=WorkBuddy 每日自动签到
Documentation=https://github.com/88lin/workbuddy-auto-signin
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$DIR
ExecStart=$RUNNER silent
# 与 signin.py 内 MAX_BUDGET_SECONDS(540) 对齐，留 60 秒给解释器启动和收尾
TimeoutStartSec=600
EOF

cat > "/etc/systemd/system/$SERVICE.timer" <<EOF
[Unit]
Description=每天 00:05 领取 WorkBuddy 签到积分

[Timer]
OnCalendar=*-*-* 00:05:00$TZ_SUFFIX
Persistent=true
AccuracySec=1min
RandomizedDelaySec=60

[Install]
WantedBy=timers.target
EOF

cat > "/etc/systemd/system/$POLL_SERVICE.service" <<EOF
[Unit]
Description=WorkBuddy 补签与成长中心轮询
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$DIR
ExecStart=$RUNNER silent-poll
# signin.py 轮询模式的上限是 240 秒，这里留到 300
TimeoutStartSec=300
EOF

cat > "/etc/systemd/system/$POLL_SERVICE.timer" <<EOF
[Unit]
Description=每 4 小时补签并领取成长中心奖励（01/05/09/13/17/21 点）

[Timer]
OnCalendar=*-*-* 01,05,09,13,17,21:05:00$TZ_SUFFIX
Persistent=true
AccuracySec=1min
RandomizedDelaySec=120

[Install]
WantedBy=timers.target
EOF

say "已写入 /etc/systemd/system/$SERVICE.{service,timer} 与 $POLL_SERVICE.{service,timer}"

# ---- 3. 启用 -----------------------------------------------------------
systemctl daemon-reload
systemctl enable --now "$SERVICE.timer" >/dev/null
systemctl enable --now "$POLL_SERVICE.timer" >/dev/null

# 凭据还没推送时给出明确提示，而不是让用户等到明天发现 NO_AUTH
if [ -f "$AUTH" ]; then
    chmod 600 "$AUTH"
    say "凭据   : $AUTH 已存在"
else
    say "凭据   : $AUTH 尚未推送（Windows 侧跑 sync-auth.py push 即可）"
fi
[ -f "$LOG" ] || : > "$LOG"
chmod 640 "$LOG" 2>/dev/null || true

say ""
say "定时任务已就位："
systemctl list-timers --all "$SERVICE.timer" "$POLL_SERVICE.timer" --no-pager 2>/dev/null | sed 's/^/  /'
say ""
say "常用命令："
say "  systemctl list-timers workbuddy-signin*     # 看下次触发"
say "  journalctl -u $SERVICE -n 30                # 看签到日志"
say "  tail -n 20 $LOG                             # 看签到结果（JSON，每行一条）"
say "  systemctl start $SERVICE                    # 立刻手动跑一次"
