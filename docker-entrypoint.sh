#!/bin/sh
set -e
mkdir -p data media
python manage.py migrate --noinput
python manage.py collectstatic --noinput
# Fiches « grille » générées avant le cadre QR : mises à jour sans toucher à
# ce que le scan lit. Ne doit jamais empêcher le démarrage.
python manage.py moderniser_fiches || true
# timeout long : téléversement de gros PDF de scans (la correction elle-même
# tourne en arrière-plan dans le worker, voir grader/services.py).
# Processus web : un par processeur (de 2 à 8), sauf GUNICORN_WORKERS dans .env.
# Deux par défaut laissaient la moitié d'un serveur à 4 processeurs sans emploi.
PROCESSEURS=$(nproc 2>/dev/null || echo 2)
if [ "$PROCESSEURS" -lt 2 ]; then PROCESSEURS=2; fi
if [ "$PROCESSEURS" -gt 8 ]; then PROCESSEURS=8; fi
exec gunicorn quizscan.wsgi:application \
    --bind 0.0.0.0:8000 --workers "${GUNICORN_WORKERS:-$PROCESSEURS}" --threads 2 --timeout 600
