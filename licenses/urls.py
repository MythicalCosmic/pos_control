from django.urls import path

from licenses import views


urlpatterns = [
    path('register', views.register, name='register'),
    path('heartbeat', views.heartbeat, name='heartbeat'),
    path('plans', views.plans, name='plans'),
    path('plan-change', views.plan_change, name='plan-change'),
]
