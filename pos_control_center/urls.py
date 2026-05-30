"""Root URL conf.

API surface is versioned under /api/v1/ from day one so we have room to
break the contract later without coordinating with every alpha_pos
install in the field.
"""
from django.contrib import admin
from django.http import HttpResponse
from django.urls import include, path


def healthz(_request):
    """Container probe + uptime check from the alpha_pos status page."""
    return HttpResponse('ok', content_type='text/plain')


urlpatterns = [
    path('healthz', healthz),
    path('admin/', admin.site.urls),
    path('api/v1/', include('licenses.urls')),
    # Payment-provider webhooks (Click.uz / Payme.uz top-ups).
    path('api/billing/', include('billing.urls')),
]
