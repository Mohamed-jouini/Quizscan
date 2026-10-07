#!/bin/sh
set -e
mkdir -p data media
python manage.py migrate --noinput
python manage.py collectstatic --noinput
# timeout long : téléversement de gros PDF de scans (la correction elle-même
# tourne en arrière-plan dans le worker, voir grader/services.py)
exec gunicorn quizscan.wsgi:application \
    --bind 0.0.0.0:8000 --workers 2 --threads 2 --timeout 600
