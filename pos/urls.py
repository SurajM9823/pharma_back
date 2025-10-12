from django.urls import path
from . import views

app_name = 'pos'

urlpatterns = [
    # Stock allocation and validation
    path('allocate-stock/', views.allocate_stock, name='allocate_stock'),
    path('validate-stock/', views.validate_stock_before_sale, name='validate_stock'),
    
    # Sales management
    path('sales/', views.get_sales, name='get_sales'),
    path('sales/create/', views.create_sale, name='create_sale'),
    path('sales/save-pending/', views.save_pending_bill, name='save_pending_bill'),
    path('sales/<int:sale_id>/update-pending/', views.update_pending_bill, name='update_pending_bill'),
    path('sales/complete/', views.complete_sale, name='complete_sale'),
    path('sales/pending/', views.get_pending_bills, name='get_pending_bills'),
    path('sales/<str:sale_id>/', views.get_sale_detail, name='sale_detail'),
    path('sales/<str:sale_id>/delete/', views.delete_sale, name='delete_sale'),
    path('sales/<str:sale_id>/receipt/', views.generate_receipt, name='generate_receipt'),
    path('sales/<str:sale_id>/pay-credit/', views.process_credit_payment, name='process_credit_payment'),
    
    # Statistics
    path('stats/', views.pos_stats, name='pos_stats'),
    
    # Settings
    path('settings/', views.pos_settings, name='pos_settings'),
]