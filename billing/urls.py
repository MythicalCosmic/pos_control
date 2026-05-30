from django.urls import path

from billing import views


urlpatterns = [
    path('click/prepare', views.click_prepare, name='click-prepare'),
    path('click/complete', views.click_complete, name='click-complete'),
    path('payme', views.payme_endpoint, name='payme'),
]
