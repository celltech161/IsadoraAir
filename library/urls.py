from django.urls import path

from . import views

app_name = 'library'

urlpatterns = [
    path('schedule/', views.schedule_page, name='schedule'),
    path('library/', views.library_page, name='library'),
    path('api/schedule/', views.api_schedule_list, name='api-schedule-list'),
    path('api/schedule/<int:pk>/', views.api_schedule_delete, name='api-schedule-delete'),
    path('api/clocks/', views.api_clock_list, name='api-clock-list'),
    path('api/tracks/', views.api_track_list, name='api-track-list'),
    path('api/tracks/bulk/', views.api_track_bulk, name='api-track-bulk'),
    path('api/tracks/<int:pk>/', views.api_track_detail, name='api-track-detail'),
    path('track/<int:pk>/', views.track_detail_page, name='track-detail'),
]
