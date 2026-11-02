"""Réception des gros envois : 10 000 fichiers scannés en une fois.

Le gestionnaire de Django garde un fichier temporaire OUVERT par fichier reçu :
l'envoi plantait en « Too many open files » (erreur 500, rien de reçu) dès la
limite du système, 1 024 sur bien des serveurs. Ici chaque fichier est refermé
aussitôt écrit sur le disque, et rouvert seulement pour être lu : un envoi tient
quel que soit le nombre de fichiers et le réglage du serveur.

Les fichiers temporaires sont effacés à la fin de la requête, quelle qu'en soit
l'issue (EffacerEnvois) : un envoi coupé en route ou refusé, même sans compte
puisque le contrôle CSRF lit le corps de la requête, ne remplit pas le disque.
"""
import os
import tempfile

from django.conf import settings
from django.core.files.uploadedfile import UploadedFile
from django.core.files.uploadhandler import FileUploadHandler


def _supprimer(chemin):
    try:
        os.remove(chemin)
    except OSError:
        pass


class FichierRecu(UploadedFile):
    """Fichier reçu, sur le disque et fermé tant qu'on ne le lit pas. close()
    libère seulement le descripteur : la lecture peut reprendre (au début)."""

    def __init__(self, chemin, name, content_type, size, charset, content_type_extra):
        self._chemin = chemin
        self._ouvert = None
        super().__init__(None, name, content_type, size, charset, content_type_extra)

    @property
    def file(self):
        if self._ouvert is None:
            self._ouvert = open(self._chemin, "rb")
        return self._ouvert

    @file.setter
    def file(self, valeur):
        pass                        # File.__init__ y pose None : ouverture à la demande

    @property
    def closed(self):
        return self._ouvert is None

    def open(self, mode=None):
        self.file.seek(0)
        return self

    def close(self):
        if self._ouvert is not None:
            self._ouvert.close()
            self._ouvert = None

    def effacer(self):
        """Contenu copié ailleurs : la place est rendue tout de suite, sans
        attendre la fin de la requête (un lot occuperait deux fois le disque)."""
        self.close()
        _supprimer(self._chemin)


class EnvoiSurDisque(FileUploadHandler):
    """Remplace TemporaryFileUploadHandler. Le fichier en cours d'écriture
    s'appelle « file » : MultiPartParser le referme si l'envoi s'interrompt."""

    def new_file(self, *args, **kwargs):
        super().new_file(*args, **kwargs)
        descripteur, self.chemin = tempfile.mkstemp(suffix=".upload",
                                                    dir=settings.FILE_UPLOAD_TEMP_DIR)
        self.file = os.fdopen(descripteur, "wb")
        self.request.__dict__.setdefault("_envois", []).append(self)
        self.request.__dict__.setdefault("_chemins_envoyes", []).append(self.chemin)

    def receive_data_chunk(self, raw_data, start):
        self.file.write(raw_data)

    def file_complete(self, file_size):
        self.file.close()
        recu = FichierRecu(self.chemin, self.file_name, self.content_type, file_size,
                           self.charset, self.content_type_extra)
        self.request.__dict__["_envois"].append(recu)
        return recu

    def close(self):
        self.file.close()


class EffacerEnvois:
    """Efface les fichiers temporaires de la requête. Tout au début de la liste
    des middlewares : le contrôle CSRF, qui lit le formulaire, passe avant."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            return self.get_response(request)
        finally:
            for envoi in request.__dict__.get("_envois", ()):
                envoi.close()
            for chemin in request.__dict__.get("_chemins_envoyes", ()):
                _supprimer(chemin)
