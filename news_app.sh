#!/bin/bash

APP_NAME="news.py"
APP_DIR="~/APPS/NEWS"

start_app() {
    echo "Starting News App..."
    cd ~/APPS/NEWS
    source venv/bin/activate
    python3 news.py &
    echo $! > /tmp/news_app.pid
    echo "News App started with PID $(cat /tmp/news_app.pid)"
}

stop_app() {
    if [ -f /tmp/news_app.pid ]; then
        PID=$(cat /tmp/news_app.pid)
        if kill -0 $PID 2>/dev/null; then
            kill $PID
            rm /tmp/news_app.pid
            echo "News App stopped"
        else
            echo "News App is not running"
            rm /tmp/news_app.pid
        fi
    else
        echo "News App is not running"
    fi
}

status_app() {
    if [ -f /tmp/news_app.pid ]; then
        PID=$(cat /tmp/news_app.pid)
        if kill -0 $PID 2>/dev/null; then
            echo "News App is running with PID $PID"
        else
            echo "News App is not running"
            rm /tmp/news_app.pid
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
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
