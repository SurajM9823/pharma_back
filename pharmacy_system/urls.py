"""
URL configuration for pharmacy_system project.
"""

from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from . import views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('backend/auth/', include('accounts.urls')),
    path('backend/organizations/', include('organizations.urls')),
    path('backend/inventory/', include('inventory.urls')),
    path('backend/pos/', include('pos.urls')),
    path('backend/patients/', include('patients.urls')),
    path('backend/', views.api_status),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)