from django.urls import path

from . import views

app_name = 'organizations'

urlpatterns = [
    # Organization endpoints
    path('', views.OrganizationListView.as_view(), name='organization_list'),
    path('<int:pk>/', views.OrganizationDetailView.as_view(), name='organization_detail'),

    # Branch endpoints
    path('branches/', views.BranchListView.as_view(), name='branch_list'),
    path('branches/<int:pk>/', views.BranchDetailView.as_view(), name='branch_detail'),

    # Organization settings
    path('settings/', views.OrganizationSettingsView.as_view(), name='organization_settings'),

    # Utility endpoints
    path('stats/', views.get_organization_stats, name='organization_stats'),
    path('create-with-owner/', views.create_organization_with_owner, name='create_organization_with_owner'),
    path('create-default-branch/', views.create_default_branch, name='create_default_branch'),
]