#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Install, upgrade or remove meshpoint-bridge as a systemd service on the Meshpoint device.
#
#   sudo ./install.sh               install, or upgrade an existing install (safe to re-run)
#   sudo ./install.sh --uninstall   stop and remove the service (asks before deleting settings)
#
# What it sets up:
#   /opt/meshpoint-bridge/                    program files and a Python venv (owned by root)
#   /etc/meshpoint-bridge/bridge.env          settings + dashboard password (mode 600, service user)
#   /etc/systemd/system/meshpoint-bridge.service
#   a 'meshpoint-bridge' system user with no login shell and no sudo rights
set -euo pipefail

SERVICE=meshpoint-bridge
SERVICE_USER=meshpoint-bridge
INSTALL_DIR=/opt/meshpoint-bridge
CONF_DIR=/etc/meshpoint-bridge
CONF_FILE="$CONF_DIR/bridge.env"
UNIT_DST=/etc/systemd/system/$SERVICE.service
MARKER=.installed-by-install-sh
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_FILES=(meshpoint_bridge.py requirements.txt README.md CHANGELOG.md LICENSE install.sh bridge.env.example)

say()  { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
confirm() {  # confirm "question" -> true only on y/yes; false when not interactive
    local reply=""
    [[ -t 0 ]] || return 1
    read -r -p "$1 [y/N]: " reply || true
    [[ $reply =~ ^[Yy]([Ee][Ss])?$ ]]
}

[[ $EUID -eq 0 ]] || die "run with sudo: sudo $0 $*"
command -v systemctl >/dev/null || die "systemd is required"

uninstall() {
    say "Stopping and disabling the $SERVICE service"
    systemctl disable --now "$SERVICE.service" 2>/dev/null || true
    rm -f "$UNIT_DST"
    systemctl daemon-reload

    if [[ -f "$INSTALL_DIR/$MARKER" ]]; then
        say "Removing program files from $INSTALL_DIR"
        for f in "${APP_FILES[@]}"; do rm -f "$INSTALL_DIR/$f"; done
        rm -rf "$INSTALL_DIR/systemd" "$INSTALL_DIR/venv" "$INSTALL_DIR/venv.old" "$INSTALL_DIR/$MARKER"
        rmdir "$INSTALL_DIR" 2>/dev/null || warn "$INSTALL_DIR still has other files in it; left in place"
    elif [[ -d "$INSTALL_DIR/venv" ]]; then
        say "Removing $INSTALL_DIR/venv (the other files there were not installed by this script)"
        rm -rf "$INSTALL_DIR/venv" "$INSTALL_DIR/venv.old"
    fi

    if [[ -e "$CONF_DIR" ]]; then
        if confirm "Delete the settings in $CONF_DIR (includes the saved dashboard password)?"; then
            rm -rf "$CONF_DIR"
            say "Settings deleted"
        else
            say "Settings kept in $CONF_DIR"
        fi
    fi

    if getent passwd "$SERVICE_USER" >/dev/null; then
        if [[ -e "$CONF_DIR" ]]; then
            say "Keeping the $SERVICE_USER user because it still owns $CONF_DIR"
        else
            userdel "$SERVICE_USER" 2>/dev/null || warn "could not remove user $SERVICE_USER"
            say "Removed user $SERVICE_USER"
        fi
    fi
    say "meshpoint-bridge uninstalled"
}

stop_manual_bridges() {
    # A bridge started by hand (e.g. from an earlier manual setup) would hold port 4403.
    local main_pid pids=() pid
    main_pid=$(systemctl show -p MainPID --value "$SERVICE.service" 2>/dev/null || echo 0)
    while read -r pid; do
        if [[ -n $pid && $pid != "$$" && $pid != "$main_pid" ]]; then pids+=("$pid"); fi
    done < <(pgrep -f '[m]eshpoint_bridge\.py' || true)
    if ((${#pids[@]} == 0)); then return 0; fi
    warn "a meshpoint-bridge started by hand is running (PID ${pids[*]}); it would block port 4403"
    if confirm "Stop it now?"; then
        kill -TERM "${pids[@]}" 2>/dev/null || true
        sleep 2
    else
        warn "leaving it running: stop it before the service can start"
    fi
}

install_files() {
    install -d -m 0755 "$INSTALL_DIR"
    if [[ "$SRC_DIR" == "$INSTALL_DIR" ]]; then
        say "Running from $INSTALL_DIR itself: using the files in place"
        chmod o+rx "$INSTALL_DIR"
        chmod o+r "$INSTALL_DIR/meshpoint_bridge.py" "$INSTALL_DIR/requirements.txt"
        return
    fi
    if [[ -e "$INSTALL_DIR/meshpoint_bridge.py" && ! -f "$INSTALL_DIR/$MARKER" ]]; then
        say "Found an earlier manual install in $INSTALL_DIR: updating its program files and venv"
        say "(other files in that folder are left alone)"
    fi
    for f in "${APP_FILES[@]}"; do
        if [[ -f "$SRC_DIR/$f" ]]; then install -m 0644 -o root -g root "$SRC_DIR/$f" "$INSTALL_DIR/$f"; fi
    done
    chmod 0755 "$INSTALL_DIR/install.sh"
    install -d -m 0755 "$INSTALL_DIR/systemd"
    install -m 0644 -o root -g root "$SRC_DIR/systemd/$SERVICE.service" "$INSTALL_DIR/systemd/$SERVICE.service"
    touch "$INSTALL_DIR/$MARKER"
}

build_venv() {
    # Build a fresh venv; keep the old one until the new one works, so a failed upgrade can be undone.
    say "Creating the Python environment (this can take a few minutes)"
    rm -rf "$INSTALL_DIR/venv.old"
    if [[ -d "$INSTALL_DIR/venv" ]]; then mv "$INSTALL_DIR/venv" "$INSTALL_DIR/venv.old"; fi
    if python3 -m venv "$INSTALL_DIR/venv" \
        && "$INSTALL_DIR/venv/bin/python" -m pip install --quiet --disable-pip-version-check \
               -r "$INSTALL_DIR/requirements.txt"; then
        rm -rf "$INSTALL_DIR/venv.old"
        chown -R root:root "$INSTALL_DIR/venv"
    else
        rm -rf "$INSTALL_DIR/venv"
        if [[ -d "$INSTALL_DIR/venv.old" ]]; then mv "$INSTALL_DIR/venv.old" "$INSTALL_DIR/venv"; fi
        die "could not create the Python environment (is python3-venv installed? sudo apt install python3-venv)"
    fi
}

configure() {
    install -d -m 0750 -o root -g "$SERVICE_USER" "$CONF_DIR"
    local run_setup=no
    if [[ ! -f "$CONF_FILE" ]]; then
        [[ -t 0 ]] || die "no settings yet and no terminal to ask in: run 'sudo ./install.sh' interactively"
        run_setup=yes
    elif confirm "Settings already exist in $CONF_FILE. Change them now?"; then
        run_setup=yes
    fi
    if [[ $run_setup == yes ]]; then
        "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/meshpoint_bridge.py" --setup --config "$CONF_FILE" \
            || die "setup did not finish; the service was not started"
    fi
    chown "$SERVICE_USER:$SERVICE_USER" "$CONF_FILE"
    chmod 0600 "$CONF_FILE"
}

install_service() {
    install -m 0644 -o root -g root "$SRC_DIR/systemd/$SERVICE.service" "$UNIT_DST"
    systemctl daemon-reload
    systemctl enable "$SERVICE.service" >/dev/null
    say "Starting the service"
    systemctl restart "$SERVICE.service"
    sleep 5
    if systemctl is-active --quiet "$SERVICE.service"; then
        say "meshpoint-bridge is running"
    else
        warn "the service is not running yet; recent log lines:"
    fi
    journalctl -u "$SERVICE.service" -n 15 --no-pager || true
}

do_install() {
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null \
        || die "Python 3.11 or newer is required"
    for f in meshpoint_bridge.py requirements.txt "systemd/$SERVICE.service"; do
        [[ -f "$SRC_DIR/$f" ]] || die "missing $SRC_DIR/$f: run this script from the meshpoint-bridge folder"
    done
    systemctl cat meshpoint.service >/dev/null 2>&1 \
        || warn "meshpoint.service not found: the bridge expects Meshpoint on this device"

    stop_manual_bridges
    if systemctl is-active --quiet "$SERVICE.service"; then
        say "Stopping the running service for the upgrade"
        systemctl stop "$SERVICE.service"
    fi
    if ! getent passwd "$SERVICE_USER" >/dev/null; then
        say "Creating system user $SERVICE_USER"
        useradd --system --user-group --no-create-home --home-dir /nonexistent \
            --shell /usr/sbin/nologin "$SERVICE_USER"
    fi
    install_files
    build_venv
    configure
    install_service

    cat <<EOF

Done.
  Logs:            journalctl -u $SERVICE -f
  Status:          systemctl status $SERVICE
  Change settings: sudo $INSTALL_DIR/venv/bin/python $INSTALL_DIR/meshpoint_bridge.py --setup
                   then: sudo systemctl restart $SERVICE
  Show settings:   sudo -u $SERVICE_USER $INSTALL_DIR/venv/bin/python $INSTALL_DIR/meshpoint_bridge.py --show-config
  Uninstall:       sudo $INSTALL_DIR/install.sh --uninstall
EOF
}

case "${1:-}" in
    --uninstall) uninstall ;;
    "" | --install) do_install ;;
    -h | --help) sed -n '3,14p' "$0" ;;
    *) die "unknown option $1 (use --uninstall, or no option to install)" ;;
esac
