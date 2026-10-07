#!/bin/sh
# Lance toute la suite de tests DANS l'image Docker de l'application.
#
# Sur un poste sans Tesseract, trois tests (lecture d'un nom manuscrit)
# échouent faute de moteur OCR. L'image, elle, l'embarque avec le français et
# l'arabe : c'est le seul endroit où la suite entière peut passer.
#
#   sh deploy/tests.sh
#
# Conteneur jetable et sans aucun volume : les tests ne touchent ni à data/
# ni à media/ du serveur. Code de sortie non nul si un test échoue.
set -eu

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"

docker compose build --quiet quizscan
IMAGE="$(docker compose config --images 2>/dev/null | head -n 1 || true)"
IMAGE="${IMAGE:-$(basename "$APP_DIR")-quizscan}"

docker run --rm \
    -e QUIZSCAN_TEST=1 -e DEBUG=1 \
    -e SECRET_KEY=tests-uniquement -e ALLOWED_HOSTS=localhost \
    --entrypoint sh "$IMAGE" -c '
        echecs=0
        for test in test_e2e*.py; do
            if python "$test" > /tmp/sortie 2>&1; then
                echo "OK      $test"
            else
                echo "ECHEC   $test"
                tail -n 15 /tmp/sortie | sed "s/^/        /"
                echecs=$((echecs + 1))
            fi
        done
        echo
        if [ "$echecs" -eq 0 ]; then
            echo "Tous les tests passent."
        else
            echo "$echecs test(s) en échec."
            exit 1
        fi'
