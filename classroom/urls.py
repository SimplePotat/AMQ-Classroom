from django.urls import path
from . import views

urlpatterns = [
    path("", views.index), path("host", views.index),
    path("assets/<str:filename>", views.asset),
    path("api/state", views.state), path("api/auth", views.auth),
    path("api/player", views.player), path("api/host", views.host),
    path("api/logout", views.logout), path("api/qr", views.qr),
    path("api/media/<str:round_id>", views.media), path("api/results.csv", views.results),
]
