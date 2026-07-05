from django.urls import path

from . import views

app_name = 'library'

urlpatterns = [
    path('', views.dashboard_page, name='dashboard'),
    path('schedule/', views.schedule_page, name='schedule'),
    path('library/', views.library_page, name='library'),
    path('api/schedule/', views.api_schedule_list, name='api-schedule-list'),
    path('api/schedule/<int:pk>/', views.api_schedule_delete, name='api-schedule-delete'),
    path('api/rotations/', views.api_rotation_list, name='api-rotation-list'),
    path('playlists/', views.playlists_page, name='playlists'),
    path('api/playlists/', views.api_playlist_list, name='api-playlist-list'),
    path('api/playlists/<int:pk>/', views.api_playlist_detail, name='api-playlist-detail'),
    path('api/playlists/<int:pk>/items/', views.api_playlist_add_item, name='api-playlist-add-item'),
    path('api/playlists/item/<int:item_id>/', views.api_playlist_remove_item, name='api-playlist-remove-item'),
    path('api/playlists/<int:pk>/reorder/', views.api_playlist_reorder, name='api-playlist-reorder'),
    path('api/playlists/<int:pk>/play-now/', views.api_playlist_play_now, name='api-playlist-play-now'),
    path('api/tracks/', views.api_track_list, name='api-track-list'),
    path('api/tracks/bulk/', views.api_track_bulk, name='api-track-bulk'),
    path('api/tracks/<int:pk>/', views.api_track_detail, name='api-track-detail'),
    path('track/<int:pk>/', views.track_detail_page, name='track-detail'),
    path('logs/', views.logs_page, name='logs'),
    path('api/log/build/', views.api_log_build, name='api-log-build'),
    path('api/log/<str:date_str>/<int:hour>/', views.api_log_get, name='api-log-get'),
    path('api/log/<str:date_str>/', views.api_log_list_date, name='api-log-list-date'),
    path('api/log/<int:pk>/update/', views.api_log_update, name='api-log-update'),
    path('api/log/<int:pk>/delete/', views.api_log_delete, name='api-log-delete'),
    path('api/log/<int:pk>/reorder/', views.api_log_reorder, name='api-log-reorder'),
    path('api/log/item/<int:item_id>/swap/', views.api_log_item_swap, name='api-log-item-swap'),
    path('api/waveform/<int:track_id>/', views.api_waveform, name='api-waveform'),
    path('api/albumart/<int:track_id>/', views.api_album_art, name='api-album-art'),
    path('api/engine/status/', views.api_engine_status, name='api-engine-status'),
    path('api/engine/queue/set-next/', views.api_engine_set_next, name='api-engine-set-next'),
    path('api/engine/queue/insert/', views.api_engine_insert_track, name='api-engine-insert-track'),
    path('api/engine/seek/', views.api_engine_seek, name='api-engine-seek'),
    path('api/engine/deck/<str:slot>/', views.api_engine_deck_command, name='api-engine-deck-command'),
    path('api/engine/restart/', views.api_engine_restart, name='api-engine-restart'),
]
