from rest_framework import generics, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.db import transaction, models
from django.shortcuts import get_object_or_404
from django.utils import timezone
from datetime import datetime, date
import json

from .models import Sale, SaleItem, Prescription, Payment, Return, POSSettings
from patients.models import Patient
from inventory.models import InventoryItem, Product
from organizations.models import Branch, Organization
from django.db.models import Sum, Count, Avg, Q, F
from datetime import datetime, timedelta
from .manager_dashboard_views import *


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def allocate_stock(request):
    """Allocate stock for POS using FIFO method."""
    try:
        medicine_id = request.data.get('medicine_id')
        quantity = int(request.data.get('quantity', 0))
        branch_id = request.data.get('branch_id')
        
        if not medicine_id or quantity <= 0:
            return Response({'error': 'Invalid medicine_id or quantity'}, status=400)
        
        # Get available inventory items for this medicine, ordered by expiry date (FIFO)
        inventory_items = InventoryItem.objects.filter(
            product_id=medicine_id,
            branch_id=branch_id,
            quantity__gt=0,
            is_active=True
        ).order_by('expiry_date', 'created_at')
        
        if not inventory_items.exists():
            return Response({'error': 'No stock available for this medicine'}, status=400)
        
        # Check total available stock
        total_available = sum(item.quantity for item in inventory_items)
        if total_available < quantity:
            return Response({'error': f'Insufficient stock. Available: {total_available}, Requested: {quantity}'}, status=400)
        
        # Allocate stock using FIFO
        allocations = []
        remaining_quantity = quantity
        
        print(f"DEBUG: allocate_stock - Need to allocate {quantity} units")
        
        for item in inventory_items:
            if remaining_quantity <= 0:
                break
                
            allocated_quantity = min(item.quantity, remaining_quantity)
            print(f"DEBUG: allocate_stock - Allocating {allocated_quantity} from batch {item.batch_number} (available: {item.quantity})")
            
            allocations.append({
                'inventory_item_id': item.id,
                'batch_number': item.batch_number,
                'expiry_date': item.expiry_date.isoformat(),
                'allocated_quantity': allocated_quantity,
                'selling_price': float(item.selling_price or item.cost_price),
                'available_quantity': item.quantity
            })
            
            remaining_quantity -= allocated_quantity
            print(f"DEBUG: allocate_stock - Remaining to allocate: {remaining_quantity}")
        
        print(f"DEBUG: allocate_stock - Final allocations: {allocations}")
        return Response({
            'allocations': allocations,
            'total_allocated': quantity,
            'medicine_id': medicine_id
        })
        
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def save_pending_bill(request):
    """Save a pending bill without reducing stock."""
    try:
        with transaction.atomic():
            data = request.data
            
            # Get or create patient
            patient = None
            patient_name = data.get('patient_name', '').strip()
            patient_phone = data.get('patient_phone', '').strip()
            patient_id = data.get('patient_id', '').strip()
            
            if patient_id:
                try:
                    patient = Patient.objects.get(patient_id=patient_id)
                except Patient.DoesNotExist:
                    pass
            
            if not patient and patient_name:
                org_id = request.user.organization_id
                timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
                anonymous_patient_id = f"PT_{org_id}_{timestamp}"
                
                patient = Patient.objects.create(
                    patient_id=anonymous_patient_id,
                    first_name=patient_name.split()[0] if patient_name else 'Anonymous',
                    last_name=' '.join(patient_name.split()[1:]) if len(patient_name.split()) > 1 else 'Patient',
                    date_of_birth=date.today(),
                    gender=data.get('patient_gender', 'other'),
                    phone=patient_phone or '0000000000',
                    address='Walk-in Customer',
                    city='Unknown',
                    organization_id=org_id,
                    branch_id=data.get('branch_id'),
                    patient_type='outpatient',
                    created_by=request.user
                )
            
            # Generate sale number
            branch_id = data.get('branch_id') or request.user.branch_id
            timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
            sale_number = f"PENDING_{branch_id}_{timestamp}"
            
            # Calculate amounts properly (discount before tax)
            subtotal = float(data.get('subtotal', 0))
            discount_amount = float(data.get('discount_amount', 0))
            discounted_subtotal = subtotal - discount_amount
            tax_amount = discounted_subtotal * 0.13
            calculated_total = discounted_subtotal + tax_amount
            
            # Create pending sale
            sale = Sale.objects.create(
                sale_number=sale_number,
                patient=patient,
                patient_name=patient_name or (patient.get_full_name() if patient else 'Walk-in Customer'),
                patient_age=data.get('patient_age', ''),
                patient_phone=patient_phone,
                patient_gender=data.get('patient_gender', ''),
                sale_type=data.get('payment_method', 'cash'),
                subtotal=subtotal,
                tax_amount=tax_amount,
                discount_amount=discount_amount,
                total_amount=calculated_total,
                amount_paid=0,  # No payment for pending bills
                credit_amount=calculated_total,  # Full amount as credit
                payment_method=data.get('payment_method', 'cash'),
                organization_id=request.user.organization_id,
                branch_id=branch_id,
                created_by=request.user,
                status='pending'  # Pending status
            )
            
            # Process sale items (no stock reduction)
            items = data.get('items', [])
            for item_data in items:
                product = get_object_or_404(Product, id=item_data['medicine_id'])
                batch_info = item_data.get('batch_info', [])
                
                SaleItem.objects.create(
                    sale=sale,
                    product=product,
                    quantity=item_data['quantity'],
                    unit_price=item_data['price'],
                    batch_number=item_data.get('batch', ''),
                    allocated_batches=batch_info
                )
            
            return Response({
                'success': True,
                'sale_id': sale.id,
                'sale_number': sale.sale_number,
                'message': 'Pending bill saved successfully'
            })
            
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['PUT'])
@permission_classes([IsAuthenticated])
def update_pending_bill(request, sale_id):
    """Update an existing pending bill."""
    try:
        with transaction.atomic():
            data = request.data
            
            # Get existing pending sale
            sale = get_object_or_404(Sale, id=sale_id, status='pending', organization_id=request.user.organization_id)
            
            # Update patient info
            patient_name = data.get('patient_name', '').strip()
            patient_phone = data.get('patient_phone', '').strip()
            
            sale.patient_name = patient_name or sale.patient_name
            sale.patient_age = data.get('patient_age', sale.patient_age)
            sale.patient_phone = patient_phone or sale.patient_phone
            sale.patient_gender = data.get('patient_gender', sale.patient_gender)
            
            # Update amounts
            subtotal = float(data.get('subtotal', 0))
            discount_amount = float(data.get('discount_amount', 0))
            discounted_subtotal = subtotal - discount_amount
            tax_amount = discounted_subtotal * 0.13
            calculated_total = discounted_subtotal + tax_amount
            
            sale.subtotal = subtotal
            sale.tax_amount = tax_amount
            sale.discount_amount = discount_amount
            sale.total_amount = calculated_total
            sale.credit_amount = calculated_total
            sale.payment_method = data.get('payment_method', sale.payment_method)
            sale.save()
            
            # Delete existing items first
            SaleItem.objects.filter(sale=sale).delete()
            
            # Add updated items
            items = data.get('items', [])
            for item_data in items:
                product = get_object_or_404(Product, id=item_data['medicine_id'])
                batch_info = item_data.get('batch_info', [])
                
                SaleItem.objects.create(
                    sale=sale,
                    product=product,
                    quantity=item_data['quantity'],
                    unit_price=item_data['price'],
                    batch_number=item_data.get('batch', ''),
                    allocated_batches=batch_info
                )
            
            return Response({
                'success': True,
                'sale_id': sale.id,
                'sale_number': sale.sale_number,
                'message': 'Pending bill updated successfully'
            })
            
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def complete_sale(request):
    """Complete a pending sale or create new sale with stock reduction."""
    try:
        with transaction.atomic():
            data = request.data
            sale_id = data.get('sale_id')
            
            if sale_id:
                # Complete existing pending sale
                sale = get_object_or_404(Sale, id=sale_id, status='pending', organization_id=request.user.organization_id)
                
                # Update sale with new data
                paid_amount = float(data.get('paid_amount', 0))
                total_amount = float(data.get('total', sale.total_amount))
                
                # Update all sale fields
                sale.patient_name = data.get('patient_name', sale.patient_name)
                sale.patient_age = data.get('patient_age', sale.patient_age)
                sale.patient_phone = data.get('patient_phone', sale.patient_phone)
                sale.patient_gender = data.get('patient_gender', sale.patient_gender)
                sale.subtotal = float(data.get('subtotal', sale.subtotal))
                sale.tax_amount = float(data.get('tax_amount', sale.tax_amount))
                sale.discount_amount = float(data.get('discount_amount', sale.discount_amount))
                sale.total_amount = total_amount
                sale.amount_paid = paid_amount
                sale.credit_amount = max(0, total_amount - paid_amount)
                sale.change_amount = max(0, paid_amount - total_amount)
                sale.payment_method = data.get('payment_method', sale.payment_method)
                sale.transaction_id = data.get('transaction_id', '')
                sale.status = 'completed'
                sale.completed_by = request.user
                
                # Update sale number for completed sale
                timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
                sale.sale_number = f"BILL_{sale.branch_id}_{timestamp}"
                
                # Update items if provided
                if 'items' in data:
                    sale.items.all().delete()
                    for item_data in data['items']:
                        product = get_object_or_404(Product, id=item_data['medicine_id'])
                        batch_info = item_data.get('batch_info', [])
                        
                        SaleItem.objects.create(
                            sale=sale,
                            product=product,
                            quantity=item_data['quantity'],
                            unit_price=item_data['price'],
                            batch_number=item_data.get('batch', ''),
                            allocated_batches=batch_info
                        )
                
                sale.save()
                
                # Reduce stock for all items - stock was already allocated during cart operations
                print(f"DEBUG: Starting stock reduction for sale {sale.id}")
                print(f"DEBUG: Sale items count: {sale.items.count()}")

                for sale_item in sale.items.all():
                    print(f"DEBUG: Processing sale item: {sale_item.product.name}, quantity: {sale_item.quantity}")
                    print(f"DEBUG: Allocated batches: {sale_item.allocated_batches}")
                    
                    # Check if allocated_batches is empty or None
                    if not sale_item.allocated_batches:
                        print(f"DEBUG: No allocated batches found for {sale_item.product.name}, using FIFO allocation")
                        # If no allocated batches, do FIFO allocation now
                        inventory_items = InventoryItem.objects.filter(
                            product_id=sale_item.product.id,
                            branch_id=sale.branch_id,
                            quantity__gt=0,
                            is_active=True
                        ).order_by('expiry_date', 'created_at')
                        
                        remaining_quantity = sale_item.quantity
                        for item in inventory_items:
                            if remaining_quantity <= 0:
                                break
                            
                            allocated_quantity = min(item.quantity, remaining_quantity)
                            print(f"DEBUG: FIFO - Reducing {allocated_quantity} from batch {item.batch_number} (current: {item.quantity})")
                            
                            if item.quantity >= allocated_quantity:
                                item.quantity -= allocated_quantity
                                item.save()
                                remaining_quantity -= allocated_quantity
                                print(f"DEBUG: FIFO - Stock reduced successfully. New quantity: {item.quantity}")
                            else:
                                print(f"DEBUG: FIFO - ERROR - Insufficient stock in batch {item.batch_number}")
                                raise ValueError(f"Insufficient stock in batch {item.batch_number}")
                        
                        if remaining_quantity > 0:
                            print(f"DEBUG: FIFO - ERROR - Could not allocate all stock. Remaining: {remaining_quantity}")
                            raise ValueError(f"Insufficient total stock for {sale_item.product.name}")
                    else:
                        # Use existing allocated batches
                        total_allocated = sum(batch['allocated_quantity'] for batch in sale_item.allocated_batches)
                        print(f"DEBUG: Sale item {sale_item.product.name} - allocated: {total_allocated}, required: {sale_item.quantity}")

                        # Verify total allocated matches sale quantity
                        if total_allocated != sale_item.quantity:
                            print(f"DEBUG: ERROR - Stock allocation mismatch for {sale_item.product.name}: allocated {total_allocated}, required {sale_item.quantity}")
                            raise ValueError(f"Stock allocation mismatch for {sale_item.product.name}: allocated {total_allocated}, required {sale_item.quantity}")

                        # Actually reduce stock now
                        for batch in sale_item.allocated_batches:
                            print(f"DEBUG: Processing batch: {batch}")
                            inventory_item = get_object_or_404(InventoryItem, id=batch['inventory_item_id'])
                            allocated_qty = batch['allocated_quantity']

                            print(f"DEBUG: Reducing stock for {sale_item.product.name} batch {batch['batch_number']}: current={inventory_item.quantity}, reducing={allocated_qty}")

                            if inventory_item.quantity >= allocated_qty:
                                inventory_item.quantity -= allocated_qty
                                inventory_item.save()
                                print(f"DEBUG: Stock reduced successfully. New quantity: {inventory_item.quantity}")
                            else:
                                print(f"DEBUG: ERROR - Insufficient stock in batch {batch['batch_number']}: has {inventory_item.quantity}, need {allocated_qty}")
                                raise ValueError(f"Insufficient stock in batch {batch['batch_number']}")
                
                # Handle split payments or single payment
                split_payments = data.get('split_payments')
                if split_payments and len(split_payments) > 0:
                    # Create multiple payment records for split payments
                    for split_payment in split_payments:
                        if split_payment.get('amount') and float(split_payment['amount']) > 0:
                            Payment.objects.create(
                                sale=sale,
                                amount=float(split_payment['amount']),
                                payment_method=split_payment.get('method', 'cash'),
                                reference_number=split_payment.get('transaction_id', ''),
                                received_by=request.user
                            )
                elif paid_amount > 0:
                    # Single payment record
                    Payment.objects.create(
                        sale=sale,
                        amount=paid_amount,
                        payment_method=data.get('payment_method', 'cash'),
                        reference_number=data.get('transaction_id', ''),
                        received_by=request.user
                    )
                
                # Generate receipt data with POS settings
                organization = sale.organization
                branch = sale.branch
                
                # Get POS settings for receipt
                try:
                    pos_settings = POSSettings.objects.get(organization_id=sale.organization_id, branch_id=sale.branch_id)
                    business_name = pos_settings.business_name or organization.name
                    business_address = pos_settings.business_address or getattr(organization, 'address', '')
                    business_phone = pos_settings.business_phone or getattr(organization, 'phone', '')
                    business_email = pos_settings.business_email or getattr(organization, 'email', '')
                    receipt_footer = pos_settings.receipt_footer or 'Thank you for your business!'
                    receipt_logo = request.build_absolute_uri(pos_settings.receipt_logo.url) if pos_settings.receipt_logo else None
                    tax_rate = pos_settings.tax_rate
                except POSSettings.DoesNotExist:
                    business_name = organization.name
                    business_address = getattr(organization, 'address', '')
                    business_phone = getattr(organization, 'phone', '')
                    business_email = getattr(organization, 'email', '')
                    receipt_footer = 'Thank you for your business!'
                    receipt_logo = None
                    tax_rate = 13
                
                receipt_data = {
                    'organization': {
                        'name': business_name,
                        'address': business_address,
                        'phone': business_phone,
                        'email': business_email
                    },
                    'branch': {
                        'name': branch.name if branch else ''
                    },
                    'settings': {
                        'receipt_footer': receipt_footer,
                        'receipt_logo': receipt_logo,
                        'tax_rate': tax_rate
                    },
                    'sale': {
                        'sale_number': sale.sale_number,
                        'sale_date': sale.created_at.strftime('%Y-%m-%d %I:%M %p'),
                        'cashier': request.user.get_full_name() or request.user.username
                    },
                    'patient': {
                        'name': sale.patient_name,
                        'patient_id': sale.patient.patient_id if sale.patient else '',
                        'age': sale.patient_age,
                        'phone': sale.patient_phone,
                        'gender': sale.patient_gender
                    },
                    'items': [{
                        'name': item.product.name,
                        'quantity': item.quantity,
                        'unit_price': float(item.unit_price),
                        'total': float(item.quantity * item.unit_price),
                        'batch': item.batch_number
                    } for item in sale.items.all()],
                    'totals': {
                        'subtotal': float(sale.subtotal),
                        'tax': float(sale.tax_amount),
                        'discount': float(sale.discount_amount),
                        'total': float(sale.total_amount),
                        'paid': float(sale.amount_paid),
                        'credit': float(sale.credit_amount),
                        'change': float(sale.change_amount)
                    },
                    'payment_method': sale.payment_method
                }
                
                return Response({
                    'success': True,
                    'sale_id': sale.id,
                    'sale_number': sale.sale_number,
                    'message': 'Sale completed successfully',
                    'receipt': receipt_data
                })
            
            else:
                # Create new direct sale
                return create_direct_sale(request)
                
    except Exception as e:
        return Response({'error': str(e)}, status=500)


