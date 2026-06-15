#!/bin/sh
# Start gunicorn web server + monitoring process in parallel
python3 -u monitor.py &
exec gunicorn -w 1 -b 0.0.0.0:8080 --timeout 120 app:app
