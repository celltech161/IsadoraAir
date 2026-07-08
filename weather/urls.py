from django.urls import path

from . import views

app_name = "weather"

urlpatterns = [
    path("gw3000", views.api_gw3000_ingest, name="gw3000_ingest"),
]