def create_direct_sale(request):
    """Create a direct sale with immediate stock reduction."""
    data = request.data
    
    # Get or create patient
    patient = None
    patient_name = data.get('patient_name', '').strip()
    patient_phone = data.get('patient_phone', '').strip()
    patient_id = data.get('patient_id', '').strip()
    
    if patient_id:
        try:
            patient = Patient.objects.get(patient_id=patient_id)
        except Patient.DoesNotExist:
            pass
    
    if not patient and patient_name:
        org_id = request.user.organization_id
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        anonymous_patient_id = f"PT_{org_id}_{timestamp}"
        
        patient = Patient.objects.create(
            patient_id=anonymous_patient_id,
            first_name=patient_name.split()[0] if patient_name else 'Anonymous',
            last_name=' '.join(patient_name.split()[1:]) if len(patient_name.split()) > 1 else 'Patient',
            date_of_birth=date.today(),
            gender=data.get('patient_gender', 'other'),
            phone=patient_phone or '0000000000',
            address='Walk-in Customer',
            city='Unknown',
            organization_id=org_id,
            branch_id=data.get('branch_id'),
            patient_type='outpatient',
            created_by=request.user
        )
    
    # Generate sale number
    branch_id = data.get('branch_id') or request.user.branch_id
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    sale_number = f"BILL_{branch_id}_{timestamp}"
    
    # Calculate amounts properly (discount before tax)
    subtotal = float(data.get('subtotal', 0))
    discount_amount = float(data.get('discount_amount', 0))
    discounted_subtotal = subtotal - discount_amount
    tax_amount = discounted_subtotal * 0.13
    calculated_total = discounted_subtotal + tax_amount
    paid_amount = float(data.get('paid_amount', 0))
    
    # Create sale
    sale = Sale.objects.create(
        sale_number=sale_number,
        patient=patient,
        patient_name=patient_name or (patient.get_full_name() if patient else 'Walk-in Customer'),
        patient_age=data.get('patient_age', ''),
        patient_phone=patient_phone,
        patient_gender=data.get('patient_gender', ''),
        sale_type=data.get('payment_method', 'cash'),
        subtotal=subtotal,
        tax_amount=tax_amount,
        discount_amount=discount_amount,
        total_amount=calculated_total,
        amount_paid=paid_amount,
        credit_amount=max(0, calculated_total - paid_amount),
        change_amount=max(0, paid_amount - calculated_total),
        payment_method=data.get('payment_method', 'cash'),
        transaction_id=data.get('transaction_id', ''),
        organization_id=request.user.organization_id,
        branch_id=branch_id,
        created_by=request.user,
        completed_by=request.user,
        status='completed'
    )
    
    # Process sale items with stock reduction
    items = data.get('items', [])
    for item_data in items:
        product = get_object_or_404(Product, id=item_data['medicine_id'])
        batch_info = item_data.get('batch_info', [])
        
        # Create sale item
        SaleItem.objects.create(
            sale=sale,
            product=product,
            quantity=item_data['quantity'],
            unit_price=item_data['price'],
            batch_number=item_data.get('batch', ''),
            allocated_batches=batch_info
        )
        
        # Reduce stock - handle both allocated batches and FIFO fallback
        if batch_info:
            # Use allocated batches
            for batch in batch_info:
                inventory_item = get_object_or_404(InventoryItem, id=batch['inventory_item_id'])
                allocated_qty = batch['allocated_quantity']
                
                if inventory_item.quantity >= allocated_qty:
                    inventory_item.quantity -= allocated_qty
                    inventory_item.save()
                else:
                    raise ValueError(f"Insufficient stock in batch {batch['batch_number']}")
        else:
            # FIFO fallback if no batch info
            print(f"DEBUG: No batch info for {product.name}, using FIFO allocation")
            inventory_items = InventoryItem.objects.filter(
                product_id=product.id,
                branch_id=branch_id,
                quantity__gt=0,
                is_active=True
            ).order_by('expiry_date', 'created_at')
            
            remaining_quantity = item_data['quantity']
            for item in inventory_items:
                if remaining_quantity <= 0:
                    break
                
                allocated_quantity = min(item.quantity, remaining_quantity)
                print(f"DEBUG: FIFO - Reducing {allocated_quantity} from batch {item.batch_number}")
                
                if item.quantity >= allocated_quantity:
                    item.quantity -= allocated_quantity
                    item.save()
                    remaining_quantity -= allocated_quantity
                else:
                    raise ValueError(f"Insufficient stock in batch {item.batch_number}")
            
            if remaining_quantity > 0:
                raise ValueError(f"Insufficient total stock for {product.name}")
    
    # Handle split payments or single payment
    split_payments = data.get('split_payments')
    if split_payments and len(split_payments) > 0:
        # Create multiple payment records for split payments
        for split_payment in split_payments:
            if split_payment.get('amount') and float(split_payment['amount']) > 0:
                Payment.objects.create(
                    sale=sale,
                    amount=float(split_payment['amount']),
                    payment_method=split_payment.get('method', 'cash'),
                    reference_number=split_payment.get('transaction_id', ''),
                    received_by=request.user
                )
    elif paid_amount > 0:
        # Single payment record
        Payment.objects.create(
            sale=sale,
            amount=paid_amount,
            payment_method=data.get('payment_method', 'cash'),
            reference_number=data.get('transaction_id', ''),
            received_by=request.user
        )
    
    return Response({
        'success': True,
        'sale_id': sale.id,
        'sale_number': sale.sale_number,
        'message': 'Sale created successfully'
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def create_sale(request):
    """Create a new sale - wrapper for backward compatibility."""
    try:
        with transaction.atomic():
            data = request.data
            print(f"DEBUG: create_sale called")
            print(f"DEBUG: Items in request: {data.get('items', [])}")
            
            # Get organization and branch IDs
            org_id = getattr(request.user, 'organization_id', None)
            branch_id = data.get('branch_id') or getattr(request.user, 'branch_id', None)
            
            # Create new direct sale
            patient = None
            patient_name = data.get('patient_name', '').strip()
            patient_phone = data.get('patient_phone', '').strip()
            patient_id = data.get('patient_id', '').strip()
            
            if patient_id:
                try:
                    patient = Patient.objects.get(patient_id=patient_id)
                except Patient.DoesNotExist:
                    pass
            
            if not patient and patient_name:
                timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
                anonymous_patient_id = f"PT_{org_id}_{timestamp}"
                
                patient = Patient.objects.create(
                    patient_id=anonymous_patient_id,
                    first_name=patient_name.split()[0] if patient_name else 'Anonymous',
                    last_name=' '.join(patient_name.split()[1:]) if len(patient_name.split()) > 1 else 'Patient',
                    date_of_birth=date.today(),
                    gender=data.get('patient_gender', 'other'),
                    phone=patient_phone or '0000000000',
                    address='Walk-in Customer',
                    city='Unknown',
                    organization_id=org_id,
                    branch_id=branch_id,
                    patient_type='outpatient',
                    created_by=request.user
                )
            
            timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
            sale_number = f"BILL_{branch_id}_{timestamp}"
            
            subtotal = float(data.get('subtotal', 0))
            tax_amount = float(data.get('tax_amount', 0))
            discount_amount = float(data.get('discount_amount', 0))
            total_amount = float(data.get('total', 0))
            paid_amount = float(data.get('paid_amount', 0))
            
            sale = Sale.objects.create(
                sale_number=sale_number,
                patient=patient,
                patient_name=patient_name or (patient.get_full_name() if patient else 'Walk-in Customer'),
                patient_age=data.get('patient_age', ''),
                patient_phone=patient_phone,
                patient_gender=data.get('patient_gender', ''),
                sale_type=data.get('payment_method', 'cash'),
                subtotal=subtotal,
                tax_amount=tax_amount,
                discount_amount=discount_amount,
                total_amount=total_amount,
                amount_paid=paid_amount,
                credit_amount=max(0, total_amount - paid_amount),
                change_amount=max(0, paid_amount - total_amount),
                payment_method=data.get('payment_method', 'cash'),
                transaction_id=data.get('transaction_id', ''),
                organization_id=org_id,
                branch_id=branch_id,
                created_by=request.user,
                completed_by=request.user,
                status='completed'
            )
            
            items = data.get('items', [])
            print(f"DEBUG: create_sale - Processing {len(items)} items")
            
            for item_data in items:
                product = get_object_or_404(Product, id=item_data['medicine_id'])
                batch_info = item_data.get('batch_info', [])
                quantity = item_data['quantity']
                
                print(f"DEBUG: create_sale - Processing {product.name}, quantity: {quantity}")
                print(f"DEBUG: create_sale - Batch info: {batch_info}")
                
                SaleItem.objects.create(
                    sale=sale,
                    product=product,
                    quantity=quantity,
                    unit_price=item_data['price'],
                    batch_number=item_data.get('batch', ''),
                    allocated_batches=batch_info
                )
                
                # Reduce stock - handle both allocated batches and FIFO fallback
                if batch_info:
                    # Check if allocated quantity matches sale quantity
                    total_allocated = sum(batch['allocated_quantity'] for batch in batch_info)
                    print(f"DEBUG: create_sale - Total allocated: {total_allocated}, Sale quantity: {quantity}")
                    
                    if total_allocated != quantity:
                        print(f"DEBUG: create_sale - Allocation mismatch! Using FIFO for {product.name}")
                        # Use FIFO fallback when allocation doesn't match
                        inventory_items = InventoryItem.objects.filter(
                            product_id=product.id,
                            branch_id=branch_id,
                            quantity__gt=0,
                            is_active=True
                        ).order_by('expiry_date', 'created_at')
                        
                        remaining_quantity = quantity
                        for item in inventory_items:
                            if remaining_quantity <= 0:
                                break
                            
                            allocated_quantity = min(item.quantity, remaining_quantity)
                            print(f"DEBUG: create_sale - FIFO reducing {allocated_quantity} from batch {item.batch_number}")
                            
                            if item.quantity >= allocated_quantity:
                                item.quantity -= allocated_quantity
                                item.save()
                                remaining_quantity -= allocated_quantity
                                print(f"DEBUG: create_sale - FIFO reduced. New qty: {item.quantity}, remaining: {remaining_quantity}")
                            else:
                                raise ValueError(f"Insufficient stock in batch {item.batch_number}")
                        
                        if remaining_quantity > 0:
                            raise ValueError(f"Insufficient total stock for {product.name}")
                    else:
                        print(f"DEBUG: create_sale - Using allocated batches for {product.name}")
                        # Use allocated batches
                        for batch in batch_info:
                            inventory_item = get_object_or_404(InventoryItem, id=batch['inventory_item_id'])
                            allocated_qty = batch['allocated_quantity']
                            print(f"DEBUG: create_sale - Reducing {allocated_qty} from batch {batch['batch_number']} (current: {inventory_item.quantity})")

                            if inventory_item.quantity >= allocated_qty:
                                inventory_item.quantity -= allocated_qty
                                inventory_item.save()
                                print(f"DEBUG: create_sale - Stock reduced. New quantity: {inventory_item.quantity}")
                            else:
                                print(f"DEBUG: create_sale - ERROR - Insufficient stock")
                                raise ValueError(f"Insufficient stock in batch {batch['batch_number']}")
                else:
                    # FIFO fallback if no batch info
                    print(f"DEBUG: create_sale - No batch info for {product.name}, using FIFO allocation")
                    inventory_items = InventoryItem.objects.filter(
                        product_id=product.id,
                        branch_id=branch_id,
                        quantity__gt=0,
                        is_active=True
                    ).order_by('expiry_date', 'created_at')
                    
                    print(f"DEBUG: create_sale - Found {inventory_items.count()} inventory items for FIFO")
                    remaining_quantity = quantity
                    
                    for item in inventory_items:
                        if remaining_quantity <= 0:
                            break
                        
                        allocated_quantity = min(item.quantity, remaining_quantity)
                        print(f"DEBUG: create_sale - FIFO reducing {allocated_quantity} from batch {item.batch_number} (current: {item.quantity})")
                        
                        if item.quantity >= allocated_quantity:
                            item.quantity -= allocated_quantity
                            item.save()
                            remaining_quantity -= allocated_quantity
                            print(f"DEBUG: create_sale - FIFO reduced. New qty: {item.quantity}, remaining: {remaining_quantity}")
                        else:
                            print(f"DEBUG: create_sale - FIFO ERROR - Insufficient stock")
                            raise ValueError(f"Insufficient stock in batch {item.batch_number}")
                    
                    if remaining_quantity > 0:
                        print(f"DEBUG: create_sale - FIFO ERROR - Could not allocate all stock. Remaining: {remaining_quantity}")
                        raise ValueError(f"Insufficient total stock for {product.name}")
                
                print(f"DEBUG: create_sale - Completed stock reduction for {product.name}")
            
            # Handle split payments or single payment
            split_payments = data.get('split_payments')
            if split_payments and len(split_payments) > 0:
                # Create multiple payment records for split payments
                for split_payment in split_payments:
                    if split_payment.get('amount') and float(split_payment['amount']) > 0:
                        Payment.objects.create(
                            sale=sale,
                            amount=float(split_payment['amount']),
                            payment_method=split_payment.get('method', 'cash'),
                            reference_number=split_payment.get('transaction_id', ''),
                            received_by=request.user
                        )
            elif paid_amount > 0:
                # Single payment record
                Payment.objects.create(
                    sale=sale,
                    amount=paid_amount,
                    payment_method=data.get('payment_method', 'cash'),
                    reference_number=data.get('transaction_id', ''),
                    received_by=request.user
                )
            
            # Generate receipt data with POS settings
            organization = Organization.objects.get(id=org_id) if org_id else None
            branch = sale.branch
            
            # Get POS settings for receipt
            try:
                pos_settings = POSSettings.objects.get(organization_id=org_id, branch_id=branch_id)
                business_name = pos_settings.business_name or (organization.name if organization else '')
                business_address = pos_settings.business_address or getattr(organization, 'address', '')
                business_phone = pos_settings.business_phone or getattr(organization, 'phone', '')
                business_email = pos_settings.business_email or getattr(organization, 'email', '')
                receipt_footer = pos_settings.receipt_footer or 'Thank you for your business!'
                receipt_logo = request.build_absolute_uri(pos_settings.receipt_logo.url) if pos_settings.receipt_logo else None
                tax_rate = pos_settings.tax_rate
            except POSSettings.DoesNotExist:
                business_name = organization.name if organization else ''
                business_address = getattr(organization, 'address', '')
                business_phone = getattr(organization, 'phone', '')
                business_email = getattr(organization, 'email', '')
                receipt_footer = 'Thank you for your business!'
                receipt_logo = None
                tax_rate = 13
            
            receipt_data = {
                'organization': {
                    'name': business_name,
                    'address': business_address,
                    'phone': business_phone,
                    'email': business_email
                },
                'branch': {
                    'name': branch.name if branch else '',
                    'address': getattr(branch, 'address', ''),
                    'phone': getattr(branch, 'phone', '')
                },
                'settings': {
                    'receipt_footer': receipt_footer,
                    'receipt_logo': receipt_logo,
                    'tax_rate': tax_rate
                },
                'sale': {
                    'sale_number': sale.sale_number,
                    'sale_date': sale.created_at.strftime('%Y-%m-%d %I:%M %p'),
                    'cashier': request.user.get_full_name() or request.user.username
                },
                'patient': {
                    'name': sale.patient_name,
                    'patient_id': sale.patient.patient_id if sale.patient else '',
                    'age': sale.patient_age,
                    'phone': sale.patient_phone,
                    'gender': sale.patient_gender
                },
                'items': [{
                    'name': item.product.name,
                    'quantity': item.quantity,
                    'unit_price': float(item.unit_price),
                    'total': float(item.quantity * item.unit_price),
                    'batch': item.batch_number
                } for item in sale.items.all()],
                'totals': {
                    'subtotal': float(sale.subtotal),
                    'tax': float(sale.tax_amount),
                    'discount': float(sale.discount_amount),
                    'total': float(sale.total_amount),
                    'paid': float(sale.amount_paid),
                    'credit': float(sale.credit_amount),
                    'change': float(sale.change_amount)
                },
                'payment_method': sale.payment_method
            }
            
            print(f"DEBUG: create_sale - Sale completed successfully: {sale.sale_number}")
            return Response({
                'success': True,
                'sale_id': sale.id,
                'sale_number': sale.sale_number,
                'message': 'Sale created successfully',
                'receipt': receipt_data
            })
            
    except Exception as e:
        print(f"DEBUG: create_sale ERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        return Response({'error': str(e)}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_pending_bills(request):
    """Get all pending bills for current branch."""
    try:
        branch_id = request.user.branch_id
        if not branch_id:
            return Response({'error': 'User not assigned to any branch'}, status=400)
        
        pending_sales = Sale.objects.filter(
            branch_id=branch_id,
            organization_id=request.user.organization_id,
            status='pending'
        ).order_by('-created_at')
        
        bills_data = []
        for sale in pending_sales:
            bills_data.append({
                'id': sale.id,
                'sale_number': sale.sale_number,
                'patientName': sale.patient_name,
                'patientId': sale.patient.patient_id if sale.patient else '',
                'patientAge': sale.patient_age,
                'patientPhone': sale.patient_phone,
                'patientGender': sale.patient_gender,
                'items': [
                    {
                        'medicine_id': item.product.id,
                        'name': item.product.name,
                        'quantity': item.quantity,
                        'price': float(item.unit_price),
                        'batch': item.batch_number,
                        'batch_info': item.allocated_batches
                    } for item in sale.items.all()
                ],
                'subtotal': float(sale.subtotal),
                'total': float(sale.total_amount),
                'discountAmount': float(sale.discount_amount),
                'taxAmount': float(sale.tax_amount),
                'paymentMethod': sale.payment_method,
                'createdAt': sale.created_at.strftime('%Y-%m-%d %I:%M %p'),
                'createdBy': sale.created_by.get_full_name() if sale.created_by else 'Unknown'
            })
        
        return Response(bills_data)
        
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_sales(request):
    """Get sales list for current user's branch."""
    try:
        branch_id = request.user.branch_id
        if not branch_id:
            return Response({'error': 'User not assigned to any branch'}, status=400)

        # Only get completed sales
        sales_query = Sale.objects.filter(
            branch_id=branch_id,
            organization_id=request.user.organization_id,
            status='completed'
        )

        # Filter by patient_id if provided
        patient_id = request.GET.get('patient_id')
        if patient_id:
            sales_query = sales_query.filter(patient_id=patient_id)

        sales = sales_query.order_by('-created_at')
        
        sales_data = []
        for sale in sales:
            # Get payment records
            payments = sale.payments.all()
            payment_details = [{
                'amount': float(payment.amount),
                'method': payment.payment_method,
                'reference': payment.reference_number,
                'date': payment.payment_date.strftime('%Y-%m-%d %I:%M %p'),
                'receivedBy': payment.received_by.get_full_name() if payment.received_by else 'Unknown'
            } for payment in payments]
            
            # Calculate payment breakdown by method
            payment_summary = {
                'cash': sum(p['amount'] for p in payment_details if p['method'] == 'cash'),
                'online': sum(p['amount'] for p in payment_details if p['method'] == 'online'),
                'card': sum(p['amount'] for p in payment_details if p['method'] == 'card')
            }
            
            # Create payment breakdown for display
            payment_breakdown = []
            if payment_summary['cash'] > 0:
                payment_breakdown.append(f"Cash: NPR {payment_summary['cash']:.2f}")
            if payment_summary['online'] > 0:
                payment_breakdown.append(f"Online: NPR {payment_summary['online']:.2f}")
            if payment_summary['card'] > 0:
                payment_breakdown.append(f"Card: NPR {payment_summary['card']:.2f}")
            if sale.credit_amount > 0:
                payment_breakdown.append(f"Credit: NPR {sale.credit_amount:.2f}")
            
            # Determine if it's a split payment
            payment_methods_used = len([method for method, amount in payment_summary.items() if amount > 0])
            is_split_payment = payment_methods_used > 1 or (payment_methods_used >= 1 and sale.credit_amount > 0)
            
            sales_data.append({
                'id': sale.sale_number,
                'patientName': sale.patient_name,
                'patientId': sale.patient.patient_id if sale.patient else '',
                'patientAge': sale.patient_age,
                'patientPhone': sale.patient_phone,
                'patientGender': sale.patient_gender,
                'items': [
                    {
                        'name': item.product.name,
                        'quantity': item.quantity,
                        'price': float(item.unit_price),
                        'batch': item.batch_number,
                        'total': float(item.line_total)
                    } for item in sale.items.all()
                ],
                'subtotal': float(sale.subtotal),
                'total': float(sale.total_amount),
                'discountAmount': float(sale.discount_amount),
                'taxAmount': float(sale.tax_amount),
                'paymentMethod': 'Split Payment' if is_split_payment else sale.payment_method,
                'paidAmount': float(sale.amount_paid),
                'creditAmount': float(sale.credit_amount),
                'changeAmount': float(sale.change_amount),
                'completedAt': sale.created_at.strftime('%Y-%m-%d %I:%M %p'),
                'completedBy': sale.completed_by.get_full_name() if sale.completed_by else 'Unknown',
                'payments': payment_details,
                'paymentSummary': payment_summary,
                'paymentBreakdown': payment_breakdown,
                'isSplitPayment': is_split_payment,
                'status': 'credit' if sale.credit_amount > 0 else 'completed'
            })
        
        return Response(sales_data)
        
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def patient_credit_history(request):
    """Get credit history for a specific patient."""
    try:
        patient_id = request.GET.get('patient_id')
        if not patient_id:
            return Response({'error': 'patient_id parameter is required'}, status=400)

        # Get sales with outstanding credit for this patient
        credit_sales = Sale.objects.filter(
            patient_id=patient_id,
            organization_id=request.user.organization_id,
            credit_amount__gt=0,
            status='completed'
        ).select_related('patient').order_by('-created_at')

        credit_data = []
        for sale in credit_sales:
            credit_data.append({
                'id': sale.id,
                'sale_number': sale.sale_number,
                'total_amount': float(sale.total_amount),
                'amount_paid': float(sale.amount_paid),
                'credit_amount': float(sale.credit_amount),
                'created_at': sale.created_at.isoformat(),
                'payment_method': sale.payment_method,
                'transaction_id': sale.transaction_id,
                'items': [{
                    'product_name': item.product.name,
                    'quantity': item.quantity,
                    'unit_price': float(item.unit_price),
                    'total': float(item.quantity * item.unit_price)
                } for item in sale.items.all()]
            })

        return Response(credit_data)

    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_sale_detail(request, sale_id):
    """Get detailed sale information."""
    try:
        sale = get_object_or_404(Sale, sale_number=sale_id, organization_id=request.user.organization_id)
        
        # Get all payment records for this sale
        payments = sale.payments.all().order_by('payment_date')
        payment_details = []
        
        for payment in payments:
            payment_details.append({
                'id': payment.id,
                'amount': float(payment.amount),
                'method': payment.payment_method,
                'reference': payment.reference_number or '',
                'date': payment.payment_date.strftime('%Y-%m-%d %I:%M %p'),
                'receivedBy': payment.received_by.get_full_name() if payment.received_by else 'Unknown'
            })
        
        # If no payment records exist, create from sale data
        if not payment_details and sale.amount_paid > 0:
            payment_details.append({
                'id': 0,
                'amount': float(sale.amount_paid),
                'method': sale.payment_method,
                'reference': sale.transaction_id or '',
                'date': sale.created_at.strftime('%Y-%m-%d %I:%M %p'),
                'receivedBy': sale.completed_by.get_full_name() if sale.completed_by else 'Unknown'
            })
        
        sale_data = {
            'id': sale.sale_number,
            'patientName': sale.patient_name,
            'patientId': sale.patient.patient_id if sale.patient else '',
            'patientAge': sale.patient_age,
            'patientPhone': sale.patient_phone,
            'patientGender': sale.patient_gender,
            'items': [
                {
                    'name': item.product.name,
                    'quantity': item.quantity,
                    'price': float(item.unit_price),
                    'batch': item.batch_number,
                    'total': float(item.quantity * item.unit_price)
                } for item in sale.items.all()
            ],
            'subtotal': float(sale.subtotal),
            'total': float(sale.total_amount),
            'discountAmount': float(sale.discount_amount),
            'taxAmount': float(sale.tax_amount),
            'paymentMethod': sale.payment_method,
            'paidAmount': float(sale.amount_paid),
            'creditAmount': float(sale.credit_amount),
            'completedAt': sale.created_at.strftime('%Y-%m-%d %I:%M %p'),
            'completedBy': sale.completed_by.get_full_name() if sale.completed_by else 'Unknown',
            'status': 'credit' if sale.credit_amount > 0 else 'completed',
            'payments': payment_details,
            'totalPayments': len(payment_details),
            'paymentSummary': {
                'cash': sum(p['amount'] for p in payment_details if p['method'] == 'cash'),
                'online': sum(p['amount'] for p in payment_details if p['method'] == 'online'),
                'card': sum(p['amount'] for p in payment_details if p['method'] == 'card')
            },
            'paymentBreakdown': [p for p in [
                {'method': 'Cash', 'amount': sum(p['amount'] for p in payment_details if p['method'] == 'cash')},
                {'method': 'Online', 'amount': sum(p['amount'] for p in payment_details if p['method'] == 'online')},
                {'method': 'Card', 'amount': sum(p['amount'] for p in payment_details if p['method'] == 'card')}
            ] if p['amount'] > 0]
        }
        
        return Response(sale_data)
        
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def delete_sale(request, sale_id):
    """Delete a sale (admin only)."""
    try:
        sale = get_object_or_404(Sale, sale_number=sale_id, organization_id=request.user.organization_id)
        
        # Only allow deletion if user has admin permissions
        if not request.user.is_staff:
            return Response({'error': 'Permission denied'}, status=403)
        
        # Restore inventory quantities
        with transaction.atomic():
            for item in sale.items.all():
                if item.allocated_batches:
                    for batch in item.allocated_batches:
                        try:
                            inventory_item = InventoryItem.objects.get(id=batch['inventory_item_id'])
                            inventory_item.quantity += batch['allocated_quantity']
                            inventory_item.save()
                        except InventoryItem.DoesNotExist:
                            pass
            
            sale.delete()
        
        return Response({'success': True, 'message': 'Sale deleted successfully'})
        
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def pos_stats(request):
    """Get POS statistics."""
    try:
        branch_id = request.user.branch_id
        org_id = request.user.organization_id
        
        today = timezone.now().date()
        
        # Today's sales
        today_sales = Sale.objects.filter(
            branch_id=branch_id,
            organization_id=org_id,
            created_at__date=today,
            status='completed'
        )
        
        # Total sales
        total_sales = Sale.objects.filter(
            branch_id=branch_id,
            organization_id=org_id,
            status='completed'
        )
        
        # Credit sales
        credit_sales = Sale.objects.filter(
            branch_id=branch_id,
            organization_id=org_id,
            credit_amount__gt=0,
            status='completed'
        )
        
        return Response({
            'today_sales_count': today_sales.count(),
            'today_sales_amount': sum(sale.total_amount for sale in today_sales),
            'total_sales_count': total_sales.count(),
            'total_sales_amount': sum(sale.total_amount for sale in total_sales),
            'credit_sales_count': credit_sales.count(),
            'credit_amount': sum(sale.credit_amount for sale in credit_sales),
        })
        
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def generate_receipt(request, sale_id):
    """Generate receipt data for a completed sale."""
    try:
        # Try to get by ID first, then by sale_number
        try:
            if sale_id.isdigit():
                sale = Sale.objects.get(id=sale_id, organization_id=request.user.organization_id)
            else:
                sale = Sale.objects.get(sale_number=sale_id, organization_id=request.user.organization_id)
        except Sale.DoesNotExist:
            return Response({'error': 'Sale not found'}, status=404)
        
        # Allow receipt generation for both completed and pending sales
        # if sale.status != 'completed':
        #     return Response({'error': 'Receipt can only be generated for completed sales'}, status=400)
        
        # Get organization and branch details with POS settings
        organization = sale.organization
        branch = sale.branch
        
        # Get POS settings for receipt
        try:
            pos_settings = POSSettings.objects.get(organization_id=sale.organization_id, branch_id=sale.branch_id)
            business_name = pos_settings.business_name or organization.name
            business_address = pos_settings.business_address or getattr(organization, 'address', '')
            business_phone = pos_settings.business_phone or getattr(organization, 'phone', '')
            business_email = pos_settings.business_email or getattr(organization, 'email', '')
            receipt_footer = pos_settings.receipt_footer or 'Thank you for your business!'
            receipt_logo = request.build_absolute_uri(pos_settings.receipt_logo.url) if pos_settings.receipt_logo else None
            tax_rate = pos_settings.tax_rate
        except POSSettings.DoesNotExist:
            business_name = organization.name
            business_address = getattr(organization, 'address', '')
            business_phone = getattr(organization, 'phone', '')
            business_email = getattr(organization, 'email', '')
            receipt_footer = 'Thank you for your business!'
            receipt_logo = None
            tax_rate = 13
        
        # Prepare receipt data
        receipt_data = {
            'organization': {
                'name': business_name,
                'address': business_address,
                'phone': business_phone,
                'email': business_email
            },
            'branch': {
                'name': branch.name if branch else ''
            },
            'settings': {
                'receipt_footer': receipt_footer,
                'receipt_logo': receipt_logo,
                'tax_rate': tax_rate
            },
            'sale': {
                'sale_number': sale.sale_number,
                'sale_date': sale.created_at.strftime('%Y-%m-%d %I:%M %p'),
                'cashier': sale.created_by.get_full_name() if sale.created_by else 'Unknown'
            },
            'patient': {
                'name': sale.patient_name,
                'patient_id': sale.patient.patient_id if sale.patient else '',
                'age': sale.patient_age,
                'phone': sale.patient_phone,
                'gender': sale.patient_gender
            },
            'items': [],
            'totals': {
                'subtotal': float(sale.subtotal),
                'discount': float(sale.discount_amount),
                'tax': float(sale.tax_amount),
                'total': float(sale.total_amount),
                'paid': float(sale.amount_paid),
                'credit': float(sale.credit_amount),
                'change': float(sale.change_amount)
            },
            'receipt_footer': receipt_footer,
            'payments': []
        }
        
        # Add items
        for item in sale.items.all():
            receipt_data['items'].append({
                'name': item.product.name,
                'quantity': item.quantity,
                'unit_price': float(item.unit_price),
                'discount': float(item.discount_amount),
                'total': float(item.line_total),
                'batch': item.batch_number
            })
        
        # Add payment details
        for payment in sale.payments.all():
            receipt_data['payments'].append({
                'method': payment.payment_method,
                'amount': float(payment.amount),
                'reference': payment.reference_number,
                'date': payment.payment_date.strftime('%Y-%m-%d %I:%M %p')
            })
        
        return Response(receipt_data)
        
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def process_credit_payment(request, sale_id):
    """Process payment for a credit sale."""
    try:
        with transaction.atomic():
            sale = get_object_or_404(Sale, sale_number=sale_id, organization_id=request.user.organization_id)
            
            if sale.credit_amount <= 0:
                return Response({'error': 'No outstanding credit for this sale'}, status=400)
            
            payment_amount = float(request.data.get('amount', 0))
            payment_method = request.data.get('payment_method', 'cash')
            reference_number = request.data.get('reference_number', '')
            
            if payment_amount <= 0:
                return Response({'error': 'Payment amount must be greater than 0'}, status=400)
            
            if payment_amount > sale.credit_amount:
                return Response({'error': 'Payment amount cannot exceed credit amount'}, status=400)
            
            # Create payment record
            Payment.objects.create(
                sale=sale,
                amount=payment_amount,
                payment_method=payment_method,
                reference_number=reference_number,
                received_by=request.user
            )
            
            # Update sale amounts
            sale.amount_paid += payment_amount
            sale.credit_amount -= payment_amount
            sale.save()
            
            return Response({
                'success': True,
                'message': 'Payment processed successfully',
                'remaining_credit': float(sale.credit_amount),
                'total_paid': float(sale.amount_paid)
            })
            
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def validate_stock_before_sale(request):
    """Validate stock availability before creating sale."""
    try:
        items = request.data.get('items', [])
        branch_id = request.data.get('branch_id') or request.user.branch_id
        
        validation_results = []
        all_valid = True
        
        for item in items:
            medicine_id = item.get('medicine_id')
            required_quantity = int(item.get('quantity', 0))
            
            # Get available stock
            available_stock = InventoryItem.objects.filter(
                product_id=medicine_id,
                branch_id=branch_id,
                quantity__gt=0,
                is_active=True
            ).aggregate(total=models.Sum('quantity'))['total'] or 0
            
            is_valid = available_stock >= required_quantity
            if not is_valid:
                all_valid = False
            
            validation_results.append({
                'medicine_id': medicine_id,
                'required_quantity': required_quantity,
                'available_stock': available_stock,
                'is_valid': is_valid,
                'shortage': max(0, required_quantity - available_stock)
            })
        
        return Response({
            'all_valid': all_valid,
            'items': validation_results
        })
        
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def pharmacy_dashboard_stats(request):
    """Get pharmacy dashboard statistics for pharmacy owner."""
    try:
        print("DEBUG: Starting pharmacy_dashboard_stats")
        org_id = getattr(request.user, 'organization_id', None)
        print(f"DEBUG: org_id = {org_id}")
        if not org_id:
            print("DEBUG: No organization_id found")
            return Response({'error': 'User not associated with an organization'}, status=400)

        # Get date filter from request
        date_filter = request.GET.get('date_filter', 'today')
        print(f"DEBUG: date_filter = {date_filter}")

        # Calculate date range based on filter
        today = timezone.now().date()
        print(f"DEBUG: today = {today}")
        if date_filter == 'today':
            start_date = today
            end_date = today
        elif date_filter == 'week':
            start_date = today - timedelta(days=7)
            end_date = today
        elif date_filter == 'month':
            start_date = today - timedelta(days=30)
            end_date = today
        elif date_filter == 'year':
            start_date = today - timedelta(days=365)
            end_date = today
        else:
            start_date = today
            end_date = today
        print(f"DEBUG: date range = {start_date} to {end_date}")

        # Get branch filter
        branch_id = request.GET.get('branch_id')
        print(f"DEBUG: branch_id = {branch_id}")
        branch_filter = Q()
        if branch_id and branch_id != 'all':
            branch_filter = Q(branch_id=branch_id)
        print(f"DEBUG: branch_filter = {branch_filter}")

        # Total Sales
        print("DEBUG: Calculating total sales")
        total_sales_query = Sale.objects.filter(
            organization_id=org_id,
            created_at__date__gte=start_date,
            created_at__date__lte=end_date,
            status='completed'
        ).filter(branch_filter)
        print(f"DEBUG: total_sales_query count = {total_sales_query.count()}")
        total_sales = total_sales_query.aggregate(
            total=Sum('total_amount')
        )['total'] or 0
        print(f"DEBUG: total_sales = {total_sales}")

        # Patient Credit (outstanding credit from sales)
        print("DEBUG: Calculating patient credit")
        patient_credit_query = Sale.objects.filter(
            organization_id=org_id,
            credit_amount__gt=0,
            status='completed'
        ).filter(branch_filter)
        print(f"DEBUG: patient_credit_query count = {patient_credit_query.count()}")
        patient_credit = patient_credit_query.aggregate(
            total=Sum('credit_amount')
        )['total'] or 0
        print(f"DEBUG: patient_credit = {patient_credit}")

        # Supplier Credit (from bulk orders - calculate outstanding payments)
        print("DEBUG: Calculating supplier credit")
        from inventory.models import BulkOrder
        supplier_credit_query = BulkOrder.objects.filter(
            buyer_organization_id=org_id,
            status__in=['confirmed', 'shipped', 'delivered'],
            remaining_amount__gt=0
        ).filter(branch_filter)
        print(f"DEBUG: supplier_credit_query count = {supplier_credit_query.count()}")
        supplier_credit = supplier_credit_query.aggregate(
            total=Sum('remaining_amount')
        )['total'] or 0
        print(f"DEBUG: supplier_credit = {supplier_credit}")

        # Critical Stock (items below minimum stock level)
        print("DEBUG: Calculating critical stock")
        from inventory.models import InventoryItem
        critical_stock_query = InventoryItem.objects.filter(
            branch__organization_id=org_id,
            is_active=True
        ).filter(branch_filter).filter(
            Q(quantity__lte=F('min_stock_level')) | Q(min_stock_level__isnull=True, quantity__lte=10)
        )
        critical_stock_items = critical_stock_query.count()
        print(f"DEBUG: critical_stock_items = {critical_stock_items}")

        # Expiring Soon (items expiring within 30 days)
        print("DEBUG: Calculating expiring items")
        expiring_soon_query = InventoryItem.objects.filter(
            branch__organization_id=org_id,
            expiry_date__isnull=False,
            expiry_date__lte=today + timedelta(days=30),
            expiry_date__gte=today,
            is_active=True,
            quantity__gt=0
        ).filter(branch_filter)
        expiring_soon = expiring_soon_query.count()
        print(f"DEBUG: expiring_soon = {expiring_soon}")

        result = {
            'totalSales': float(total_sales),
            'patientCredit': float(patient_credit),
            'supplierCredit': float(supplier_credit),
            'criticalStock': critical_stock_items,
            'expiringItems': expiring_soon
        }
        print(f"DEBUG: Final result = {result}")
        return Response(result)

    except Exception as e:
        print(f"DEBUG: Exception occurred: {str(e)}")
        import traceback
        traceback.print_exc()
        return Response({'error': str(e)}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def pharmacy_sales_chart(request):
    """Get sales chart data for pharmacy dashboard."""
    try:
        org_id = getattr(request.user, 'organization_id', None)
        if not org_id:
            return Response({'error': 'User not associated with an organization'}, status=400)

        date_filter = request.GET.get('date_filter', 'today')
        branch_id = request.GET.get('branch_id')

        branch_filter = Q()
        if branch_id and branch_id != 'all':
            branch_filter = Q(branch_id=branch_id)

        today = timezone.now().date()

        if date_filter == 'today':
            # Hourly data for today
            sales_data = []
            for hour in range(9, 20):  # 9 AM to 7 PM
                hour_start = timezone.make_aware(datetime.combine(today, datetime.min.time().replace(hour=hour)))
                hour_end = timezone.make_aware(datetime.combine(today, datetime.min.time().replace(hour=hour+1)))

                hour_sales = Sale.objects.filter(
                    organization_id=org_id,
                    created_at__gte=hour_start,
                    created_at__lt=hour_end,
                    status='completed'
                ).filter(branch_filter).aggregate(
                    sales=Sum('total_amount'),
                    leads=Count('id')
                )

                sales_data.append({
                    'name': f'{hour}:00',
                    'sales': float(hour_sales['sales'] or 0),
                    'leads': hour_sales['leads'] or 0
                })
        else:
            # Daily data for the period
            days_count = 7 if date_filter == 'week' else 30 if date_filter == 'month' else 365
            start_date = today - timedelta(days=days_count)

            sales_data = []
            for i in range(days_count):
                current_date = start_date + timedelta(days=i)

                day_sales = Sale.objects.filter(
                    organization_id=org_id,
                    created_at__date=current_date,
                    status='completed'
                ).filter(branch_filter).aggregate(
                    sales=Sum('total_amount'),
                    leads=Count('id')
                )

                sales_data.append({
                    'name': current_date.strftime('%a'),  # Mon, Tue, etc.
                    'sales': float(day_sales['sales'] or 0),
                    'leads': day_sales['leads'] or 0
                })

        return Response(sales_data)

    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def pharmacy_stock_categories(request):
    """Get stock categories pie chart data."""
    try:
        print("DEBUG: Starting pharmacy_stock_categories")
        org_id = getattr(request.user, 'organization_id', None)
        print(f"DEBUG: org_id = {org_id}")
        if not org_id:
            print("DEBUG: No organization_id found")
            return Response({'error': 'User not associated with an organization'}, status=400)

        branch_id = request.GET.get('branch_id')
        print(f"DEBUG: branch_id = {branch_id}")
        branch_filter = Q()
        if branch_id and branch_id != 'all':
            branch_filter = Q(branch__organization_id=org_id, branch_id=branch_id)
        else:
            branch_filter = Q(branch__organization_id=org_id)
        print(f"DEBUG: branch_filter = {branch_filter}")

        from inventory.models import InventoryItem
        print("DEBUG: Importing InventoryItem model")

        # Group by product category and sum quantities
        print("DEBUG: Building category_data query")
        category_data_query = InventoryItem.objects.filter(
            branch_filter,
            is_active=True,
            quantity__gt=0
        ).values(
            'product__category__name'
        ).annotate(
            total_stock=Sum('quantity')
        ).order_by('-total_stock')
        print(f"DEBUG: category_data_query SQL = {category_data_query.query}")
        category_data = list(category_data_query)
        print(f"DEBUG: category_data = {category_data}")

        # Format for pie chart (limit to top categories)
        medicine_data = []
        colors = ['#8884d8', '#82ca9d', '#ffc658', '#ff7c7c', '#8dd1e1']

        for i, category in enumerate(category_data[:5]):  # Top 5 categories
            category_name = category['product__category__name'] or 'Uncategorized'
            medicine_data.append({
                'name': category_name,
                'value': category['total_stock'],
                'color': colors[i % len(colors)]
            })

        print(f"DEBUG: medicine_data after processing = {medicine_data}")

        # If no categories, provide default data
        if not medicine_data:
            print("DEBUG: No category data found, using defaults")
            medicine_data = [
                {'name': 'Prescription', 'value': 2847, 'color': '#8884d8'},
                {'name': 'OTC', 'value': 1234, 'color': '#82ca9d'},
                {'name': 'Supplies', 'value': 567, 'color': '#ffc658'}
            ]

        print(f"DEBUG: Final medicine_data = {medicine_data}")
        return Response(medicine_data)

    except Exception as e:
        print(f"DEBUG: Exception in pharmacy_stock_categories: {str(e)}")
        import traceback
        traceback.print_exc()
        return Response({'error': str(e)}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def pharmacy_recent_activities(request):
    """Get recent activities for pharmacy dashboard."""
    try:
        org_id = request.user.organization_id
        if not org_id:
            return Response({'error': 'User not associated with an organization'}, status=400)

        branch_id = request.GET.get('branch_id')
        branch_filter = Q()
        if branch_id and branch_id != 'all':
            branch_filter = Q(branch_id=branch_id)

        activities = []

        # Get recent sales (last 5)
        recent_sales = Sale.objects.filter(
            organization_id=org_id,
            status='completed'
        ).filter(branch_filter).select_related('created_by').order_by('-created_at')[:5]

        for sale in recent_sales:
            # Calculate time ago
            time_diff = timezone.now() - sale.created_at
            if time_diff.days > 0:
                time_ago = f"{time_diff.days} days ago"
            elif time_diff.seconds // 3600 > 0:
                time_ago = f"{time_diff.seconds // 3600} hours ago"
            elif time_diff.seconds // 60 > 0:
                time_ago = f"{time_diff.seconds // 60} mins ago"
            else:
                time_ago = "Just now"

            activities.append({
                'id': f'sale_{sale.id}',
                'type': 'sale',
                'title': 'Sale completed',
                'description': f'₹{float(sale.total_amount):.0f} • {time_ago}',
                'amount': float(sale.total_amount),
                'timestamp': sale.created_at.isoformat(),
                'status': 'success'
            })

        # Get recent stock updates (from inventory items created/modified recently)
        from inventory.models import InventoryItem
        recent_stock_updates = InventoryItem.objects.filter(
            branch__organization_id=org_id
        ).filter(branch_filter).select_related('product').order_by('-updated_at')[:3]

        for item in recent_stock_updates:
            # Calculate time ago
            time_diff = timezone.now() - item.updated_at
            if time_diff.days > 0:
                time_ago = f"{time_diff.days} days ago"
            elif time_diff.seconds // 3600 > 0:
                time_ago = f"{time_diff.seconds // 3600} hours ago"
            elif time_diff.seconds // 60 > 0:
                time_ago = f"{time_diff.seconds // 60} mins ago"
            else:
                time_ago = "Just now"

            activities.append({
                'id': f'stock_{item.id}',
                'type': 'stock',
                'title': 'Stock updated',
                'description': f'{item.product.name} • {time_ago}',
                'timestamp': item.updated_at.isoformat(),
                'status': 'info'
            })

        # Get low stock alerts
        low_stock_items = InventoryItem.objects.filter(
            branch__organization_id=org_id,
            is_active=True
        ).filter(branch_filter).filter(
            Q(quantity__lte=F('min_stock_level')) | Q(min_stock_level__isnull=True, quantity__lte=10)
        ).select_related('product').order_by('quantity')[:2]

        for item in low_stock_items:
            activities.append({
                'id': f'alert_{item.id}',
                'type': 'alert',
                'title': 'Low stock alert',
                'description': f'{item.product.name} • {item.quantity} units remaining',
                'timestamp': timezone.now().isoformat(),
                'status': 'warning'
            })

        # Sort activities by timestamp (most recent first)
        activities.sort(key=lambda x: x['timestamp'], reverse=True)

        # Return only the 8 most recent activities
        return Response(activities[:8])

    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def pos_settings(request):
    """Get or save POS settings for current branch."""
    try:
        branch_id = request.user.branch_id
        org_id = request.user.organization_id
        
        if not org_id:
            return Response({'error': 'User not assigned to organization'}, status=400)
        
        # For pharmacy owners without branch assignment, use the first branch or create default
        if not branch_id and request.user.role == 'pharmacy_owner':
            from organizations.models import Branch
            try:
                # Get the first branch of the organization
                branch = Branch.objects.filter(organization_id=org_id).first()
                if branch:
                    branch_id = branch.id
                else:
                    return Response({'error': 'No branches found for organization'}, status=400)
            except Exception:
                return Response({'error': 'Unable to determine branch'}, status=400)
        elif not branch_id:
            return Response({'error': 'User not assigned to branch'}, status=400)
        
        if request.method == 'GET':
            # Get existing settings or return defaults
            try:
                settings = POSSettings.objects.get(organization_id=org_id, branch_id=branch_id)
                # Get full URL for logo
                logo_url = None
                if settings.receipt_logo:
                    logo_url = request.build_absolute_uri(settings.receipt_logo.url)
                
                return Response({
                    'business_name': settings.business_name,
                    'business_address': settings.business_address,
                    'business_phone': settings.business_phone,
                    'business_email': settings.business_email,
                    'receipt_footer': settings.receipt_footer,
                    'receipt_logo': logo_url,
                    'tax_rate': float(settings.tax_rate),
                    'tax_inclusive': settings.tax_inclusive,
                    'payment_methods': settings.payment_methods
                })
            except POSSettings.DoesNotExist:
                return Response({
                    'business_name': '',
                    'business_address': '',
                    'business_phone': '',
                    'business_email': '',
                    'receipt_footer': '',
                    'receipt_logo': None,
                    'tax_rate': 13.0,
                    'tax_inclusive': False,
                    'payment_methods': ['cash', 'online']
                })
        
        elif request.method == 'POST':
            # Save or update settings
            data = request.data
            
            settings, created = POSSettings.objects.get_or_create(
                organization_id=org_id,
                branch_id=branch_id,
                defaults={'created_by': request.user}
            )
            
            # Update fields
            settings.business_name = data.get('business_name', settings.business_name)
            settings.business_address = data.get('business_address', settings.business_address)
            settings.business_phone = data.get('business_phone', settings.business_phone)
            settings.business_email = data.get('business_email', settings.business_email)
            settings.receipt_footer = data.get('receipt_footer', settings.receipt_footer)
            settings.tax_rate = float(data.get('tax_rate', settings.tax_rate))
            
            # Handle boolean conversion for tax_inclusive
            tax_inclusive_value = data.get('tax_inclusive', settings.tax_inclusive)
            if isinstance(tax_inclusive_value, str):
                settings.tax_inclusive = tax_inclusive_value.lower() == 'true'
            else:
                settings.tax_inclusive = bool(tax_inclusive_value)
            
            # Handle payment methods JSON
            payment_methods_value = data.get('payment_methods', settings.payment_methods)
            if isinstance(payment_methods_value, str):
                import json
                settings.payment_methods = json.loads(payment_methods_value)
            else:
                settings.payment_methods = payment_methods_value
            
            # Handle logo upload
            if 'receipt_logo' in request.FILES:
                settings.receipt_logo = request.FILES['receipt_logo']
            
            settings.save()
            
            return Response({
                'success': True,
                'message': 'Settings saved successfully'
            })
    
    except Exception as e:
        return Response({'error': str(e)}, status=500)