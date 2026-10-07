#!/bin/sh
# Sauvegarde nocturne de QuizScan : base de données ET fichiers (copies
# scannées, images de contrôle, corrigés scannés).
# Lancé chaque nuit par quizscan-sauvegarde.timer (voir README).
#
# Réglages facultatifs, dans .env :
#   SAUVEGARDE_GARDER=7          archives conservées (sur ce serveur et sur la copie)
#   SAUVEGARDE_COPIE=/mnt/nas/quizscan
#       second support où recopier chaque archive. FORTEMENT conseillé : une
#       sauvegarde restée sur le disque qu'elle protège disparaît avec lui.
set -eu

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"     # dossier quizscan/
cd "$APP_DIR"
log() { echo "$(date '+%F %T') $*"; }
reglage() { sed -n "s/^$1=//p" .env 2>/dev/null | tail -n 1; }

GARDER="$(reglage SAUVEGARDE_GARDER)"; GARDER="${GARDER:-7}"
COPIE="$(reglage SAUVEGARDE_COPIE)"

log "Sauvegarde de QuizScan"
docker compose exec -T quizscan python manage.py sauvegarde --garder "$GARDER"

DERNIERE=$(ls -1t data/sauvegardes/quizscan-*.zip 2>/dev/null | grep -v -- '-base\.zip$' | head -n 1 || true)
if [ -z "$DERNIERE" ]; then
    log "ERREUR : aucune archive trouvée dans data/sauvegardes"
    exit 1
fi

if [ -n "$COPIE" ]; then
    if ! mkdir -p "$COPIE" 2>/dev/null || [ ! -w "$COPIE" ]; then
        log "ERREUR : copie impossible vers $COPIE (non monté ou en lecture seule)"
        exit 1
    fi
    cp "$DERNIERE" "$COPIE/"
    # même rotation sur la copie que sur le serveur
    ls -1t "$COPIE"/quizscan-*.zip 2>/dev/null | grep -v -- '-base\.zip$' \
        | tail -n +$((GARDER + 1)) | xargs -r rm -f
    log "OK : $(basename "$DERNIERE") recopiée vers $COPIE"
else
    log "OK : $(basename "$DERNIERE") — ATTENTION, aucune copie hors du serveur (SAUVEGARDE_COPIE vide)"
fi
