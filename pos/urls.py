from django.urls import path
from . import views
from . import reports_views

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
    
    # Reports endpoints
    path('reports/sales-summary/', reports_views.sales_summary, name='sales_summary'),
    path('reports/daily-trend/', reports_views.daily_sales_trend, name='daily_sales_trend'),
    path('reports/hourly-pattern/', reports_views.hourly_sales_pattern, name='hourly_sales_pattern'),
    path('reports/top-products/', reports_views.top_selling_products, name='top_selling_products'),
    path('reports/payment-methods/', reports_views.payment_methods_report, name='payment_methods_report'),
    path('reports/staff-performance/', reports_views.staff_performance_report, name='staff_performance_report'),
    path('reports/customer-analytics/', reports_views.customer_analytics, name='customer_analytics'),
    path('reports/export/', reports_views.export_sales_report, name='export_sales_report'),
]