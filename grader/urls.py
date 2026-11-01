from django.urls import path

from . import views, views_inscriptions as vi, views_public as vp

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("classes/", views.class_list, name="class_list"),
    path("concours/", views.concours_list, name="concours_list"),
    path("classes/<int:pk>/", views.class_detail, name="class_detail"),
    path("classes/<int:pk>/etiquettes.pdf", views.class_labels_pdf,
         name="class_labels_pdf"),
    path("candidats/<int:pk>/qr.png", views.student_qr, name="student_qr"),
    path("quiz/nouveau/", views.quiz_create, name="quiz_create"),
    path("quiz/<int:pk>/", views.quiz_detail, name="quiz_detail"),
    path("quiz/<int:pk>/upload/", views.quiz_upload, name="quiz_upload"),
    path("quiz/<int:pk>/corrige/", views.quiz_answer_key, name="quiz_answer_key"),
    path("resultats/", views.resultats_list, name="resultats_list"),
    path("quiz/<int:pk>/resultats/", views.quiz_results, name="quiz_results"),
    path("quiz/<int:pk>/note-admission/", views.quiz_note_admission,
         name="quiz_note_admission"),
    path("quiz/<int:pk>/resultats.xlsx", views.quiz_results_xlsx, name="quiz_results_xlsx"),
    path("quiz/<int:pk>/bulletins.pdf", views.quiz_bulletins_pdf, name="quiz_bulletins_pdf"),
    path("quiz/<int:pk>/admis.pdf", views.quiz_admis_pdf, name="quiz_admis_pdf"),
    path("quiz/<int:pk>/concours/", views.quiz_parcours, name="quiz_parcours"),
    path("quiz/<int:pk>/emargement.pdf", views.quiz_emargement_pdf,
         name="quiz_emargement_pdf"),
    path("lots/<int:pk>/", views.batch_detail, name="batch_detail"),
    path("lots/<int:pk>/supprimer/", views.batch_delete, name="batch_delete"),
    path("copies/<int:pk>/", views.sheet_review, name="sheet_review"),
    # registre des inscriptions, communiqués, publication (personnel)
    path("inscriptions/", vi.inscriptions, name="inscriptions"),
    path("inscriptions/<int:pk>/qr.png", vi.inscrit_qr, name="inscrit_qr"),
    path("inscriptions/cartes.pdf", vi.inscriptions_cartes, name="inscriptions_cartes"),
    path("communiques/", vi.communiques, name="communiques"),
    path("quiz/<int:pk>/publication/", vi.quiz_publication, name="quiz_publication"),
    # espace public (sans compte)
    path("public/", vp.accueil, name="public_accueil"),
    path("public/resultats/", vp.resultat, name="public_resultat"),
    path("public/epreuves/<int:pk>/", vp.epreuve, name="public_epreuve"),
    path("public/communiques/", vp.communiques, name="public_communiques"),
    path("public/communiques/<int:pk>/", vp.communique, name="public_communique"),
    path("public/communiques/piece/<int:pk>/<str:nom>", vp.piece_jointe, name="public_piece"),
]
