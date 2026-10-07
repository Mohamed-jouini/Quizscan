"""Les tests de QuizScan sont des scripts de bout en bout, à la racine du
projet, car ils simulent la chaîne complète : génération du PDF, impression,
remplissage, scan (rotation + bruit), lecture et notation.

Lancez-les un par un (base et documents de test séparés de la production) :

    python test_e2e.py            # chaîne complète, français
    python test_e2e_ar.py         # idem en arabe (RTL)  — requiert tesseract-ocr-ara
    python test_e2e_qr.py         # fiches nominatives à QR code
    python test_e2e_labels.py     # étiquettes autocollantes de concours
    python test_e2e_concours.py   # mode concours (candidats créés au scan)
    python test_e2e_deferred.py   # correction lancée après le scan
    python test_e2e_forms.py      # formulaires et réglages
    python test_e2e_import.py     # imports Excel / Word / PDF / texte
    python test_e2e_web.py        # parcours complet par l'interface web
    python test_e2e_comptes.py    # comptes, rôles et mots de passe
    python test_e2e_corrige.py    # corrigé facultatif puis scanné
    python test_e2e_design.py     # un seul système de style pour toutes les pages
    python test_e2e_robustesse.py # pannes d'OCR, fiches longues, URL bricolées

Les tests qui lisent un nom manuscrit (test_e2e_ar, test_e2e_web et la
variante « grille » de test_e2e_concours) ont besoin de Tesseract et de ses
paquets de langue, installés par le Dockerfile. test_e2e_robustesse simule
l'OCR et tourne partout.
"""
