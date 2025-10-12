from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.utils import timezone
from organizations.models import Organization, Branch
from .models import (
    Product, Category, Manufacturer, BulkOrder, BulkOrderItem, 
    BulkOrderStatusLog, BulkOrderPayment, InventoryItem
)

User = get_user_model()


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ['id', 'name', 'description', 'is_active']


class ManufacturerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Manufacturer
        fields = ['id', 'name', 'contact_person', 'phone', 'email']


class ProductSerializer(serializers.ModelSerializer):
    category = CategorySerializer(read_only=True)
    manufacturer = ManufacturerSerializer(read_only=True)
    
    class Meta:
        model = Product
        fields = [
            'id', 'name', 'generic_name', 'brand_name', 'product_code',
            'category', 'manufacturer', 'dosage_form', 'strength', 'unit',
            'cost_price', 'selling_price', 'is_active'
        ]


class ProductCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = [
            'name', 'generic_name', 'brand_name', 'product_code',
            'category', 'manufacturer', 'dosage_form', 'strength', 'unit',
            'cost_price', 'selling_price', 'description'
        ]


class ProductUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = [
            'name', 'generic_name', 'brand_name', 'dosage_form', 'strength',
            'cost_price', 'selling_price', 'description', 'is_active'
        ]


class StockEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = Product  # Placeholder
        fields = ['id']


class PurchaseOrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product  # Placeholder
        fields = ['id']


class SupplierSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product  # Placeholder
        fields = ['id']


class MedicationListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = ['id', 'name', 'generic_name', 'brand_name', 'strength']


class BulkMedicationUploadSerializer(serializers.Serializer):
    file = serializers.FileField()


class MedicationStatsSerializer(serializers.Serializer):
    total_medications = serializers.IntegerField()
    active_medications = serializers.IntegerField()
    prescription_medications = serializers.IntegerField()
    controlled_medications = serializers.IntegerField()


class BulkOrderItemSerializer(serializers.ModelSerializer):
    product = ProductSerializer(read_only=True)
    product_id = serializers.IntegerField(write_only=True)
    total_price = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    
    class Meta:
        model = BulkOrderItem
        fields = [
            'id', 'product', 'product_id', 'quantity_requested', 'quantity_confirmed',
            'quantity_final', 'unit_price', 'total_price', 'buyer_notes', 'supplier_notes', 
            'buyer_reconfirm_notes', 'is_available', 'is_cancelled', 'quantity_shipped', 'quantity_delivered'
        ]


class BulkOrderStatusLogSerializer(serializers.ModelSerializer):
    changed_by_name = serializers.CharField(source='changed_by.get_full_name', read_only=True)
    
    class Meta:
        model = BulkOrderStatusLog
        fields = ['id', 'from_status', 'to_status', 'notes', 'changed_by_name', 'changed_at']


class BulkOrderPaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = BulkOrderPayment
        fields = [
            'id', 'payment_type', 'payment_method', 'amount', 'payment_date',
            'reference_number', 'notes', 'installment_number', 'is_final_payment',
            'cash_amount', 'online_amount', 'credit_amount'
        ]


class BulkOrderSerializer(serializers.ModelSerializer):
    items = BulkOrderItemSerializer(many=True, read_only=True)
    status_logs = BulkOrderStatusLogSerializer(many=True, read_only=True)
    payments = BulkOrderPaymentSerializer(many=True, read_only=True)
    
    buyer_organization_name = serializers.CharField(source='buyer_organization.name', read_only=True)
    buyer_branch_name = serializers.CharField(source='buyer_branch.name', read_only=True)
    supplier_organization_name = serializers.CharField(source='supplier_organization.name', read_only=True)
    supplier_user_name = serializers.CharField(source='supplier_user.get_full_name', read_only=True)
    
    total_items = serializers.IntegerField(read_only=True)
    total_quantity = serializers.IntegerField(read_only=True)
    
    class Meta:
        model = BulkOrder
        fields = [
            'id', 'order_number', 'buyer_organization', 'buyer_organization_name',
            'buyer_branch', 'buyer_branch_name', 'supplier_organization', 
            'supplier_organization_name', 'supplier_user', 'supplier_user_name',
            'order_date', 'expected_delivery_date', 'status', 'subtotal', 
            'tax_amount', 'shipping_amount', 'total_amount', 'advance_amount',
            'advance_paid', 'total_paid_amount', 'remaining_amount', 'payment_status',
            'shipping_method', 'tracking_number', 'shipped_date', 'delivered_date', 
            'released_date', 'imported_date', 'buyer_notes', 'supplier_notes', 
            'buyer_delivery_notes', 'buyer_reconfirm_notes', 'shipping_notes', 
            'delivery_notes', 'can_modify_items', 'supplier_locked', 'items', 
            'status_logs', 'payments', 'total_items', 'total_quantity', 
            'created_at', 'updated_at'
        ]


