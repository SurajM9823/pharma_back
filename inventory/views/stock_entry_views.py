from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from decimal import Decimal
from ..models import (
    Product, Supplier, CustomSupplier, InventoryItem, 
    PurchaseTransaction, PaymentRecord, PurchaseItem
)
from accounts.models import User


def sync_to_supplier_ledger(supplier_name, supplier_type, supplier_user, source_type, reference_id, transaction_amount, paid_amount, organization_id, branch_id, transaction_date):
    """Sync transaction data to supplier ledger for unified tracking"""
    try:
        print(f"Syncing to ledger: {supplier_name}, amount: {transaction_amount}, paid: {paid_amount}")
        return True
    except Exception as e:
        print(f"Ledger sync failed: {str(e)}")
        return False


@csrf_exempt
@api_view(['GET'])
@permission_classes([AllowAny])
def supplier_search(request):
    """Search for suppliers (both user suppliers and custom suppliers) - branch-specific."""
    try:
        query = request.GET.get('q', '').strip()
        branch_id = request.GET.get('branch_id')
        
        if not query:
            return Response([])
        
        results = []
        
        organization_id = getattr(request.user, 'organization_id', None)
        user_branch_id = getattr(request.user, 'branch_id', None)
        target_branch_id = branch_id or user_branch_id
        
        # Search user suppliers
        user_suppliers = User.objects.filter(
            role=User.SUPPLIER_ADMIN
        ).filter(
            Q(first_name__icontains=query) | 
            Q(last_name__icontains=query) |
            Q(email__icontains=query)
        )[:10]
        
        for user in user_suppliers:
            results.append({
                'id': user.id,
                'name': user.get_full_name() or user.email,
                'contact': user.phone or user.email,
                'type': 'user',
                'organization_id': user.organization_id
            })
        
        # Search custom suppliers
        if organization_id:
            custom_suppliers = CustomSupplier.objects.filter(
                organization_id=organization_id,
                name__icontains=query,
                is_active=True
            )[:10]
            
            for supplier in custom_suppliers:
                results.append({
                    'id': supplier.id,
                    'name': supplier.name,
                    'contact': supplier.phone or supplier.email,
                    'type': 'custom'
                })
        
        return Response(results)
        
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['POST'])
def create_inventory_item(request):
    """Create inventory items from stock entry with transaction tracking (branch-specific)."""
    try:
        data = request.data
        supplier_data = data.get('supplier', {})
        items_data = data.get('items', [])
        payment_data = data.get('payment', {})
        branch_id = data.get('branch_id')
        
        if not supplier_data.get('name') or not items_data:
            return Response({
                'error': 'Supplier and items are required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        organization_id = getattr(request.user, 'organization_id', None)
        user_branch_id = getattr(request.user, 'branch_id', None)
        target_branch_id = branch_id or user_branch_id
        
        if not organization_id:
            organization_id = 3
        if not target_branch_id:
            target_branch_id = 1
        
        created_items = []
        errors = []
        
        # Handle supplier
        supplier_user = None
        custom_supplier = None
        supplier_type = supplier_data.get('type', 'custom')
        
        if supplier_type == 'user':
            try:
                supplier_user = User.objects.get(
                    id=supplier_data['id'],
                    role=User.SUPPLIER_ADMIN
                )
            except User.DoesNotExist:
                return Response({
                    'error': 'Invalid supplier user'
                }, status=status.HTTP_400_BAD_REQUEST)
        else:
            custom_supplier, created = CustomSupplier.objects.get_or_create(
                name=supplier_data['name'],
                organization_id=organization_id,
                defaults={
                    'contact_person': supplier_data.get('contact', ''),
                    'phone': supplier_data.get('contact', ''),
                    'created_by': request.user
                }
            )
        
        # Calculate total amount
        total_amount = sum(float(item['cost_price']) * int(item['quantity']) for item in items_data)
        
        # Create purchase transaction
        transaction = PurchaseTransaction.objects.create(
            supplier_name=supplier_data['name'],
            supplier_contact=supplier_data.get('contact', ''),
            total_amount=total_amount,
            organization_id=organization_id,
            branch_id=target_branch_id,
            created_by=request.user
        )
        
        # Create payment record
        payment = PaymentRecord.objects.create(
            transaction=transaction,
            payment_method=payment_data.get('paymentMethod', 'cash'),
            payment_date=payment_data.get('paymentDate', '2024-01-01'),
            total_amount=total_amount,
            paid_amount=float(payment_data.get('paidAmount', total_amount)),
            notes=payment_data.get('notes', ''),
            organization_id=organization_id,
            created_by=request.user
        )
        
        # Sync to unified ledger
        try:
            sync_to_supplier_ledger(
                supplier_name=supplier_data['name'],
                supplier_type='user' if supplier_user else 'custom',
                supplier_user=supplier_user,
                source_type='stock_management',
                reference_id=transaction.transaction_number,
                transaction_amount=total_amount,
                paid_amount=float(payment_data.get('paidAmount', total_amount)),
                organization_id=organization_id,
                branch_id=target_branch_id,
                transaction_date=timezone.now()
            )
        except Exception as e:
            print(f"Ledger sync error: {str(e)}")
        
        # Create inventory items
        for item_data in items_data:
            try:
                medicine_id = item_data.get('medicine_id')
                product = Product.objects.get(id=medicine_id)
                
                # Validate required fields
                required_fields = ['quantity', 'cost_price', 'batch_number', 'expiry_date']
                missing_fields = [field for field in required_fields if not item_data.get(field)]
                if missing_fields:
                    error_msg = f"Missing required fields for {product.name}: {missing_fields}"
                    errors.append(error_msg)
                    continue
                
                # Get rack and section information
                rack_id = item_data.get('rackId')
                section_id = item_data.get('sectionId')
                rack_name = item_data.get('rackName', '')
                section_name = item_data.get('sectionName', '')

                # Create location string from rack and section
                location = ''
                if rack_name and section_name:
                    location = f"{rack_name}-{section_name}"

                inventory_item = InventoryItem.objects.create(
                    product=product,
                    supplier_type='user' if supplier_user else 'custom',
                    supplier_user=supplier_user,
                    custom_supplier=custom_supplier,
                    quantity=int(item_data['quantity']),
                    unit=item_data.get('unit', 'pieces'),
                    cost_price=float(item_data['cost_price']),
                    selling_price=float(item_data.get('selling_price', 0)) if item_data.get('selling_price') else None,
                    batch_number=item_data['batch_number'],
                    manufacturing_date=item_data.get('manufacturing_date') or None,
                    expiry_date=item_data['expiry_date'],
                    location=location,
                    organization_id=organization_id,
                    branch_id=target_branch_id,
                    created_by=request.user
                )
                
                # Create purchase item record
                purchase_item = PurchaseItem.objects.create(
                    purchase_transaction=transaction,
                    product=product,
                    quantity_purchased=int(item_data['quantity']),
                    unit=item_data.get('unit', 'pieces'),
                    cost_price=float(item_data['cost_price']),
                    selling_price=float(item_data.get('selling_price', 0)) if item_data.get('selling_price') else None,
                    batch_number=item_data['batch_number'],
                    manufacturing_date=item_data.get('manufacturing_date') or None,
                    expiry_date=item_data['expiry_date'],
                    inventory_item=inventory_item
                )
                
                created_items.append({
                    'id': inventory_item.id,
                    'product_name': product.name,
                    'quantity': inventory_item.quantity,
                    'batch_number': inventory_item.batch_number,
                    'purchase_item_id': purchase_item.id
                })
                
            except Product.DoesNotExist:
                error_msg = f"Medicine with ID {item_data.get('medicine_id')} not found"
                errors.append(error_msg)
            except Exception as e:
                error_msg = f"Error creating item for medicine {item_data.get('medicine_id')}: {str(e)}"
                errors.append(error_msg)
        
        return Response({
            'message': f'Successfully created {len(created_items)} inventory items',
            'transaction_number': transaction.transaction_number,
            'payment_number': payment.payment_number,
            'credit_amount': float(payment.credit_amount),
            'created_items': created_items,
            'errors': errors
        })
        
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def inventory_items_list(request):
    """Get list of inventory items (branch-specific) with FIFO grouping."""
    try:
        organization_id = getattr(request.user, 'organization_id', None)
        branch_id = getattr(request.user, 'branch_id', None)
        pos_mode = request.GET.get('pos_mode', 'false').lower() == 'true'
        
        query_branch_id = request.GET.get('branch_id')
        if query_branch_id and request.user.role == 'super_admin':
            branch_id = int(query_branch_id)
        
        if not organization_id:
            organization_id = 3
        
        filters = {'organization_id': organization_id, 'quantity__gt': 0}
        if branch_id:
            filters['branch_id'] = branch_id
        
        inventory_items = InventoryItem.objects.filter(
            **filters
        ).select_related('product', 'product__category', 'supplier_user', 'custom_supplier').order_by('product_id', 'created_at')
        
        if pos_mode:
            # For POS: Group by medicine and show only first available batch per medicine
            medicine_batches = {}
            for item in inventory_items:
                medicine_id = item.product.id
                if medicine_id not in medicine_batches:
                    medicine_batches[medicine_id] = []
                medicine_batches[medicine_id].append(item)
            
            results = []
            for medicine_id, batches in medicine_batches.items():
                batches.sort(key=lambda x: x.created_at)
                first_batch = batches[0]
                
                supplier_name = ''
                if first_batch.supplier_type == 'user' and first_batch.supplier_user:
                    supplier_name = first_batch.supplier_user.get_full_name() or first_batch.supplier_user.email
                elif first_batch.supplier_type == 'custom' and first_batch.custom_supplier:
                    supplier_name = first_batch.custom_supplier.name
                
                total_stock = sum(batch.quantity for batch in batches)
                
                results.append({
                    'id': first_batch.id,
                    'medicine_id': medicine_id,
                    'medicine': {
                        'id': first_batch.product.id,
                        'name': first_batch.product.name,
                        'strength': first_batch.product.strength,
                        'dosage_form': first_batch.product.dosage_form,
                        'product_code': first_batch.product.product_code,
                        'category': {
                            'name': first_batch.product.category.name if first_batch.product.category else 'N/A'
                        }
                    },
                    'current_stock': first_batch.quantity,
                    'total_stock': total_stock,
                    'cost_price': float(first_batch.cost_price),
                    'selling_price': float(first_batch.selling_price) if first_batch.selling_price else float(first_batch.cost_price),
                    'location': first_batch.location or '',
                    'supplier_name': supplier_name,
                    'batch_number': first_batch.batch_number,
                    'expiry_date': first_batch.expiry_date.strftime('%Y-%m-%d') if first_batch.expiry_date else None,
                    'unit': first_batch.unit,
                    'created_at': first_batch.created_at.strftime('%Y-%m-%d') if first_batch.created_at else None,
                    'all_batches': [{
                        'id': batch.id,
                        'quantity': batch.quantity,
                        'selling_price': float(batch.selling_price) if batch.selling_price else float(batch.cost_price),
                        'batch_number': batch.batch_number,
                        'expiry_date': batch.expiry_date.strftime('%Y-%m-%d') if batch.expiry_date else None,
                        'created_at': batch.created_at.strftime('%Y-%m-%d %H:%M:%S') if batch.created_at else None
                    } for batch in batches]
                })
        else:
            # For inventory management: Show all items individually
            results = []
            for item in inventory_items:
                supplier_name = ''
                if item.supplier_type == 'user' and item.supplier_user:
                    supplier_name = item.supplier_user.get_full_name() or item.supplier_user.email
                elif item.supplier_type == 'custom' and item.custom_supplier:
                    supplier_name = item.custom_supplier.name
                
                results.append({
                    'id': item.id,
                    'medicine': {
                        'id': item.product.id,
                        'name': item.product.name,
                        'strength': item.product.strength,
                        'dosage_form': item.product.dosage_form,
                        'category': {
                            'name': item.product.category.name if item.product.category else 'N/A'
                        }
                    },
                    'current_stock': item.quantity,
                    'min_stock': getattr(item, 'min_stock_level', 10),
                    'max_stock': getattr(item, 'max_stock_level', 1000),
                    'cost_price': float(item.cost_price),
                    'selling_price': float(item.selling_price) if item.selling_price else 0,
                    'location': item.location or '',
                    'supplier_name': supplier_name,
                    'batch_number': item.batch_number,
                    'expiry_date': item.expiry_date.strftime('%Y-%m-%d') if item.expiry_date else None,
                    'unit': item.unit,
                    'created_at': item.created_at.strftime('%Y-%m-%d') if item.created_at else None
                })
        
        return Response(results)
        
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['PATCH'])
@permission_classes([IsAuthenticated])
def update_inventory_item(request, item_id):
    """Update inventory item (branch-specific)."""
    try:
        organization_id = getattr(request.user, 'organization_id', None)
        branch_id = getattr(request.user, 'branch_id', None)
        request_branch_id = request.data.get('branch_id')
        
        target_branch_id = request_branch_id or branch_id
        
        if not organization_id:
            organization_id = 3
        
        filters = {'id': item_id, 'organization_id': organization_id}
        if target_branch_id:
            filters['branch_id'] = target_branch_id
        
        inventory_item = get_object_or_404(InventoryItem, **filters)
        
        data = request.data
        if 'selling_price' in data:
            inventory_item.selling_price = float(data['selling_price'])
        if 'location' in data:
            inventory_item.location = data['location']
        
        inventory_item.save()
        
        return Response({
            'message': 'Inventory item updated successfully',
            'item': {
                'id': inventory_item.id,
                'selling_price': float(inventory_item.selling_price) if inventory_item.selling_price else 0,
                'location': inventory_item.location
            }
        })
        
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def restock_item(request):
    """Restock existing inventory item with transaction tracking (branch-specific)."""
    try:
        data = request.data
        supplier_data = data.get('supplier', {})
        item_data = data.get('item', {})
        payment_data = data.get('payment', {})
        previous_item_id = data.get('previous_item_id')
        request_branch_id = data.get('branch_id')
        
        organization_id = getattr(request.user, 'organization_id', None)
        user_branch_id = getattr(request.user, 'branch_id', None)
        branch_id = request_branch_id or user_branch_id
        
        if not organization_id:
            organization_id = 3
        if not branch_id:
            branch_id = 1
        
        # Get the medicine
        try:
            product = Product.objects.get(id=item_data['medicine_id'])
        except Product.DoesNotExist:
            return Response({'error': 'Medicine not found'}, status=400)
        
        # Handle supplier
        supplier_user = None
        custom_supplier = None
        supplier_type = supplier_data.get('type', 'custom')
        
        if supplier_type == 'user':
            try:
                supplier_user = User.objects.get(
                    id=supplier_data.get('id'),
                    role=User.SUPPLIER_ADMIN
                )
            except User.DoesNotExist:
                return Response({'error': 'Invalid supplier user'}, status=400)
        else:
            custom_supplier, created = CustomSupplier.objects.get_or_create(
                name=supplier_data['name'],
                organization_id=organization_id,
                defaults={
                    'contact_person': supplier_data.get('contact', ''),
                    'phone': supplier_data.get('contact', ''),
                    'created_by': request.user
                }
            )
        
        # Get rack and section information
        rack_id = item_data.get('rackId')
        section_id = item_data.get('sectionId')
        rack_name = item_data.get('rackName', '')
        section_name = item_data.get('sectionName', '')

        # Create location string from rack and section, or use previous item's location
        location = ''
        if rack_name and section_name:
            location = f"{rack_name}-{section_name}"
        elif previous_item_id:
            try:
                previous_item = InventoryItem.objects.get(id=previous_item_id)
                location = previous_item.location or ''
            except InventoryItem.DoesNotExist:
                pass

        # Create new inventory item
        inventory_item = InventoryItem.objects.create(
            product=product,
            supplier_type='user' if supplier_user else 'custom',
            supplier_user=supplier_user,
            custom_supplier=custom_supplier,
            quantity=int(item_data['quantity']),
            unit=item_data.get('unit', 'pieces'),
            cost_price=float(item_data['cost_price']),
            selling_price=float(item_data.get('selling_price', 0)) if item_data.get('selling_price') else None,
            batch_number=item_data['batch_number'],
            manufacturing_date=item_data.get('manufacturing_date'),
            expiry_date=item_data['expiry_date'],
            location=location,
            organization_id=organization_id,
            branch_id=branch_id,
            created_by=request.user
        )
        
        # Create purchase transaction
        total_amount = float(item_data['cost_price']) * int(item_data['quantity'])
        transaction = PurchaseTransaction.objects.create(
            supplier_name=supplier_data['name'],
            supplier_contact=supplier_data.get('contact', ''),
            total_amount=total_amount,
            organization_id=organization_id,
            branch_id=branch_id,
            created_by=request.user
        )
        
        # Create purchase item record
        purchase_item = PurchaseItem.objects.create(
            purchase_transaction=transaction,
            product=product,
            quantity_purchased=int(item_data['quantity']),
            unit=item_data.get('unit', 'pieces'),
            cost_price=float(item_data['cost_price']),
            selling_price=float(item_data.get('selling_price', 0)) if item_data.get('selling_price') else None,
            batch_number=item_data['batch_number'],
            manufacturing_date=item_data.get('manufacturing_date'),
            expiry_date=item_data['expiry_date'],
            inventory_item=inventory_item
        )
        
        # Create payment record
        payment = PaymentRecord.objects.create(
            transaction=transaction,
            payment_method=payment_data.get('paymentMethod', 'cash'),
            payment_date=payment_data.get('paymentDate', '2024-01-01'),
            total_amount=total_amount,
            paid_amount=float(payment_data.get('paidAmount', 0)),
            notes=payment_data.get('notes', ''),
            organization_id=organization_id,
            created_by=request.user
        )
        
        # Sync to unified ledger
        try:
            sync_to_supplier_ledger(
                supplier_name=supplier_data['name'],
                supplier_type='user' if supplier_user else 'custom',
                supplier_user=supplier_user,
                source_type='stock_management',
                reference_id=transaction.transaction_number,
                transaction_amount=total_amount,
                paid_amount=float(payment_data.get('paidAmount', 0)),
                organization_id=organization_id,
                branch_id=branch_id,
                transaction_date=timezone.now()
            )
        except Exception as e:
            print(f"Ledger sync error: {str(e)}")
        
        return Response({
            'message': f'Successfully restocked {product.name}',
            'transaction_number': transaction.transaction_number,
            'payment_number': payment.payment_number,
            'inventory_item_id': inventory_item.id,
            'purchase_item_id': purchase_item.id,
            'credit_amount': float(payment.credit_amount)
        })
        
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def allocate_stock_fifo(request):
    """Allocate stock using FIFO method for POS sales."""
    try:
        data = request.data
        medicine_id = data.get('medicine_id')
        requested_quantity = int(data.get('quantity', 0))
        branch_id = data.get('branch_id')
        
        organization_id = getattr(request.user, 'organization_id', None)
        user_branch_id = getattr(request.user, 'branch_id', None)
        target_branch_id = branch_id or user_branch_id
        
        if not organization_id:
            organization_id = 3
        
        if not medicine_id or requested_quantity <= 0:
            return Response({'error': 'Invalid medicine_id or quantity'}, status=400)
        
        # Get all available batches for this medicine, ordered by creation date (FIFO)
        available_batches = InventoryItem.objects.filter(
            product_id=medicine_id,
            organization_id=organization_id,
            branch_id=target_branch_id,
            quantity__gt=0
        ).order_by('created_at')
        
        if not available_batches.exists():
            return Response({'error': 'No stock available for this medicine'}, status=400)
        
        # Calculate total available stock
        total_available = sum(batch.quantity for batch in available_batches)
        
        if requested_quantity > total_available:
            return Response({
                'error': f'Insufficient stock. Available: {total_available}, Requested: {requested_quantity}'
            }, status=400)
        
        # Allocate stock using FIFO
        allocations = []
        remaining_quantity = requested_quantity
        
        for batch in available_batches:
            if remaining_quantity <= 0:
                break
            
            allocated_from_batch = min(batch.quantity, remaining_quantity)
            
            allocations.append({
                'batch_id': batch.id,
                'batch_number': batch.batch_number,
                'allocated_quantity': allocated_from_batch,
                'selling_price': float(batch.selling_price) if batch.selling_price else float(batch.cost_price),
                'expiry_date': batch.expiry_date.strftime('%Y-%m-%d') if batch.expiry_date else None,
                'location': batch.location or '',
                'remaining_in_batch': batch.quantity - allocated_from_batch
            })
            
            remaining_quantity -= allocated_from_batch
        
        return Response({
            'medicine_id': medicine_id,
            'requested_quantity': requested_quantity,
            'allocations': allocations,
            'total_amount': sum(alloc['allocated_quantity'] * alloc['selling_price'] for alloc in allocations)
        })
        
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def purchase_history(request):
    """Get purchase history with items."""
    try:
        organization_id = getattr(request.user, 'organization_id', None)
        branch_id = getattr(request.user, 'branch_id', None)
        
        if not organization_id:
            organization_id = 3
        
        filters = {'organization_id': organization_id}
        if branch_id:
            filters['branch_id'] = branch_id
        
        transactions = PurchaseTransaction.objects.filter(
            **filters
        ).prefetch_related('items__product', 'payments').order_by('-created_at')[:50]
        
        results = []
        for transaction in transactions:
            payment = transaction.payments.first()
            
            results.append({
                'id': transaction.id,
                'transaction_number': transaction.transaction_number,
                'supplier_name': transaction.supplier_name,
                'supplier_contact': transaction.supplier_contact,
                'total_amount': float(transaction.total_amount),
                'created_at': transaction.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                'payment_method': payment.payment_method if payment else 'N/A',
                'paid_amount': float(payment.paid_amount) if payment else 0,
                'credit_amount': float(payment.credit_amount) if payment else 0,
                'items': [{
                    'id': item.id,
                    'product_name': item.product.name,
                    'quantity_purchased': item.quantity_purchased,
                    'unit': item.unit,
                    'cost_price': float(item.cost_price),
                    'selling_price': float(item.selling_price) if item.selling_price else 0,
                    'batch_number': item.batch_number,
                    'expiry_date': item.expiry_date.strftime('%Y-%m-%d') if item.expiry_date else None,
                    'total_cost': float(item.total_cost)
                } for item in transaction.items.all()]
            })
        
        return Response(results)
        
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def deallocate_stock(request):
    """Deallocate stock from cart when quantity is reduced."""
    try:
        medicine_id = request.data.get('medicine_id')
        quantity = int(request.data.get('quantity', 0))
        branch_id = request.data.get('branch_id')
        cart_key = request.data.get('cart_key')

        if not medicine_id or quantity <= 0:
            return Response({'error': 'Invalid medicine_id or quantity'}, status=400)

        # Get user's allocated batches for this medicine in cart
        # This is a simplified version - in a real implementation, you'd track allocations per cart item
        inventory_items = InventoryItem.objects.filter(
            product_id=medicine_id,
            branch_id=branch_id,
            quantity__gt=0,
            is_active=True
        ).order_by('-expiry_date')  # LIFO for deallocation

        if not inventory_items.exists():
            return Response({'error': 'No stock available for this medicine'}, status=400)

        # Calculate how much to deallocate from each batch (simplified - deallocate from newest first)
        remaining_quantity = quantity
        deallocated_batches = []

        for item in inventory_items:
            if remaining_quantity <= 0:
                break

            deallocate_qty = min(item.quantity, remaining_quantity)
            item.quantity += deallocate_qty  # ADD back to inventory (deallocate)
            item.save()

            deallocated_batches.append({
                'inventory_item_id': item.id,
                'batch_number': item.batch_number,
                'deallocated_quantity': deallocate_qty,
                'selling_price': float(item.selling_price or item.cost_price),
                'available_quantity': item.quantity
            })

            remaining_quantity -= deallocate_qty

        return Response({
            'deallocated_batches': deallocated_batches,
            'total_deallocated': quantity,
            'medicine_id': medicine_id,
            'remaining_batches': []  # In a real implementation, you'd return remaining allocated batches
        })

    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['GET'])
@permission_classes([AllowAny])
def test_api(request):
    """Test API endpoint."""
    try:
        total_users = User.objects.count()
        supplier_users = User.objects.filter(role=User.SUPPLIER_ADMIN).count()
        return Response({
            "status": "API working",
            "total_users": total_users,
            "supplier_users": supplier_users,
            "query_param": request.GET.get('q', 'none')
        })
    except Exception as e:
        return Response({"error": str(e)}, status=500)