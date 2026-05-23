from django.urls import path

from licenses import views


urlpatterns = [
    path('register', views.register, name='register'),
    path('heartbeat', views.heartbeat, name='heartbeat'),
]
