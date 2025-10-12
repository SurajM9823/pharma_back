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
        
        for item in inventory_items:
            if remaining_quantity <= 0:
                break
                
            allocated_quantity = min(item.quantity, remaining_quantity)
            allocations.append({
                'inventory_item_id': item.id,
                'batch_number': item.batch_number,
                'expiry_date': item.expiry_date.isoformat(),
                'allocated_quantity': allocated_quantity,
                'selling_price': float(item.selling_price or item.cost_price),
                'available_quantity': item.quantity
            })
            
            remaining_quantity -= allocated_quantity
        
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
                
                # Reduce stock for all items
                for sale_item in sale.items.all():
                    for batch in sale_item.allocated_batches:
                        inventory_item = get_object_or_404(InventoryItem, id=batch['inventory_item_id'])
                        allocated_qty = batch['allocated_quantity']
                        
                        if inventory_item.quantity >= allocated_qty:
                            inventory_item.quantity -= allocated_qty
                            inventory_item.save()
                        else:
                            raise ValueError(f"Insufficient stock in batch {batch['batch_number']}")
                
                # Create payment record
                if paid_amount > 0:
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
                    pos_settings = POSSettings.objects.get(organization_id=org_id, branch_id=branch_id)
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
        
        # Reduce stock
        for batch in batch_info:
            inventory_item = get_object_or_404(InventoryItem, id=batch['inventory_item_id'])
            allocated_qty = batch['allocated_quantity']
            
            if inventory_item.quantity >= allocated_qty:
                inventory_item.quantity -= allocated_qty
                inventory_item.save()
            else:
                raise ValueError(f"Insufficient stock in batch {batch['batch_number']}")
    
    # Create payment record
    if paid_amount > 0:
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
                
                for batch in batch_info:
                    inventory_item = get_object_or_404(InventoryItem, id=batch['inventory_item_id'])
                    allocated_qty = batch['allocated_quantity']
                    
                    if inventory_item.quantity >= allocated_qty:
                        inventory_item.quantity -= allocated_qty
                        inventory_item.save()
                    else:
                        raise ValueError(f"Insufficient stock in batch {batch['batch_number']}")
            
            if paid_amount > 0:
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
            
            return Response({
                'success': True,
                'sale_id': sale.id,
                'sale_number': sale.sale_number,
                'message': 'Sale created successfully',
                'receipt': receipt_data
            })
            
    except Exception as e:
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
        sales = Sale.objects.filter(
            branch_id=branch_id,
            organization_id=request.user.organization_id,
            status='completed'
        ).order_by('-created_at')
        
        sales_data = []
        for sale in sales:
            # Get payment records
            payments = sale.payments.all()
            payment_details = [{
                'amount': float(payment.amount),
                'method': payment.payment_method,
                'reference': payment.reference_number,
                'date': payment.payment_date.strftime('%Y-%m-%d %I:%M %p')
            } for payment in payments]
            
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
                'paymentMethod': sale.payment_method,
                'paidAmount': float(sale.amount_paid),
                'creditAmount': float(sale.credit_amount),
                'changeAmount': float(sale.change_amount),
                'completedAt': sale.created_at.strftime('%Y-%m-%d %I:%M %p'),
                'completedBy': sale.completed_by.get_full_name() if sale.completed_by else 'Unknown',
                'payments': payment_details,
                'status': 'credit' if sale.credit_amount > 0 else 'completed'
            })
        
        return Response(sales_data)
        
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_sale_detail(request, sale_id):
    """Get detailed sale information."""
    try:
        sale = get_object_or_404(Sale, sale_number=sale_id, organization_id=request.user.organization_id)
        
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
            'status': 'credit' if sale.credit_amount > 0 else 'completed'
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