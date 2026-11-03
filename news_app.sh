#!/bin/bash

APP_NAME="news.py"
APP_DIR="${NEWS_APP_DIR:-$HOME/APPS/NEWS}"
PID_FILE="/tmp/news_app.pid"
UNIT_NAME="news-dock.service"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/news-dock"
LOG_FILE="$STATE_DIR/news-dock.log"
CRASH_LOG_FILE="$STATE_DIR/crash.log"
CONSOLE_LOG_FILE="$STATE_DIR/console.log"

start_app() {
    echo "Starting News App..."
    mkdir -p "$STATE_DIR"
    cd "$APP_DIR" || exit 1
    if command -v systemd-run >/dev/null 2>&1 \
            && systemctl --user is-system-running >/dev/null 2>&1; then
        if systemctl --user is-active --quiet "$UNIT_NAME"; then
            echo "News App is already running"
            return
        fi
        systemd-run --user --quiet --collect \
            --unit="$UNIT_NAME" \
            --property="WorkingDirectory=$APP_DIR" \
            --setenv="NEWS_DOCK_LOG_FILE=$LOG_FILE" \
            --setenv="NEWS_DOCK_CRASH_LOG_FILE=$CRASH_LOG_FILE" \
            "$APP_DIR/venv/bin/python" "$APP_DIR/$APP_NAME"
        sleep 1
        systemctl --user show --property=MainPID --value "$UNIT_NAME" > "$PID_FILE"
    else
        source venv/bin/activate
        NEWS_DOCK_LOG_FILE="$LOG_FILE" \
        NEWS_DOCK_CRASH_LOG_FILE="$CRASH_LOG_FILE" \
            nohup python3 "$APP_NAME" >> "$CONSOLE_LOG_FILE" 2>&1 &
        echo $! > "$PID_FILE"
    fi
    echo "News App started with PID $(cat "$PID_FILE")"
    echo "Action log: $LOG_FILE"
    echo "Crash log:  $CRASH_LOG_FILE"
}

stop_app() {
    if systemctl --user is-active --quiet "$UNIT_NAME" 2>/dev/null; then
        systemctl --user stop "$UNIT_NAME"
        rm -f "$PID_FILE"
        echo "News App stopped"
        return
    fi
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            kill "$PID"
            rm "$PID_FILE"
            echo "News App stopped"
        else
            echo "News App is not running"
            rm "$PID_FILE"
        fi
    else
        echo "News App is not running"
    fi
}

status_app() {
    if systemctl --user is-active --quiet "$UNIT_NAME" 2>/dev/null; then
        PID=$(systemctl --user show --property=MainPID --value "$UNIT_NAME")
        echo "News App is running with PID $PID"
        echo "Action log: $LOG_FILE"
        return
    fi
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "News App is running with PID $PID"
            echo "Action log: $LOG_FILE"
        else
            echo "News App is not running"
            rm "$PID_FILE"
        fi
    else
        echo "News App is not running"
    fi
}

case "$1" in
    start)
        start_app
        ;;
    stop)
        stop_app
        ;;
    restart)
        stop_app
        sleep 2
        start_app
        ;;
    status)
        status_app
        ;;
    logs)
        mkdir -p "$STATE_DIR"
        touch "$LOG_FILE"
        tail -n 200 "$LOG_FILE"
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs}"
        exit 1
        ;;
esac
