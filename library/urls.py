from django.urls import path

from . import views

app_name = 'library'

urlpatterns = [
    path('schedule/', views.schedule_page, name='schedule'),
    path('api/schedule/', views.api_schedule_list, name='api-schedule-list'),
    path('api/schedule/<int:pk>/', views.api_schedule_delete, name='api-schedule-delete'),
    path('api/clocks/', views.api_clock_list, name='api-clock-list'),
]
