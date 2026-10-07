from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve as file_serve

from grader.views import AdminLoginView, protected_media

urlpatterns = [
    path("admin/", admin.site.urls),
    # avant l'include ci-dessous : django.contrib.auth.urls capterait sinon
    # tout le préfixe « comptes/ » et renverrait 404 sur cette page.
    path("comptes/administration/", AdminLoginView.as_view(), name="admin_login"),
    path("comptes/", include("django.contrib.auth.urls")),
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