class BulkOrderCreateSerializer(serializers.ModelSerializer):
    items = BulkOrderItemSerializer(many=True, write_only=True)
    supplier_user_id = serializers.IntegerField(write_only=True)
    
    class Meta:
        model = BulkOrder
        fields = [
            'supplier_user_id', 'expected_delivery_date', 'buyer_notes', 'items'
        ]
    
    def create(self, validated_data):
        items_data = validated_data.pop('items')
        supplier_user_id = validated_data.pop('supplier_user_id')
        
        # Get supplier user and organization
        supplier_user = User.objects.get(id=supplier_user_id, role='supplier_admin')
        supplier_organization = Organization.objects.get(id=supplier_user.organization_id)
        
        # Get buyer info from request user
        user = self.context['request'].user
        buyer_organization = Organization.objects.get(id=user.organization_id)
        buyer_branch = Branch.objects.get(id=user.branch_id)
        
        # Create bulk order
        bulk_order = BulkOrder.objects.create(
            buyer_organization=buyer_organization,
            buyer_branch=buyer_branch,
            supplier_organization=supplier_organization,
            supplier_user=supplier_user,
            created_by=user,
            status=BulkOrder.SUBMITTED,
            **validated_data
        )
        
        # Create order items
        for item_data in items_data:
            BulkOrderItem.objects.create(bulk_order=bulk_order, **item_data)
        
        # Create status log
        BulkOrderStatusLog.objects.create(
            bulk_order=bulk_order,
            to_status=BulkOrder.SUBMITTED,
            notes="Order submitted to supplier",
            changed_by=user
        )
        
        return bulk_order


class BulkOrderSupplierUpdateSerializer(serializers.ModelSerializer):
    items = BulkOrderItemSerializer(many=True)
    
    class Meta:
        model = BulkOrder
        fields = ['supplier_notes', 'items']
    
    def update(self, instance, validated_data):
        items_data = validated_data.pop('items', [])
        
        # Update bulk order
        instance.supplier_notes = validated_data.get('supplier_notes', instance.supplier_notes)
        instance.status = BulkOrder.SUPPLIER_CONFIRMED
        instance.save()
        
        # Update items with supplier confirmation
        total_amount = 0
        for item_data in items_data:
            item_id = item_data.get('id')
            if item_id:
                item = BulkOrderItem.objects.get(id=item_id, bulk_order=instance)
                item.quantity_confirmed = item_data.get('quantity_confirmed', item.quantity_confirmed)
                item.unit_price = item_data.get('unit_price', item.unit_price)
                item.supplier_notes = item_data.get('supplier_notes', item.supplier_notes)
                item.is_available = item_data.get('is_available', item.is_available)
                item.save()
                
                if item.unit_price and item.quantity_confirmed:
                    total_amount += item.unit_price * item.quantity_confirmed
        
        # Update order totals
        instance.subtotal = total_amount
        instance.total_amount = total_amount + instance.tax_amount + instance.shipping_amount
        instance.save()
        
        # Create status log
        BulkOrderStatusLog.objects.create(
            bulk_order=instance,
            from_status=BulkOrder.SUPPLIER_REVIEWING,
            to_status=BulkOrder.SUPPLIER_CONFIRMED,
            notes="Supplier confirmed order with pricing",
            changed_by=self.context['request'].user
        )
        
        return instance


class BulkOrderBuyerUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = BulkOrder
        fields = ['buyer_delivery_notes', 'advance_amount']
    
    def update(self, instance, validated_data):
        instance.buyer_delivery_notes = validated_data.get('buyer_delivery_notes', instance.buyer_delivery_notes)
        instance.advance_amount = validated_data.get('advance_amount', instance.advance_amount)
        instance.status = BulkOrder.BUYER_CONFIRMED
        instance.save()
        
        # Create status log
        BulkOrderStatusLog.objects.create(
            bulk_order=instance,
            from_status=BulkOrder.BUYER_REVIEWING,
            to_status=BulkOrder.BUYER_CONFIRMED,
            notes="Buyer confirmed order and delivery details",
            changed_by=self.context['request'].user
        )
        
        return instance


class BulkOrderShippingUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = BulkOrder
        fields = ['shipping_method', 'tracking_number', 'shipping_notes']
    
    def update(self, instance, validated_data):
        instance.shipping_method = validated_data.get('shipping_method', instance.shipping_method)
        instance.tracking_number = validated_data.get('tracking_number', instance.tracking_number)
        instance.shipping_notes = validated_data.get('shipping_notes', instance.shipping_notes)
        instance.status = BulkOrder.SHIPPED
        instance.shipped_date = timezone.now()
        instance.save()
        
        # Create status log
        BulkOrderStatusLog.objects.create(
            bulk_order=instance,
            from_status=BulkOrder.BUYER_CONFIRMED,
            to_status=BulkOrder.SHIPPED,
            notes=f"Order shipped via {instance.shipping_method}",
            changed_by=self.context['request'].user
        )
        
        return instance