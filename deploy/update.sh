#!/bin/sh
# Mise à jour automatique de QuizScan depuis GitHub.
#
# Vérifie s'il y a de nouveaux commits sur la branche suivie ; si oui :
# sauvegarde la base, récupère le code, reconstruit et redémarre le conteneur
# (les migrations s'appliquent au démarrage). Sinon, ne fait rien.
# Lancé toutes les 5 minutes par quizscan-update.timer (voir README).
#
# Les fichiers propres au serveur ne sont jamais touchés : .env (réglages),
# data/ (base de données), media/ (fiches, scans) — ils ne sont pas versionnés.
set -eu

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"     # dossier quizscan/
BRANCH="${QUIZSCAN_BRANCH:-$(git -C "$APP_DIR" rev-parse --abbrev-ref HEAD)}"
KEEP_BACKUPS=15

cd "$APP_DIR"
log() { echo "$(date '+%F %T') $*"; }

git fetch --quiet origin "$BRANCH"
LOCAL=$(git rev-parse --short HEAD)
REMOTE=$(git rev-parse --short "origin/$BRANCH")
if [ "$LOCAL" = "$REMOTE" ] && [ "${1:-}" != "--force" ]; then
    exit 0                                   # rien de nouveau
fi
log "Mise à jour $LOCAL -> $REMOTE ($BRANCH)"

if [ ! -f .env ]; then
    log "ERREUR : fichier .env absent (cp .env.example .env), mise à jour annulée"
    exit 1
fi

# Sauvegarde de la base avant les migrations éventuelles
if [ -f data/db.sqlite3 ]; then
    mkdir -p data/backups
    cp data/db.sqlite3 "data/backups/db-$(date +%Y%m%d-%H%M%S)-$LOCAL.sqlite3"
    ls -1t data/backups/db-*.sqlite3 | tail -n +$((KEEP_BACKUPS + 1)) | xargs -r rm -f
fi

git reset --hard --quiet "origin/$BRANCH"
docker compose up -d --build --remove-orphans
docker image prune -f >/dev/null 2>&1 || true

# Contrôle : le conteneur doit tourner
sleep 5
if docker compose ps --status running --services | grep -qx quizscan; then
    log "OK : QuizScan mis à jour et redémarré"
else
    log "ERREUR : le conteneur ne tourne pas — voir : docker compose logs quizscan"
    exit 1
fi
