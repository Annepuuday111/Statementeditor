from django.urls import path
from . import views

app_name = 'statements'

urlpatterns = [
    path('', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('upload/', views.upload_statement, name='upload'),
    path('edit/<int:pk>/', views.edit_statement, name='edit'),
    path('save/<int:pk>/', views.save_statement, name='save'),
    path('download/<int:pk>/', views.download_statement, name='download'),
    path('<int:pk>/delete/', views.delete_statement, name='delete'),
]
