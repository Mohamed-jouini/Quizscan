from django.urls import path

from . import views

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
    path("quiz/<int:pk>/resultats/", views.quiz_results, name="quiz_results"),
    path("quiz/<int:pk>/resultats.xlsx", views.quiz_results_xlsx, name="quiz_results_xlsx"),
    path("quiz/<int:pk>/bulletins.pdf", views.quiz_bulletins_pdf, name="quiz_bulletins_pdf"),
    path("lots/<int:pk>/", views.batch_detail, name="batch_detail"),
    path("copies/<int:pk>/", views.sheet_review, name="sheet_review"),
]
