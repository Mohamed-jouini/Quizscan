#!/bin/sh
# Passe QuizScan de SQLite à PostgreSQL, une fois, sur le serveur :
#     sh deploy/passer_a_postgres.sh
#
# À lancer en dehors de toute correction : l'application s'arrête le temps de la
# copie. Le fichier SQLite n'est jamais modifié. Au moindre problème, le script
# remet .env comme avant et relance l'application sur SQLite.
set -eu

cd "$(dirname "$0")/.."
AVANT=data/.env.avant-postgres
MOT_DE_PASSE=data/.postgres-mot-de-passe

log() { echo "$(date '+%F %T') $*"; }
stop() { log "ARRÊT : $*"; exit 1; }

revenir_sur_sqlite() {
    log "$1"
    log "Retour sur SQLite…"
    cp "$AVANT" .env
    docker compose up -d --remove-orphans quizscan
    log "L'application tourne de nouveau sur SQLite, rien n'est perdu."
    exit 1
}

# ---------------------------------------------------------------- contrôles
[ -f .env ] || stop "fichier .env absent : lancez ce script depuis le dossier de QuizScan."
if grep -q '^POSTGRES_DB=' .env; then
    stop "PostgreSQL est déjà configuré dans .env : rien à faire."
fi
if grep -q '^COMPOSE_FILE=' .env; then
    stop ".env contient déjà COMPOSE_FILE : suivez la marche à la main (README)."
fi
[ -f data/db.sqlite3 ] || stop "data/db.sqlite3 introuvable : pas de base SQLite à copier."
docker compose exec -T quizscan python manage.py help passer_a_postgres >/dev/null 2>&1 \
    || stop "l'application ne tourne pas, ou cette version n'est pas encore déployée."
EN_COURS=$(docker compose exec -T quizscan python manage.py shell -v 0 -c \
    "from grader.models import ScanBatch; print(ScanBatch.objects.filter(status='processing').count())" \
    | tr -d '\r' | tail -n 1)
if [ "$EN_COURS" != "0" ]; then
    stop "$EN_COURS lot(s) en cours de correction ou interrompu(s) : attendez la fin, ou reprenez-les d'abord."
fi

# ---------------------------------------------------------------- préparation
log "Sauvegarde de la base…"
docker compose exec -T quizscan python manage.py sauvegarde --sans-medias \
    --dossier data/sauvegardes/avant-postgres --garder 3 \
    || stop "sauvegarde impossible : rien n'a été changé."

# Même mot de passe si le script est relancé : PostgreSQL garde celui de sa création.
if [ ! -f "$MOT_DE_PASSE" ]; then
    (umask 077; docker compose exec -T quizscan python -c \
        "import secrets; print(secrets.token_urlsafe(32))" | tr -d '\r' > "$MOT_DE_PASSE")
fi
[ -s "$MOT_DE_PASSE" ] || stop "mot de passe non généré : rien n'a été changé."

(umask 077; cp .env "$AVANT")
{
    echo ""
    echo "# PostgreSQL (deploy/passer_a_postgres.sh)"
    echo "COMPOSE_FILE=docker-compose.yml:docker-compose.postgres.yml"
    echo "POSTGRES_DB=quizscan"
    echo "POSTGRES_USER=quizscan"
    echo "POSTGRES_PASSWORD=$(cat "$MOT_DE_PASSE")"
    echo "POSTGRES_HOST=db"
} >> .env

# ---------------------------------------------------------------- bascule
log "Arrêt de l'application et démarrage de PostgreSQL…"
docker compose stop quizscan || revenir_sur_sqlite "L'application ne s'arrête pas."
docker compose up -d db || revenir_sur_sqlite "PostgreSQL ne démarre pas."

log "Copie des données…"
docker compose run --rm quizscan python manage.py passer_a_postgres \
    || revenir_sur_sqlite "La copie a échoué ou n'est pas fidèle."

log "Redémarrage de l'application sur PostgreSQL…"
docker compose up -d --build --remove-orphans \
    || revenir_sur_sqlite "L'application ne redémarre pas sur PostgreSQL."
sleep 10
docker compose exec -T quizscan python manage.py verifier_deploiement | grep -q 'Base postgresql joignable' \
    || revenir_sur_sqlite "L'application ne répond pas sur PostgreSQL."

# ---------------------------------------------------------------- après
log "Sauvegarde au nouveau format…"
docker compose exec -T quizscan python manage.py sauvegarde --sans-medias \
    || log "ATTENTION : sauvegarde impossible, relancez-la à la main."

log "TERMINÉ : QuizScan tourne sur PostgreSQL."
log "Vérifiez le site. Pour revenir sur SQLite : cp $AVANT .env && docker compose up -d --remove-orphans"
log "(ce qui aura été saisi depuis la bascule n'existe pas dans SQLite)."
