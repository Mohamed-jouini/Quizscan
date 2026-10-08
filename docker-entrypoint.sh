#!/bin/sh
set -e
mkdir -p data media
python manage.py migrate --noinput
python manage.py collectstatic --noinput
# Fiches « grille » générées avant le cadre QR : mises à jour sans toucher à
# ce que le scan lit. Ne doit jamais empêcher le démarrage.
python manage.py moderniser_fiches || true
# timeout long : téléversement de gros PDF de scans (la correction elle-même
# tourne en arrière-plan dans le worker, voir grader/services.py)
exec gunicorn quizscan.wsgi:application \
    --bind 0.0.0.0:8000 --workers 2 --threads 2 --timeout 600
