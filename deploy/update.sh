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

# Sauvegarde de la base avant les migrations éventuelles. Par la commande
# de l'application : copie cohérente même si une correction écrit au même
# moment (un « cp » de la base en service peut donner un fichier corrompu).
# Base seule, pour aller vite ; la sauvegarde complète est celle de la nuit.
if ! docker compose exec -T quizscan python manage.py sauvegarde --sans-medias \
        --dossier data/sauvegardes/avant-mise-a-jour --garder "$KEEP_BACKUPS"; then
    # Conteneur arrêté, ou version trop ancienne pour connaître la commande.
    if [ -f data/db.sqlite3 ]; then
        log "Sauvegarde par la commande impossible : simple copie de la base"
        mkdir -p data/backups
        cp data/db.sqlite3 "data/backups/db-$(date +%Y%m%d-%H%M%S)-$LOCAL.sqlite3"
        ls -1t data/backups/db-*.sqlite3 | tail -n +$((KEEP_BACKUPS + 1)) | xargs -r rm -f
    fi
fi

git reset --hard --quiet "origin/$BRANCH"

# Facultatif (TESTS_AVANT_MISE_A_JOUR=1 dans .env) : la suite de tests tourne
# dans la nouvelle image AVANT qu'elle remplace l'ancienne. En cas d'échec, le
# code revient à la version en service, qui continue de tourner, et ce commit
# est mis de côté : on ne relance pas les tests toutes les 5 minutes sur le
# même échec. Le prochain commit sera essayé normalement.
TESTS="$(sed -n 's/^TESTS_AVANT_MISE_A_JOUR=//p' .env | tail -n 1)"
if [ "$TESTS" = "1" ]; then
    ECHEC="data/.tests-en-echec-$REMOTE"
    if [ -f "$ECHEC" ] && [ "${1:-}" != "--force" ]; then
        git reset --hard --quiet "$LOCAL"
        exit 0                               # déjà essayé, déjà refusé
    fi
    log "Tests de $REMOTE dans la nouvelle image…"
    if ! sh deploy/tests.sh; then
        touch "$ECHEC"
        git reset --hard --quiet "$LOCAL"
        log "ERREUR : tests en échec sur $REMOTE — mise à jour annulée, $LOCAL reste en service"
        exit 1
    fi
    rm -f data/.tests-en-echec-*
fi

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
