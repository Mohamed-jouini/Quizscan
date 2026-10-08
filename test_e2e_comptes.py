"""Test de la gestion des comptes et des mots de passe.

Couvre ce que fait réellement l'administrateur d'établissement :
  1. créer un compte enseignant ;
  2. le passer administrateur, puis le remettre enseignant, puis le
     désactiver — par le choix « Rôle », qui pilote is_staff et is_superuser ;
  3. réinitialiser le mot de passe d'un enseignant qui l'a oublié ;
  4. et, côté enseignant, changer soi-même son mot de passe depuis son
     espace — sur un écran de l'application, PAS d'administration.
"""
import os
import re
import sys

# Les tests affichent des caractères accentués et des symboles : on force la
# sortie en UTF-8 pour ne pas échouer sur une console Windows en cp1252.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import logging

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "quizscan.settings")
os.environ["QUIZSCAN_TEST"] = "1"   # base de test séparée (data/test_db.sqlite3)
django.setup()
from django.core.management import call_command  # noqa: E402
call_command("migrate", verbosity=0)

from django.contrib.auth import get_user_model
from django.test import Client

User = get_user_model()
ANCIEN = "Mdp-Depart-2026!"
NOUVEAU = "Mdp-Change-2026!"


def main():
    logging.disable(logging.ERROR)
    User.objects.filter(username__startswith="cpt_").delete()
    admin = User.objects.create_superuser("cpt_admin", email="",
                                          password=ANCIEN)
    c = Client()
    c.force_login(admin)

    # ---- 1) création d'un compte enseignant --------------------------------
    r = c.post("/admin/auth/user/add/", {
        "username": "cpt_prof", "usable_password": "true",
        "password1": ANCIEN, "password2": ANCIEN, "_save": "1"})
    assert r.status_code == 302, r.status_code
    prof = User.objects.get(username="cpt_prof")
    assert prof.check_password(ANCIEN)
    assert not prof.is_staff and not prof.is_superuser, "créé administrateur !"
    print("  compte enseignant créé, mot de passe posé OK")

    def enregistre(**champs):
        base = {"username": "cpt_prof", "first_name": "Ahmed",
                "last_name": "BEN SALAH", "email": "a@b.tn",
                "groups": [], "user_permissions": [], "_save": "1"}
        base.update(champs)
        reponse = c.post(f"/admin/auth/user/{prof.pk}/change/", base)
        prof.refresh_from_db()
        return reponse

    # ---- 2) le rôle pilote is_staff et is_superuser ------------------------
    enregistre(role="admin", is_active="on")
    assert prof.is_staff and prof.is_superuser, "rôle administrateur non appliqué"
    assert prof.first_name == "Ahmed", "l'identité ne doit pas être perdue"
    enregistre(role="enseignant", is_active="on")
    assert not prof.is_staff and not prof.is_superuser, "rôle enseignant non appliqué"
    print("  rôle Administrateur / Enseignant appliqué dans les deux sens OK")

    # case « Actif » décochée : le compte est fermé, les données conservées
    enregistre(role="enseignant")
    assert not prof.is_active
    assert not Client().login(username="cpt_prof", password=ANCIEN), \
        "un compte désactivé ne doit pas pouvoir se connecter"
    enregistre(role="enseignant", is_active="on")
    assert prof.is_active
    print("  compte désactivé puis réactivé, connexion refusée entre-temps OK")

    # ---- 3) l'administrateur réinitialise un mot de passe oublié ----------
    # Le bouton « Modifier le mot de passe » doit mener au bon écran depuis la
    # liste ET depuis la fiche du compte. Un lien relatif (« 3/password/ »)
    # marchait depuis la liste mais menait, depuis la fiche, à
    # « …/3/change/3/password/ » : « l'utilisateur n'existe pas ».
    from urllib.parse import urljoin
    attendu = f"/admin/auth/user/{prof.pk}/password/"
    for page_url, classe in (("/admin/auth/user/", "qs-pw-link"),
                             (f"/admin/auth/user/{prof.pk}/change/", "qs-pw-btn")):
        html = c.get(page_url).content.decode()
        liens = [urljoin(page_url, h) for h in re.findall(
            rf'<a class="[^"]*\b{classe}\b[^"]*" href="([^"]+)"', html)]
        assert attendu in liens, f"{page_url} : bouton mot de passe -> {liens}"
        assert c.get(attendu).status_code == 200
    print("  bouton « Modifier le mot de passe » juste depuis la liste et la fiche OK")

    r = c.post(f"/admin/auth/user/{prof.pk}/password/",
               {"password1": NOUVEAU, "password2": NOUVEAU})
    assert r.status_code == 302, r.status_code
    prof.refresh_from_db()
    assert prof.check_password(NOUVEAU), "mot de passe non réinitialisé"
    print("  mot de passe réinitialisé par l'administrateur OK")

    # ---- 4) l'enseignant change son mot de passe depuis SON espace --------
    cp = Client()
    assert cp.login(username="cpt_prof", password=NOUVEAU)
    page = cp.get("/comptes/password_change/")
    corps = page.content.decode()
    assert page.status_code == 200
    # l'écran doit être celui de l'application, pas celui de l'administration
    assert "Tableau de bord" in corps, "l'enseignant doit rester dans son espace"
    assert "Administration" not in corps, \
        "un enseignant ne doit pas voir la navigation d'administration"
    r = cp.post("/comptes/password_change/", {
        "old_password": NOUVEAU,
        "new_password1": ANCIEN, "new_password2": ANCIEN})
    assert r.status_code == 302, r.status_code
    prof.refresh_from_db()
    assert prof.check_password(ANCIEN)
    assert "modifié" in cp.get(r["Location"]).content.decode()
    print("  enseignant : mot de passe changé depuis son espace OK")

    # ---- 5) l'administrateur garde son propre écran ------------------------
    page = c.get("/admin/password_change/")
    assert page.status_code == 200
    assert "Modifier mon mot de passe" in page.content.decode()
    r = c.post("/admin/password_change/", {
        "old_password": ANCIEN,
        "new_password1": NOUVEAU, "new_password2": NOUVEAU})
    assert r.status_code == 302, r.status_code
    admin.refresh_from_db()
    assert admin.check_password(NOUVEAU)
    print("  administrateur : mot de passe changé sur son propre écran OK")

    # ---- 6) un enseignant n'entre pas dans l'administration ---------------
    cp = Client()
    cp.login(username="cpt_prof", password=ANCIEN)
    r = cp.get("/admin/auth/user/", follow=True)
    assert "/admin/login/" in r.redirect_chain[-1][0], r.redirect_chain
    print("  enseignant refusé à l'entrée de l'administration OK")

    print("\n✅ TEST DE LA GESTION DES COMPTES RÉUSSI")


if __name__ == "__main__":
    main()
