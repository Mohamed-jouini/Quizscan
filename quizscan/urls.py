from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path, re_path
from django.views.static import serve as file_serve

from grader.views import AdminLoginView, ConnexionView, protected_media

urlpatterns = [
    path("admin/", admin.site.urls),
    # avant l'include ci-dessous : django.contrib.auth.urls capterait sinon
    # tout le préfixe « comptes/ » et renverrait 404 sur cette page.
    path("comptes/administration/", AdminLoginView.as_view(), name="admin_login"),
    # Les seules pages de compte : connexion (essais limités), déconnexion,
    # changement de mot de passe. Pas de réinitialisation par courriel :
    # aucun envoi n'est configuré, c'est l'administrateur qui la fait.
    path("comptes/login/", ConnexionView.as_view(), name="login"),
    path("comptes/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("comptes/password_change/", auth_views.PasswordChangeView.as_view(),
         name="password_change"),
    path("comptes/password_change/done/", auth_views.PasswordChangeDoneView.as_view(),
         name="password_change_done"),
    path("", include("grader.urls")),
]

# Les documents (fiches PDF, scans des copies, images de contrôle) contiennent
# des données nominatives : chaque enseignant n'accède qu'aux siens.
urlpatterns += [
    re_path(r"^media/(?P<path>.*)$", protected_media),
]
if not settings.DEBUG:
    # En production (application interne à faible trafic), Django sert
    # lui-même ses fichiers statiques (en DEBUG, runserver s'en charge).
    urlpatterns += [
        re_path(r"^static/(?P<path>.*)$", file_serve,
                {"document_root": settings.STATIC_ROOT}),
    ]
