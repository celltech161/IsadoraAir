from django.urls import path

from . import views

app_name = "webrequests"

urlpatterns = [
    path("web-request/", views.web_request_page, name="web_request_page"),
    path("api/web-request/config/", views.api_web_request_config, name="api-config"),
    path("api/web-request/open-slot/toggle/", views.api_open_slot_toggle, name="api-open-slot-toggle"),
    path("api/web-request/open-slot/toggle-row/", views.api_open_slot_toggle_row, name="api-open-slot-toggle-row"),
    path("api/web-request/open-slot/toggle-column/", views.api_open_slot_toggle_column, name="api-open-slot-toggle-column"),
]
