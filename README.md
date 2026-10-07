# QuizScan — correction automatique de questionnaires scannés

Application web Django qui corrige automatiquement les quiz des étudiants à partir
de pages scannées (scanner professionnel, PDF multi-pages ou images).

## Fonctionnement

0. **Comptes enseignants** : l'application demande une connexion. Créez d'abord
   l'administrateur (`python manage.py createsuperuser`, ou sous Docker
   `docker compose exec quizscan python manage.py createsuperuser`), puis créez
   les comptes des enseignants dans l'interface `/admin` (Utilisateurs →
   Ajouter). Chaque enseignant ne voit que ses propres classes et quiz et crée
   ses QCM lui-même ; l'administrateur voit tout.
1. **Classes** : créez une classe et importez la liste des étudiants, soit par
   **fichier** (Excel `.xlsx`, CSV, Word `.docx` ou PDF — colonnes Nom, Prénom,
   N° d'inscription, voir `modele_candidats.xlsx`), soit par **copier-coller**
   (une ligne par étudiant : `NOM;Prénom;N°` ou `NOM Prénom N°`). Le numéro est
   optionnel mais nécessaire pour la grille d'identification.
2. **Quiz** : créez un quiz en choisissant le **type de fiche** :
   - **Sujet séparé + feuille de réponses** : deux documents — un PDF
     « Sujet » avec les questions et leurs choix (à distribuer, non scanné)
     et un PDF « Feuille de réponses » (grille de cases à remplir et scanner).

   - **Questionnaire complet** (par défaut) : les questions et leurs choix de
     réponse sont imprimés sur la fiche, avec une case à noircir devant chaque
     choix. Saisissez l'intitulé de chaque question et ses choix (un par ligne).
   - **Fiche de réponses seule** : grille compacte de cases (le sujet est
     distribué à part) — pratique avec l'« ajout rapide » qui crée toutes les
     questions en saisissant le corrigé (ex. `ABCADB`).

   Les questions se saisissent une à une, par **ajout rapide** du corrigé, par
   **import d'un questionnaire existant** (voir plus bas) ou depuis la **banque
   de questions** : « Reprendre les questions du quiz… » copie tout ou partie
   (ex. `1-5, 8`) des questions d'une de vos épreuves précédentes.

   Le **nombre de choix (A, B, C…) se règle pour chaque question** : il suit
   les choix saisis, ou le champ « Nombre de choix » de la question (et de
   l'ajout rapide) quand seul le corrigé est donné.

   Deux types de questions :
   - **QCM** (barème + bonne réponse) — lus automatiquement ;
   - **Manuscrites** (barème + hauteur de la zone) — recadrées automatiquement
     et corrigées à l'écran.

   La fiche passe automatiquement sur plusieurs pages si nécessaire (jusqu'à
   62 pages, le nombre de repères disponibles) ; chaque
   page porte ses propres repères et la zone nom, et les pages d'un même
   étudiant sont regroupées automatiquement.
3. **Fiche de réponses** : cliquez sur « Générer la fiche PDF » et imprimez-la
   en A4, **sans mise à l'échelle (100 %)** — une copie par étudiant.
   Les 4 repères ArUco dans les coins permettent le recalage automatique du scan.
4. **Scan** : après l'épreuve, scannez toutes les copies (200–300 dpi, couleur ou
   niveaux de gris) et téléversez le PDF ou les images sur la page du quiz.
   **La correction démarre dès l'envoi, en arrière-plan** : la page du lot
   affiche un bandeau « N / total copies notées » qui se met à jour tout seul,
   et les notes déjà calculées sont visibles dans les résultats. Il n'y a pas
   de limite de volume (un concours de plusieurs milliers de copies se traite
   page par page) ; si le serveur redémarre en cours de route, un bouton
   « Reprendre la correction » relance le lot sans relire les pages déjà notées.
5. L'application, pour chaque page :
   - détecte les repères, redresse l'image (rotation/inclinaison corrigées) ;
   - lit le **NOM / PRÉNOM** par OCR (Tesseract) et l'associe à l'étudiant de la
     classe par rapprochement flou ;
   - lit les cases noircies, applique le **barème** et calcule :
     note, questions répondues, correctes, **fausses** et sans réponse ;
   - recadre les zones manuscrites pour la notation à l'écran.
6. **Vérification** : chaque copie affiche une image de contrôle (vert = juste,
   rouge = faux, bleu = réponse attendue, orange = plusieurs cases). Vous pouvez
   réaffecter un étudiant non reconnu, corriger une case mal lue et noter les
   questions manuscrites. **Le corrigé et le barème restent modifiables** sur la
   page du quiz, même après le scan : toutes les notes sont recalculées.
7. **Résultats** : tableau par étudiant, **statistiques de l'épreuve**
   (moyenne, médiane, histogramme des notes, taux de réussite par question —
   les questions les plus ratées sont surlignées), **export Excel** (résultats
   + feuille statistiques) et **bulletins PDF** (relevé individuel par
   étudiant, en un seul PDF pour toute la classe ou un par un).

### Identification des copies : trois modes au choix (par quiz)

- **OCR du nom manuscrit** (par défaut) : fiches identiques pour toute la
  classe ; l'étudiant écrit son nom, l'application le lit et le rapproche de
  la liste de classe. Simple, mais dépend de la lisibilité de l'écriture.
- **Grille de n° d'inscription** : fiches identiques ; l'étudiant noircit les
  chiffres de son numéro dans une grille imprimée sur chaque page. Lecture
  exacte (pas d'OCR). Renseignez le numéro d'inscription des étudiants
  (3e colonne de l'import) et le nombre de chiffres du quiz.
- **QR code — un par candidat**, sous deux formes :
  - *fiches nominatives* : l'application génère **une fiche par étudiant**
    dans un seul PDF, avec son nom pré-imprimé et un QR code d'identification ;
  - *étiquettes autocollantes* : fiches identiques avec un **emplacement
    réservé** ; la page de la classe imprime en un clic la **planche
    d'étiquettes** sur papier autocollant : 16 vignettes par page A4, chacune
    portant le n° d'inscription et le NOM Prénom du candidat à côté de son QR.
    Seul le QR est découpé et collé sur la copie ; le nom reste sur la planche
    et sert à savoir à qui remettre quelle étiquette. Le QR collé est lu
    automatiquement au scan.

  Identification instantanée et infaillible, sans rien écrire. (Nécessite
  d'importer la liste des étudiants avant de générer fiches ou étiquettes.)

Dans les trois modes, l'OCR du nom reste un secours automatique, et une copie
non identifiée s'affecte en deux clics dans l'écran de vérification.

### Concours : des centaines de copies, identifiées par étiquette QR

Créez le concours depuis le menu **Concours → Nouveau concours** : pas de
classe à choisir ni de case à cocher, chaque concours a sa propre liste de
candidats. Deux modes d'identification :

- **QR code — étiquette autocollante** (par défaut) : importez la liste des
  candidats (Excel : Nom, Prénom, N° d'inscription), imprimez la planche
  d'étiquettes sur papier autocollant, **découpez chaque QR le long des
  pointillés** — le n° et le nom imprimés à côté indiquent à qui le remettre —
  et remettez-le au candidat, qui le colle dans l'emplacement réservé de sa
  copie. Au scan, la copie est identifiée et notée sans aucune saisie.
- **Grille de n°** : chaque candidat noircit son numéro d'inscription ; le
  champ « Nombre de chiffres du n° d'inscription » (taille de la grille)
  n'apparaît que pour ce mode.

**Correction des copies** (choisie à la création du concours, modifiable
ensuite dans ses réglages) :

- **immédiate** : chaque lot est corrigé dès son téléversement ;
- **après scan** : les lots téléversés sont mis **en attente** (rien n'est lu
  ni noté). Quand toutes les salles ont été scannées, le bouton
  **« ▶ Lancer la correction »** de la page du concours corrige tous les lots
  en attente d'un coup, avec suivi de l'avancement. Un lot arrivé plus tard
  attend à son tour et se corrige au prochain lancement, sans recorriger
  les autres.

Un numéro lu qui n'est pas dans la liste crée automatiquement son candidat
(nom lu par OCR) ; importer ensuite la liste officielle met les noms à jour
par numéro, les notes suivent.

## Import de fichiers existants

**Questionnaires** (page du quiz → « Importer un questionnaire ») :

- **Excel / CSV** : une ligne par question, colonnes `Question`, `Type`
  (QCM / Manuscrite), `A`, `B`, `C`… (textes des choix), `Réponse` (lettre
  A–J ou أ ب ت…, numéro, ou texte exact du choix), `Barème`, `Hauteur` (mm) —
  voir **`modele_questions.xlsx`** ;
- **Word (.docx), PDF, texte** : questions numérotées, choix précédés d'une
  lettre, bonne réponse marquée d'un astérisque ou donnée sur une ligne
  « Réponse : » ; barème facultatif entre parenthèses ; une question sans
  choix (ou marquée `[manuscrite]`) devient une question manuscrite :

  ```
  1. Quelle est la capitale de la Tunisie ? (2 pts)
  A) Sfax
  B) Tunis *
  C) Sousse
  2. Combien de gouvernorats compte la Tunisie ?
  a. 20
  b. 24
  Réponse : b
  3. Décrivez le climat du Sahel. [manuscrite] (4 pts)
  ```

  Les lignes non reconnues (titre, consignes) sont ignorées ; une question
  incomplète (sans bonne réponse, un seul choix…) est signalée sans bloquer
  les autres. Un PDF doit contenir du texte (PDF exporté, pas un scan).

**Listes de candidats** (page de la classe ou du concours) : Excel, CSV,
Word (tableau ou une ligne par candidat) ou PDF (une ligne par candidat,
`NOM Prénom N°`).

## Installation

### Option 1 — Docker (recommandé)

```bash
docker compose up -d --build
```

C'est tout : l'image installe Python, Tesseract (français + arabe) et toutes
les dépendances. Ouvrez ensuite http://localhost:8000 (ou http://IP-du-serveur:8000).
La base de données est conservée dans `./data/` et les fichiers (fiches PDF,
scans, images de contrôle) dans `./media/` — sauvegardez ces deux dossiers.

Les réglages du serveur (`SECRET_KEY`, `ALLOWED_HOSTS`, `URL_PREFIX`…) se
mettent dans un fichier **`.env`** : `cp .env.example .env`, puis adaptez-le.
Il n'est jamais versionné, donc jamais écrasé par une mise à jour.

### Mise à jour automatique depuis GitHub

La VM vérifie toutes les 5 minutes s'il y a de nouveaux commits sur GitHub
et, si oui, sauvegarde la base (`data/backups/`, 15 dernières), récupère le
code, reconstruit et redémarre le conteneur (`deploy/update.sh`). Comme c'est
la VM qui interroge GitHub, elle n'a pas besoin d'être joignable depuis
Internet. `.env`, `data/` et `media/` ne sont jamais touchés.

Installation, une seule fois, sur la VM :

```bash
# 1. Clé de lecture seule pour le dépôt (dépôt privé)
sudo ssh-keygen -t ed25519 -N "" -f /root/.ssh/quizscan_deploy
sudo cat /root/.ssh/quizscan_deploy.pub
#    -> GitHub : dépôt > Settings > Deploy keys > Add deploy key
#       (coller la clé, NE PAS cocher « Allow write access »)
sudo tee -a /root/.ssh/config >/dev/null <<'CFG'
Host github-quizscan
    HostName github.com
    User git
    IdentityFile /root/.ssh/quizscan_deploy
CFG

# 2. Récupérer uniquement le dossier quizscan/ du dépôt, sur la branche suivie
sudo git clone --filter=blob:none --sparse -b <branche> \
     git@github-quizscan:<compte>/<depot>.git /opt/apps/dtech/quizscan-git
cd /opt/apps/dtech/quizscan-git && sudo git sparse-checkout set quizscan
cd quizscan

# 3. Reprendre les données et réglages de l'installation actuelle
sudo docker compose -f /opt/apps/dtech/quizscan/docker-compose.yml down
sudo cp -a /opt/apps/dtech/quizscan/data /opt/apps/dtech/quizscan/media .
sudo cp .env.example .env && sudo nano .env      # reprendre vos valeurs
sudo docker compose up -d --build

# 4. Activer la vérification automatique
sudo cp deploy/quizscan-update.service deploy/quizscan-update.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now quizscan-update.timer
```

Ensuite, chaque `git push` sur la branche suivie est en ligne en moins de
5 minutes. Commandes utiles :

```bash
sudo tail -f /var/log/quizscan-update.log          # journal des mises à jour
sudo systemctl start quizscan-update.service        # vérifier tout de suite
sudo ./deploy/update.sh --force                     # reconstruire même sans changement
systemctl list-timers quizscan-update.timer         # prochaine vérification
```

Revenir en arrière : `git reset --hard <commit>` puis
`docker compose up -d --build` (et, si besoin, restaurer une copie de
`data/backups/` dans `data/db.sqlite3`, conteneur arrêté). Tant que GitHub
n'a pas de commit plus récent, la VM reste sur cette version ; sinon, elle
reprend la dernière au prochain passage du timer.

### Derrière Caddy, sur un domaine ou sous-domaine dédié

Le cas le plus courant : l'application a son propre nom, par exemple
`https://quiz.mon-etablissement.tn`. **`URL_PREFIX` doit rester vide** — c'est
l'erreur la plus fréquente, un préfixe laissé par erreur casse tous les liens.

`.env` :

```ini
SECRET_KEY=<sortie de : python3 -c "import secrets; print(secrets.token_urlsafe(50))">
ALLOWED_HOSTS=quiz.mon-etablissement.tn,192.168.1.10,localhost
CSRF_TRUSTED_ORIGINS=https://quiz.mon-etablissement.tn
URL_PREFIX=
QUIZSCAN_PORT=5011
HTTPS=1
```

Mettez dans `ALLOWED_HOSTS` **tous** les noms par lesquels on accède, adresse
IP comprise : un hôte absent de la liste renvoie une erreur 400.

`Caddyfile` (modèle prêt à copier : `deploy/Caddyfile.exemple`) :

```
quiz.mon-etablissement.tn {
    reverse_proxy 127.0.0.1:5011 {
        header_up X-Real-IP {remote_host}
    }
}
```

puis `sudo systemctl reload caddy`. Laissez `QUIZSCAN_BIND=127.0.0.1` : seul
Caddy a besoin de joindre l'application.

**Certificat TLS.** Si le nom résout publiquement vers une IP joignable depuis
Internet, Caddy obtient seul un certificat Let's Encrypt : rien à faire. Si le
nom ne résout qu'en interne vers une **IP privée** (192.168.x.x), Let's Encrypt
ne peut pas le valider ; ajoutez `tls internal` dans le bloc — Caddy signe alors
avec son autorité locale. Les navigateurs afficheront un avertissement tant que
le certificat racine de Caddy n'est pas installé sur les postes ; il se récupère
sur le serveur :
`/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt`.

**Accès en http:// sans TLS.** Déconseillé, mais si vous joignez l'application
par `http://192.168.1.10` sans certificat, posez `HTTPS=0` : sinon le cookie de
session est marqué « Secure », le navigateur refuse de le renvoyer et la page
de connexion boucle sans message d'erreur. À réserver à un réseau de confiance.
Notez que `docker-compose.yml` publie le port sur `127.0.0.1` uniquement : un
accès direct depuis un autre poste passe forcément par le reverse proxy.

### Accès direct par l'adresse IP, sans reverse proxy

Le plus simple pour un serveur de salle : l'application répond directement sur
`http://192.168.1.10/`, sans Caddy ni certificat.

`.env` :

```ini
SECRET_KEY=<python3 -c "import secrets; print(secrets.token_urlsafe(50))">
ALLOWED_HOSTS=192.168.1.10,localhost
CSRF_TRUSTED_ORIGINS=http://192.168.1.10
URL_PREFIX=
QUIZSCAN_BIND=0.0.0.0
QUIZSCAN_PORT=80
HTTPS=0
```

Les deux lignes qui changent par rapport au mode reverse proxy :

- `QUIZSCAN_BIND=0.0.0.0` — par défaut le conteneur ne publie son port que sur
  `127.0.0.1`, donc injoignable depuis un autre poste ;
- `HTTPS=0` — sans cela les cookies sont marqués « Secure », le navigateur
  refuse de les renvoyer en `http://` et la connexion boucle sans message.

Puis `docker compose up -d --build`, et pensez au pare-feu :
`sudo ufw allow 80/tcp`.

**À savoir :** en `http://`, les mots de passe, les copies scannées et les
notes circulent en clair sur le réseau. C'est acceptable sur le réseau interne
d'un établissement, pas au-delà. Pour chiffrer sans nom de domaine public,
gardez Caddy et demandez-lui un certificat pour l'adresse IP :

```
https://192.168.1.10 {
    tls internal
    reverse_proxy 127.0.0.1:5011
}
```

avec `QUIZSCAN_BIND=127.0.0.1`, `QUIZSCAN_PORT=5011` et `HTTPS=1`. Les postes
afficheront un avertissement tant que le certificat racine de Caddy n'y est pas
installé.

### Vérifier une installation

```bash
docker compose exec quizscan python manage.py verifier_deploiement
```

La commande contrôle les réglages (SECRET_KEY, ALLOWED_HOSTS, URL_PREFIX,
cookies), la présence de Tesseract et de ses paquets de langue, l'état de la
base et des migrations, les droits d'écriture sur `data/` et `media/` et la
collecte des fichiers statiques. Chaque point est marqué OK, ATTENTION ou
ERREUR, avec la commande à lancer pour corriger.

### Derrière un reverse proxy Caddy, en sous-chemin

Pour exposer l'application sous `https://mon-domaine.tn/quizscan` (schéma
`uri strip_prefix`), renseignez le fichier `.env` :

```ini
SECRET_KEY=une-longue-chaine-aleatoire
ALLOWED_HOSTS=mon-domaine.tn,192.168.x.x,localhost
CSRF_TRUSTED_ORIGINS=https://mon-domaine.tn
URL_PREFIX=/quizscan
QUIZSCAN_PORT=5011
HTTPS=1
```

puis ajoutez dans le Caddyfile, avant le `handle` final « attrape-tout » :

```
    handle /quizscan* {
        uri strip_prefix /quizscan
        reverse_proxy localhost:5011 {
            header_up X-Real-IP {remote_host}
        }
    }
```

et rechargez Caddy (`sudo systemctl reload caddy`). `URL_PREFIX` fait que
toutes les URLs générées (liens, redirections, fichiers) portent le préfixe ;
en production (`DEBUG=0`) l'application sert elle-même ses fichiers statiques
et les documents (fiches, scans). Pour un accès à la racine d'un domaine,
laissez simplement `URL_PREFIX` vide.

### Option 2 — Installation directe (Linux)

Prérequis : Python 3.10+, Tesseract OCR.

```bash
# Debian / Ubuntu
sudo apt install python3-pip python3-venv tesseract-ocr tesseract-ocr-fra tesseract-ocr-ara

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver 0.0.0.0:8000
```

Ouvrez ensuite http://localhost:8000

### Option 3 — Windows (poste de l'enseignant)

1. Installez Python 3.10+ depuis https://www.python.org/downloads/
   (cochez « Add Python to PATH ») ;
2. Installez Tesseract depuis https://github.com/UB-Mannheim/tesseract/wiki
   en sélectionnant les langues **French** et **Arabic** ; si nécessaire,
   indiquez le chemin dans `grader/omr.py` :
   `pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"` ;
3. Dans le dossier du projet :
   ```bat
   pip install -r requirements.txt
   python manage.py migrate
   python manage.py runserver
   ```
4. Ouvrez http://localhost:8000

## Questionnaires en arabe

Chaque quiz a une **langue** (français ou arabe), choisie à sa création :

- la fiche PDF est générée en arabe, de droite à gauche : اللقب / الاسم,
  consignes en arabe, lettres de choix أ ب ت ث (ordre alphabétique, أ à droite de chaque ligne),
  numéros (questions, pages, barème, grille de n°) en chiffres occidentaux
  (1, 2, 3…) — police Noto Naskh Arabic embarquée
  dans `grader/fonts/` (titres et textes de questions en arabe acceptés) ;
- le nom de l'étudiant est lu par l'OCR arabe de Tesseract (paquet
  `tesseract-ocr-ara` requis) avec normalisation (hamza, ta marbouta,
  diacritiques) avant le rapprochement avec la liste de classe ;
- le corrigé rapide accepte les lettres arabes (`أ ب ت ث`) comme latines (`ABCD`).

La liste des étudiants peut être importée avec des noms en arabe : c'est elle
qui sert de référence pour l'identification.

## Tests automatiques

`python test_e2e.py` (français) et `python test_e2e_ar.py` (arabe) génèrent une
fiche, simulent une copie remplie et scannée (rotation + bruit), la traitent et
vérifient l'identification de l'étudiant et la note calculée. Autres tests :

| Script | Vérifie |
|---|---|
| `test_e2e_qr.py` | fiches nominatives QR |
| `test_e2e_concours.py` | mode concours (QR anonyme, grille de n°, import des noms) |
| `test_e2e_labels.py` | étiquette QR d'un candidat de concours collée sur la copie |
| `test_e2e_import.py` | import de questionnaires et de candidats (Excel, Word, PDF, CSV), banque de questions |
| `test_e2e_forms.py` | création d'un concours (sans classe ni case « mode concours ») et d'un quiz, nombre de choix par question jusqu'à la lecture de la copie |
| `test_e2e_deferred.py` | concours « correction après scan » : lots en attente, puis « Lancer la correction » |
| `test_e2e_web.py` | par l'interface : étiquettes QR d'une classe, correction en arrière-plan, secours OCR, corrigé modifié après l'épreuve, alerte de fiche périmée, droits d'accès |
| `test_e2e_comptes.py` | création d'un compte, bascule de rôle, désactivation, réinitialisation d'un mot de passe, changement par l'enseignant |
| `test_e2e_design.py` | une seule feuille de style pour toutes les pages, aucun style redéclaré en ligne, aucun jeton défini deux fois |
| `test_e2e_corrige.py` | bonne réponse facultative, corrigé scanné et conservé, correction bloquée tant qu'il manque une réponse |
| `test_e2e_robustesse.py` | pannes d'OCR (Tesseract absent, paquet de langue manquant), fiches de plus de 12 pages, QR collés de travers dans leur cadre, contenu de la planche d'étiquettes, identifiants d'URL bricolés |

Pour tout lancer : `for t in test_e2e*.py; do python $t || break; done`

Les tests qui lisent un **nom manuscrit** ont besoin de Tesseract et de ses
paquets de langue (`tesseract-ocr-fra`, `tesseract-ocr-ara`) : `test_e2e_ar.py`,
`test_e2e_web.py` et la variante « grille » de `test_e2e_concours.py`. Les
autres, dont `test_e2e_robustesse.py` qui simule l'OCR, tournent sans Tesseract.

Les tests utilisent leur **propre base** (`data/test_db.sqlite3`) et leur propre
dossier de documents (`media_test/`) : la base de production `data/db.sqlite3`
ne contient jamais de comptes ni de copies de test. Au premier démarrage elle
est vide — créez l'administrateur avec `createsuperuser`.

## Conseils pour une lecture fiable

- Demandez aux étudiants de **noircir complètement** les cases (pas de croix
  légères) et d'écrire le nom en **MAJUSCULES**.
- Scannez à plat, 200 dpi minimum, sans recadrage automatique du scanner.
- Si les questions changent après impression, **régénérez la fiche** avant de
  scanner (la disposition est figée dans la fiche PDF).
- L'OCR d'un nom manuscrit n'est jamais parfait : les copies non identifiées
  sont signalées et s'affectent en deux clics dans l'écran de vérification.

## L'interface

L'application présente une **barre latérale** (Tableau de bord, Classes,
Concours, et Administration pour l'administrateur), une barre du haut avec le
compte connecté, et des panneaux arrondis sur un fond dégradé bleu → violet.
La feuille de style est écrite dans `templates/grader/base.html` : l'application
est interne et à faible trafic, cela évite une requête de plus et garde les
pages lisibles même si les fichiers statiques ne sont pas collectés.

Le **tableau de bord** donne les quatre chiffres utiles — nombre d'épreuves,
taux de copies lues sans intervention, copies scannées (avec les 7 derniers
jours) et ce qui reste à traiter — puis les épreuves et lots récents.

Le panneau s'étend jusqu'à 1760 px et **épouse son contenu** en hauteur : sur
un écran large il n'y a ni bandes vides sur les côtés, ni grande zone blanche
sous le contenu. Au-delà de 1700 px, la typographie et la barre latérale
montent d'un cran — un corps de 15 px est peu lisible sur un 2560 px.

Sous 980 px, la barre latérale devient une barre d'icônes horizontale et les
panneaux se mettent en colonne : les pages restent utilisables sur tablette et
téléphone.

**Un seul système de style.** Tout — espace enseignant, pages de connexion et
administration — vient de `grader/static/grader/quizscan.css` : un seul jeu de
jetons (couleurs, rayons, ombres, mesures), une seule barre latérale, un seul
jeu de composants (carte, tuile, bouton, champ, pastille, tableau). Les trois
contextes avaient auparavant trois feuilles séparées qui redéfinissaient les
mêmes couleurs sous des noms différents ; une règle oubliée dans l'une écrasait
l'autre. `test_e2e_design.py` monte la garde : il vérifie que chaque page
charge cette feuille et qu'aucune ne redéclare le style en ligne.

L'**interface d'administration** (`/admin/`) s'ouvre dans le **même cadre** que
le reste : même barre latérale, même barre du haut, même panneau. La barre
latérale y liste les rubriques de l'application *et* les tables
administrables ; celle de Django est désactivée
(`admin.site.enable_nav_sidebar = False`) pour ne pas avoir deux colonnes de
navigation.

`templates/admin/base.html` en fournit l'ossature : elle reprend tous les
blocs et identifiants attendus par Django (`#container`, `#main`, `#content`…)
pour que listes, formulaires et fenêtres de sélection continuent de
fonctionner. Les fenêtres de sélection (loupe d'une clé étrangère) s'affichent
sans la barre latérale, comme il se doit.

Django 5 expose ses couleurs en variables CSS (`--primary`, `--body-bg`…) : la
feuille unique les fait **pointer sur les jetons de QuizScan** plutôt que de
lutter contre sa feuille de style — ce qui reste valable quand Django fait
évoluer ses règles internes. Le thème sombre est couvert aussi.

## Le nombre de cases d'une question

Deux façons de définir une question QCM, et le formulaire n'affiche que ce qui
s'applique :

- **Vous saisissez le texte des choix** (questionnaire complet) : le nombre de
  cases suit le nombre de lignes tapées. Le champ « Nombre de cases à
  imprimer » disparaît et laisse place à un rappel — *« 5 cases (A, B, C, D,
  E) — d'après les choix saisis »*.
- **Vous ne saisissez aucun choix** (fiche de réponses seule, le sujet est
  distribué à part) : la fiche ne porte qu'une rangée de cases, et le champ
  « Nombre de cases à imprimer » est le seul endroit où en fixer le nombre
  (2 à 10). C'est ce qui permet d'avoir une question à 5 choix et une question
  vrai/faux à 2 cases dans la même épreuve.

De même, « Hauteur zone réponse » n'apparaît que pour une question manuscrite,
et les choix disparaissent pour celle-ci.

## Le corrigé : saisi, ou scanné

La **bonne réponse n'est pas obligatoire** à la création d'une question : on
peut monter tout le questionnaire d'abord et renseigner le corrigé ensuite,
de deux façons — au choix, et les deux se complètent :

1. **À la main**, dans la colonne « Bonne rép. » du tableau des questions.
   Une question sans corrigé y affiche « — à définir — » sur fond orange.
2. **En scannant le corrigé** : imprimez la fiche d'épreuve, noircissez les
   bonnes réponses comme le ferait un étudiant, scannez-la et déposez-la dans
   « Fiche du corrigé scannée » sur la page du quiz. Les cases lues
   renseignent le corrigé d'un coup.

La fiche scannée est **conservée avec l'épreuve** — image d'origine et image
de contrôle colorée — et reste consultable dans l'historique du quiz : elle
fait foi sur l'origine du barème en cas de contestation.

Si une case est laissée vide ou si plusieurs cases sont noircies, la question
est signalée comme illisible et reste « à définir » : le reste du corrigé est
quand même appliqué, il ne reste qu'à compléter les questions concernées.

**La correction des copies ne démarre pas tant qu'une bonne réponse manque.**
Les copies téléversées sont reçues et mises en attente ; le bouton « Lancer la
correction » reste désactivé avec la liste des questions à compléter. Sans ce
verrou, les questions sans corrigé seraient comptées fausses pour tout le
monde. Dès le corrigé complet, la correction part et les copies déjà scannées
sont recalculées.

## Se connecter

Deux pages de connexion, volontairement distinctes :

| Page | URL | Pour qui |
|---|---|---|
| **Espace enseignant** (vert) | `/comptes/login/` | les enseignants : leurs classes, leurs épreuves, leurs copies |
| **Administration** (bleu) | `/comptes/administration/` | l'administrateur de l'établissement : comptes et vue d'ensemble |

Chacune renvoie vers l'autre, pour qu'un utilisateur qui se trompe de porte
retrouve la bonne en un clic. La page d'administration refuse les comptes
enseignants avec un message explicite, et `/admin/login/` (la connexion de
l'interface d'administration Django) affiche exactement le même écran.

Aucune réinitialisation de mot de passe en libre-service : les comptes sont
créés et réinitialisés par l'administrateur depuis `/admin/`.

### Gérer les comptes

L'écran **Administration → Utilisateurs** est réorganisé autour de ce que fait
réellement un administrateur d'établissement :

- la liste montre l'identifiant, le nom, le rôle (*Enseignant* ou
  *Administrateur*), l'état actif, et un lien **Modifier** le mot de passe ;
- la fiche d'un compte est découpée en *Compte*, *Identité*, *Accès* ; les
  groupes et permissions unitaires de Django sont repliés, inutiles dans la
  plupart des établissements ;
- **« Rôle »** remplace les deux cases « Statut équipe » et « Statut
  super-utilisateur » : dans QuizScan elles vont toujours ensemble, un compte
  ouvre l'administration et voit tout, ou ni l'un ni l'autre ;
- décocher **« Actif »** ferme l'accès sans supprimer le compte : les classes,
  épreuves et copies de l'enseignant sont conservées ;
- le champ mot de passe n'affiche plus l'empreinte technique
  (`pbkdf2_sha256 itérations: … salage: …`) mais un bouton **Modifier le mot
  de passe**. Un mot de passe ne se relit pas, il ne peut qu'être remplacé.

Chaque enseignant peut changer **son** mot de passe depuis son espace, par
*Mon mot de passe* en bas de la barre latérale. Cet écran est bien celui de
l'application : un enseignant n'atterrit jamais sur une page d'administration.

### Créer les comptes

Le premier administrateur se crée en ligne de commande :

```bash
python manage.py createsuperuser
```

Il crée ensuite les comptes enseignants depuis `/admin/` → *Utilisateurs*.
Pour remettre un mot de passe à zéro sans passer par l'interface :

```bash
python manage.py changepassword nom_du_compte
```

**Pour essayer l'application**, deux comptes de démonstration (un enseignant,
un administrateur) s'installent d'un coup :

```bash
python manage.py comptes_demo
```

Le mot de passe est tiré au hasard et écrit dans `data/comptes-demo.txt` — il
n'est pas affiché, pour ne pas rester dans l'historique du terminal. `data/`
n'est pas versionné. À supprimer avant la mise en service :

```bash
python manage.py comptes_demo --supprimer
```

## Mise en production (aperçu)

- `DEBUG=0` (déjà posé par `docker-compose.yml`). Dans ce mode, l'application
  **refuse de démarrer** sans `SECRET_KEY` ni `ALLOWED_HOSTS` propres dans
  `.env` : la clé de développement du dépôt est publique, elle signe les
  sessions et les jetons CSRF. Le message d'erreur indique quoi renseigner ;
- toujours en `DEBUG=0`, les cookies de session et CSRF sont marqués
  `Secure` + `HttpOnly` (HTTPS assuré par le reverse proxy), avec
  `X-Frame-Options: DENY` et `nosniff` ;
- servir avec gunicorn/nginx (l'image Docker le fait) ; toutes les pages et
  tous les documents exigent une connexion, et chaque enseignant n'accède
  qu'à **ses propres** fiches PDF, scans, images de contrôle et recadrages
  (l'administrateur voit tout) ;
- **PostgreSQL** recommandé au-delà d'un usage mono-poste (SQLite par
  défaut) : il suffit de renseigner `POSTGRES_DB`, `POSTGRES_USER`,
  `POSTGRES_PASSWORD` (et au besoin `POSTGRES_HOST` / `POSTGRES_PORT`) dans
  `.env`, le pilote est déjà installé dans l'image.
#   Q u i z s c a n 
 
 