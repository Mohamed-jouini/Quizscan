#!/bin/sh
# Installe PostgreSQL sur le serveur et y copie les données de l'ancienne base
# SQLite, une fois, depuis le dossier de QuizScan :
#     sh deploy/passer_a_postgres.sh
#
# QuizScan ne fonctionne plus que sur PostgreSQL : tant que ce script n'a pas été
# lancé, la nouvelle version ne démarre pas (l'ancienne continue de tourner).
# À lancer en dehors de toute correction : l'application s'arrête pendant la copie.
# data/db.sqlite3 n'est pas modifié. Quand tout est vérifié, on le supprime par :
#     sh deploy/passer_a_postgres.sh --supprimer-sqlite
set -eu

cd "$(dirname "$0")/.."
FAIT=data/.postgres-copie-faite

log() { echo "$(date '+%F %T') $*"; }
stop() { log "ARRÊT : $*"; exit 1; }

base_active() {
    docker compose exec -T quizscan python manage.py verifier_deploiement 2>/dev/null \
        | grep -q 'Base postgresql joignable'
}

panne() {
    docker compose ps -a || true
    docker compose logs --tail=60 quizscan || true
    stop "$1 Votre base SQLite n'a pas été touchée. Envoyez les lignes ci-dessus."
}

[ -f .env ] || stop "fichier .env absent : lancez ce script depuis le dossier de QuizScan."

# ---------------------------------------------------------------- suppression
if [ "${1:-}" = "--supprimer-sqlite" ]; then
    [ -f "$FAIT" ] || stop "la copie vers PostgreSQL n'a jamais réussi : SQLite est conservé."
    base_active || stop "l'application ne répond pas sur PostgreSQL : SQLite est conservé."
    rm -f data/db.sqlite3 data/db.sqlite3-wal data/db.sqlite3-shm data/.env.avant-postgres
    log "Ancienne base SQLite supprimée. Une copie reste dans data/sauvegardes/avant-postgres/."
    exit 0
fi

# ---------------------------------------------------------------- réglages
# Mot de passe créé une seule fois : PostgreSQL garde celui de sa création.
if ! grep -q '^POSTGRES_PASSWORD=..' .env; then
    MOT_DE_PASSE=$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 40)
    [ "${#MOT_DE_PASSE}" -eq 40 ] || stop "mot de passe non généré : rien n'a été changé."
    (umask 077; cp .env data/.env.avant-postgres)
    {
        echo ""
        echo "# PostgreSQL (deploy/passer_a_postgres.sh)"
        echo "POSTGRES_PASSWORD=$MOT_DE_PASSE"
    } >> .env
fi
# Réglage laissé par une version précédente de ce script, s'il a réussi : le fichier
# n'existe plus, et les données sont déjà dans PostgreSQL.
if grep -q '^COMPOSE_FILE=.*docker-compose\.postgres\.yml' .env; then
    : > "$FAIT"
    sed -i '/^COMPOSE_FILE=.*docker-compose\.postgres\.yml/d' .env
fi

# ---------------------------------------------------------------- contrôle
if docker compose ps --status running --services 2>/dev/null | grep -qx quizscan; then
    EN_COURS=$(docker compose exec -T quizscan python manage.py shell -v 0 -c \
        "from grader.models import ScanBatch; print(ScanBatch.objects.filter(status='processing').count())" \
        2>/dev/null | tr -d '\r' | tail -n 1 || true)
    case "$EN_COURS" in
        ""|0) ;;
        *) stop "$EN_COURS lot(s) en cours de correction ou interrompu(s) : attendez la fin, ou reprenez-les d'abord." ;;
    esac
fi

# ---------------------------------------------------------------- copie
log "Construction de l'application, plusieurs minutes si l'image est à refaire…"
docker compose build quizscan || stop "construction impossible : envoyez les messages ci-dessus. Rien n'a été changé."

log "Arrêt de l'application…"
docker compose stop quizscan || true

if [ -f data/db.sqlite3 ] && [ ! -f "$FAIT" ]; then
    SECOURS="data/sauvegardes/avant-postgres/db-$(date +%Y%m%d-%H%M%S).sqlite3"
    mkdir -p data/sauvegardes/avant-postgres
    cp -a data/db.sqlite3 "$SECOURS"
    [ ! -f data/db.sqlite3-wal ] || cp -a data/db.sqlite3-wal "$SECOURS-wal"
    log "Copie de secours : $SECOURS"
fi

log "Démarrage de PostgreSQL…"
docker compose up -d --wait db || panne "PostgreSQL ne démarre pas."

if [ -f "$FAIT" ]; then
    log "Données déjà copiées : la copie n'est pas refaite."
elif [ -f data/db.sqlite3 ]; then
    log "Copie des données…"
    docker compose run --rm quizscan python manage.py passer_a_postgres \
        || panne "La copie a échoué ou n'est pas fidèle."
    : > "$FAIT"
else
    log "Pas de data/db.sqlite3 : PostgreSQL démarre avec une base vide."
fi

# ---------------------------------------------------------------- démarrage
log "Démarrage de l'application sur PostgreSQL…"
docker compose up -d --remove-orphans || panne "L'application ne démarre pas."
n=0
until base_active || [ "$n" -ge 36 ]; do
    n=$((n + 1))
    sleep 5
done
base_active || panne "L'application ne répond pas sur PostgreSQL."

log "Sauvegarde au nouveau format…"
docker compose exec -T quizscan python manage.py sauvegarde --sans-medias \
    || log "ATTENTION : sauvegarde impossible, relancez-la à la main."

log "TERMINÉ : QuizScan tourne sur PostgreSQL."
log "Vérifiez le site (comptes, classes, notes), puis supprimez l'ancienne base :"
log "    sh deploy/passer_a_postgres.sh --supprimer-sqlite"
