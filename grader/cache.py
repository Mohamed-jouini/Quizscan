"""Cache en fichiers commun aux processus gunicorn (settings.CACHES), sans relire tout son
dossier à chaque écriture.

Le FileBasedCache de Django, avant chaque écriture, liste le dossier entier pour savoir s'il
faut faire de la place (MAX_ENTRIES). Or le frein aux recherches publiques garde un fichier
par adresse qui a cherché un résultat : le jour des résultats, des milliers. Mesure du
23 novembre 2026, 10 000 candidats : à 10 000 fichiers, une recherche passait de 67 à 467 ms
sur un poste Windows (moins sur le serveur Linux, mais toujours en proportion du nombre de
fichiers). Et Django n'efface une entrée périmée qu'en la relisant : une adresse venue une
fois laissait son fichier jusqu'à ce que le dossier déborde.

Ici le ménage se fait au plus une fois par minute et par processus : les entrées périmées
d'abord, puis, s'il en reste trop, celui de Django (MAX_ENTRIES, entrées tirées au hasard).
"""
import threading
import time

from django.core.cache.backends.filebased import FileBasedCache

INTERVALLE_MENAGE_S = 60

# Django crée une instance du cache par fil d'exécution : l'heure du prochain ménage est
# gardée pour tout le processus, par dossier.
_prochain_menage = {}
_verrou = threading.Lock()


class CacheFichiers(FileBasedCache):

    def _cull(self):
        maintenant = time.monotonic()
        with _verrou:
            if maintenant < _prochain_menage.get(self._dir, 0.0):
                return
            _prochain_menage[self._dir] = maintenant + INTERVALLE_MENAGE_S
        self.effacer_perimees()
        super()._cull()

    def effacer_perimees(self):
        """Efface les fichiers des entrées périmées ; retourne combien."""
        effacees = 0
        for chemin in self._list_cache_files():
            try:
                with open(chemin, "rb") as fichier:
                    effacees += self._is_expired(fichier)    # l'efface s'il est périmé
            except Exception:  # noqa: BLE001 — le ménage ne doit jamais faire échouer une requête
                pass        # effacé ou remplacé entre-temps par un autre processus, ou illisible
        return effacees
