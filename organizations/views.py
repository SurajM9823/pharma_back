import logging
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from rest_framework import generics, status, permissions, serializers
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.views import APIView
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter

# Set up logger
logger = logging.getLogger(__name__)

# Test logging at module level
print("BRANCH VIEWS MODULE LOADED")
logger.critical("BRANCH VIEWS MODULE LOADED WITH LOGGER")

from .models import Organization, Branch, OrganizationSettings
from .serializers import (
    OrganizationSerializer,
    OrganizationCreateSerializer,
    OrganizationUpdateSerializer,
    BranchSerializer,
    BranchCreateSerializer,
    OrganizationSettingsSerializer
)
from accounts.models import User
from accounts.serializers import UserSerializer, UserCreateSerializer


class OrganizationListView(generics.ListCreateAPIView):
    """List and create organizations view."""
    queryset = Organization.objects.all()
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    search_fields = ['name', 'email', 'phone', 'license_number']
    ordering_fields = ['name', 'created_at', 'status']
    ordering = ['-created_at']

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return OrganizationCreateSerializer
        return OrganizationSerializer

    def get_queryset(self):
        """Filter organizations based on user permissions."""
        user = self.request.user

        if user.role == 'super_admin':
            return Organization.objects.all()
        elif user.role == 'pharmacy_owner':
            # Pharmacy owners can see their own organization
            return Organization.objects.filter(owner=user)
        else:
            # Other users can only see their organization
            return Organization.objects.filter(id=user.organization_id)

    def create(self, request, *args, **kwargs):
        """Create new organization with proper validation."""
        data = request.data.copy()

        # Handle owner as dict (create user if needed)
        owner_data = data.get('owner')
        if owner_data and isinstance(owner_data, dict):
            # Add missing fields for owner creation
            owner_data_copy = owner_data.copy()
            if 'password_confirm' not in owner_data_copy:
                owner_data_copy['password_confirm'] = owner_data_copy.get('password', '')
            if 'role' not in owner_data_copy:
                owner_data_copy['role'] = 'pharmacy_owner'  # Default role for organization owner
            if 'organization_id' not in owner_data_copy:
                owner_data_copy['organization_id'] = None  # Will be set after organization creation
            if 'branch_id' not in owner_data_copy:
                owner_data_copy['branch_id'] = None

            owner_serializer = UserCreateSerializer(data=owner_data_copy, context={'request': request})
            owner_serializer.is_valid(raise_exception=True)
            owner = owner_serializer.save()
            owner.created_by = request.user
            owner.save()
            data['owner'] = owner.id
        elif owner_data and isinstance(owner_data, str):
            # Handle owner as email string
            try:
                owner = User.objects.get(email=owner_data)
                data['owner'] = owner.id
            except User.DoesNotExist:
                raise serializers.ValidationError({'owner': _('User with this email does not exist.')})

        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)

        # Set created_by
        serializer.validated_data['created_by'] = request.user

        # For pharmacy owners, set them as the owner if not already set
        if request.user.role == 'pharmacy_owner' and not serializer.validated_data.get('owner'):
            serializer.validated_data['owner'] = request.user

        organization = serializer.save()

        # Always create default main branch for new organizations
        default_branch = None
        try:
            # Generate unique branch code for this organization
            branch_code = f"MAIN_{organization.id}"
            default_branch = Branch.objects.create(
                name='Main Branch',
                code=branch_code,
                type='main',
                address=organization.address,
                city=organization.city,
                state=organization.state,
                postal_code=organization.postal_code,
                country=organization.country,
                phone=organization.phone,
                email=organization.email,
                organization=organization,
                status='active',
                created_by=request.user
            )
            logger.info(f"Created default branch for organization {organization.id}")
        except Exception as e:
            logger.error(f"Failed to create default branch for organization {organization.id}: {str(e)}")

        # Update owner's organization_id and assign to branch if owner was created
        if owner_data and isinstance(owner_data, dict) and hasattr(organization, 'owner') and organization.owner:
            organization.owner.organization_id = organization.id
            
            # Assign owner to the main branch if it was created
            if default_branch:
                organization.owner.branch_id = default_branch.id
            
            organization.owner.save()

        return Response({
            'organization': OrganizationSerializer(organization).data,
            'message': _('Organization created successfully.')
        }, status=status.HTTP_201_CREATED)


class OrganizationDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Organization detail view."""
    queryset = Organization.objects.all()
    serializer_class = OrganizationSerializer

    def get_serializer_class(self):
        if self.request.method in ['PUT', 'PATCH']:
            return OrganizationUpdateSerializer
        return OrganizationSerializer

    def get_queryset(self):
        """Filter organizations based on user permissions."""
        user = self.request.user

        if user.role == 'super_admin':
            return Organization.objects.all()
        elif user.role == 'pharmacy_owner':
            return Organization.objects.filter(owner=user)
        else:
            return Organization.objects.filter(id=user.organization_id)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        return Response({
            'organization': serializer.data,
            'message': _('Organization updated successfully.')
        })

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()

        # Check permissions
        user = request.user
        if user.role == 'super_admin':
            pass  # Super admin can delete any organization
        elif user.role == 'pharmacy_owner' and instance.owner == user:
            pass  # Pharmacy owner can delete their own organization
        else:
            return Response({
                'error': _('Insufficient permissions to delete this organization.')
            }, status=status.HTTP_403_FORBIDDEN)

        self.perform_destroy(instance)
        return Response({
            'message': _('Organization deleted successfully.')
        })


class BranchListView(generics.ListCreateAPIView):
    """List and create branches view."""
    serializer_class = BranchSerializer
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['organization', 'status', 'type']
    search_fields = ['name', 'email', 'phone', 'address']
    ordering_fields = ['name', 'created_at', 'status']
    ordering = ['-created_at']

    # Disable pagination for this view
    pagination_class = None

    def list(self, request, *args, **kwargs):
        """Override list method to handle queryset manually."""
        user = request.user

        # Manual queryset filtering based on user permissions
        if user.role == 'super_admin':
            queryset = Branch.objects.all()
        elif user.role == 'pharmacy_owner' and user.organization_id:
            queryset = Branch.objects.filter(organization_id=user.organization_id)
        elif user.organization_id:
            queryset = Branch.objects.filter(organization_id=user.organization_id)
        else:
            queryset = Branch.objects.none()

        # Manually serialize and return
        from .serializers import BranchSerializer
        serializer = BranchSerializer(queryset, many=True)
        return Response(serializer.data)

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return BranchCreateSerializer
        return BranchSerializer

    def get_queryset(self):
        """Filter branches based on user permissions."""
        user = self.request.user

        if user.role == 'super_admin':
            return Branch.objects.all()
        elif user.role == 'pharmacy_owner':
            # Pharmacy owner can see branches in organizations they own
            return Branch.objects.filter(organization__owner=user)
        else:
            # Regular users can see branches in their organization
            if user.organization_id:
                return Branch.objects.filter(organization_id=user.organization_id)
            else:
                # If user has no organization_id, return empty queryset
                return Branch.objects.none()

    def create(self, request, *args, **kwargs):
        """Create new branch with proper validation."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Set created_by
        serializer.validated_data['created_by'] = request.user

        # Validate organization permissions
        organization = serializer.validated_data['organization']
        user = request.user

        if user.role == 'super_admin':
            pass  # Super admin can create branches for any organization
        elif user.role == 'pharmacy_owner' and organization.owner == user:
            pass  # Pharmacy owner can create branches for their organization
        elif organization.id == user.organization_id:
            pass  # Any user can create branches in their own organization
        else:
            return Response({
                'error': _('Insufficient permissions to create branch for this organization.')
            }, status=status.HTTP_403_FORBIDDEN)

        branch = serializer.save()

        return Response({
            'branch': BranchSerializer(branch).data,
            'message': _('Branch created successfully.')
        }, status=status.HTTP_201_CREATED)


class BranchDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Branch detail view."""
    queryset = Branch.objects.all()
    serializer_class = BranchSerializer


    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        return Response({
            'branch': serializer.data,
            'message': _('Branch updated successfully.')
        })

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()

        # Check permissions
        user = request.user
        if user.role == 'super_admin':
            pass  # Super admin can delete any branch
        elif user.role == 'pharmacy_owner' and instance.organization.owner == user:
            pass  # Pharmacy owner can delete branches in their organization
        else:
            return Response({
                'error': _('Insufficient permissions to delete this branch.')
            }, status=status.HTTP_403_FORBIDDEN)

        self.perform_destroy(instance)
        return Response({
            'message': _('Branch deleted successfully.')
        })


class OrganizationSettingsView(generics.RetrieveUpdateAPIView):
    """Organization settings view."""
    serializer_class = OrganizationSettingsSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        """Get or create organization settings."""
        organization = Organization.objects.get(id=self.request.user.organization_id)
        settings, created = OrganizationSettings.objects.get_or_create(
            organization=organization,
            defaults={'updated_by': self.request.user}
        )
        return settings

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)

        # Set updated_by
        serializer.validated_data['updated_by'] = request.user

        self.perform_update(serializer)

        return Response({
            'settings': serializer.data,
            'message': _('Organization settings updated successfully.')
        })


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_organization_stats(request):
    """Get comprehensive organization statistics for dashboard."""
    user = request.user

    if user.role == 'super_admin':
        # Organization statistics
        total_orgs = Organization.objects.count()
        active_orgs = Organization.objects.filter(status='active').count()
        pending_orgs = Organization.objects.filter(status='pending').count()

        # Branch statistics
        total_branches = Branch.objects.count()
        active_branches = Branch.objects.filter(status='active').count()

        # User statistics (import User model)
        from accounts.models import User
        total_users = User.objects.count()
        active_users = User.objects.filter(status='active', is_active=True).count()

        # Subscription statistics
        subscription_stats = {}
        for plan in ['trial', 'basic', 'professional', 'enterprise']:
            count = Organization.objects.filter(subscription_plan=plan).count()
            subscription_stats[plan] = count

        # Revenue calculation (simplified - you can enhance this)
        # For now, we'll calculate based on subscription plans
        revenue = 0
        revenue += subscription_stats.get('basic', 0) * 5000  # ₹5,000/month
        revenue += subscription_stats.get('professional', 0) * 15000  # ₹15,000/month
        revenue += subscription_stats.get('enterprise', 0) * 50000  # ₹50,000/month

        # Monthly growth (organizations created this month)
        from django.utils import timezone
        from django.db.models import Count
        import calendar

        now = timezone.now()
        current_month = now.month
        current_year = now.year

        monthly_orgs = Organization.objects.filter(
            created_at__year=current_year,
            created_at__month=current_month
        ).count()

        # Previous month comparison
        if current_month == 1:
            prev_month = 12
            prev_year = current_year - 1
        else:
            prev_month = current_month - 1
            prev_year = current_year

        prev_month_orgs = Organization.objects.filter(
            created_at__year=prev_year,
            created_at__month=prev_month
        ).count()

        growth_percentage = 0
        if prev_month_orgs > 0:
            growth_percentage = ((monthly_orgs - prev_month_orgs) / prev_month_orgs) * 100

        stats = {
            'total_organizations': total_orgs,
            'active_organizations': active_orgs,
            'pending_organizations': pending_orgs,
            'total_branches': total_branches,
            'active_branches': active_branches,
            'total_users': total_users,
            'active_users': active_users,
            'monthly_revenue': revenue,
            'monthly_growth': monthly_orgs,
            'growth_percentage': round(growth_percentage, 1),
            'subscription_distribution': subscription_stats,
            'active_subscriptions': total_orgs - subscription_stats.get('trial', 0),
        }
    elif user.role == 'pharmacy_owner':
        org = Organization.objects.filter(owner=user).first()
        if org:
            total_branches = org.branches.count()
            active_branches = org.branches.filter(status='active').count()

            # Get users for this organization
            from accounts.models import User
            total_users = User.objects.filter(organization_id=org.id).count()
            active_users = User.objects.filter(organization_id=org.id, status='active', is_active=True).count()

            stats = {
                'organization_name': org.name,
                'total_branches': total_branches,
                'active_branches': active_branches,
                'total_users': total_users,
                'active_users': active_users,
                'subscription_plan': org.subscription_plan,
                'monthly_revenue': 0,  # Individual org revenue calculation
            }
        else:
            stats = {'error': 'No organization found'}
    else:
        org = Organization.objects.filter(id=user.organization_id).first()
        if org:
            user_branch = user.branch
            if user_branch:
                from accounts.models import User
                branch_users = User.objects.filter(branch_id=user_branch.id).count()
                branch_active_users = User.objects.filter(branch_id=user_branch.id, status='active', is_active=True).count()

                stats = {
                    'organization_name': org.name,
                    'branch_name': user_branch.name,
                    'branch_users': branch_users,
                    'branch_active_users': branch_active_users,
                }
            else:
                stats = {
                    'organization_name': org.name,
                    'total_branches': org.branches.count(),
                    'active_branches': org.branches.filter(status='active').count(),
                }
        else:
            stats = {'error': 'No organization found'}

    return Response(stats)


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def create_default_branch(request):
    """Create a default main branch for an organization if none exists."""
    user = request.user
    
    # Only allow organization owners and super admins to create default branches
    if user.role not in ['super_admin', 'pharmacy_owner']:
        return Response({
            'error': _('Insufficient permissions to create branches.')
        }, status=status.HTTP_403_FORBIDDEN)
    
    organization_id = user.organization_id
    if not organization_id:
        return Response({
            'error': _('No organization found for user.')
        }, status=status.HTTP_400_BAD_REQUEST)
    
    # Check if organization already has branches
    existing_branches = Branch.objects.filter(organization_id=organization_id)
    if existing_branches.exists():
        return Response({
            'error': _('Organization already has branches.'),
            'branches': BranchSerializer(existing_branches, many=True).data
        }, status=status.HTTP_400_BAD_REQUEST)
    
    # Get organization details
    try:
        organization = Organization.objects.get(id=organization_id)
    except Organization.DoesNotExist:
        return Response({
            'error': _('Organization not found.')
        }, status=status.HTTP_404_NOT_FOUND)
    
    # Create default main branch
    branch_data = {
        'name': 'Main Branch',
        'code': 'MAIN',
        'type': 'main',
        'address': organization.address,
        'city': organization.city,
        'state': organization.state,
        'postal_code': organization.postal_code,
        'country': organization.country,
        'phone': organization.phone,
        'email': organization.email,
        'organization': organization,
        'status': 'active',
        'created_by': user
    }
    
    try:
        branch = Branch.objects.create(**branch_data)
        return Response({
            'branch': BranchSerializer(branch).data,
            'message': _('Default main branch created successfully.')
        }, status=status.HTTP_201_CREATED)
    except Exception as e:
        return Response({
            'error': str(e)
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def create_organization_with_owner(request):
    """Create organization and set owner in one step (for super admin)."""
    if request.user.role != 'super_admin':
        return Response({
            'error': _('Only super admin can create organizations with owners.')
        }, status=status.HTTP_403_FORBIDDEN)

    data = request.data.copy()

    # Handle owner as dict (create user if needed)
    owner_data = data.get('owner')
    if owner_data and isinstance(owner_data, dict):
        # Add missing fields for owner creation
        owner_data_copy = owner_data.copy()
        if 'password_confirm' not in owner_data_copy:
            owner_data_copy['password_confirm'] = owner_data_copy.get('password', '')
        if 'role' not in owner_data_copy:
            owner_data_copy['role'] = 'pharmacy_owner'  # Default role for organization owner
        if 'organization_id' not in owner_data_copy:
            owner_data_copy['organization_id'] = None  # Will be set after organization creation
        if 'branch_id' not in owner_data_copy:
            owner_data_copy['branch_id'] = None

        owner_serializer = UserCreateSerializer(data=owner_data_copy)
        owner_serializer.is_valid(raise_exception=True)
        owner = owner_serializer.save(created_by=request.user)
        data['owner'] = owner.id
    elif owner_data and isinstance(owner_data, str):
        # Handle owner as email string
        try:
            owner = User.objects.get(email=owner_data)
            data['owner'] = owner.id
        except User.DoesNotExist:
            raise serializers.ValidationError({'owner': _('User with this email does not exist.')})

    # Validate organization data
    org_serializer = OrganizationCreateSerializer(data=data)
    org_serializer.is_valid(raise_exception=True)

    # Set created_by
    org_serializer.validated_data['created_by'] = request.user

    organization = org_serializer.save()

    # Always create default main branch for new organizations
    default_branch = None
    try:
        # Generate unique branch code for this organization
        branch_code = f"MAIN_{organization.id}"
        default_branch = Branch.objects.create(
            name='Main Branch',
            code=branch_code,
            type='main',
            address=organization.address,
            city=organization.city,
            state=organization.state,
            postal_code=organization.postal_code,
            country=organization.country,
            phone=organization.phone,
            email=organization.email,
            organization=organization,
            status='active',
            created_by=request.user
        )
        logger.info(f"Created default branch for organization {organization.id}")
    except Exception as e:
        logger.error(f"Failed to create default branch for organization {organization.id}: {str(e)}")

    # Update owner's organization_id and assign to branch if owner was created
    if owner_data and isinstance(owner_data, dict) and hasattr(organization, 'owner') and organization.owner:
        organization.owner.organization_id = organization.id
        
        # Assign owner to the main branch if it was created
        if default_branch:
            organization.owner.branch_id = default_branch.id
        
        organization.owner.save()

    if owner_data:
        return Response({
            'organization': OrganizationSerializer(organization).data,
            'owner': UserSerializer(organization.owner).data,
            'message': _('Organization and owner created successfully.')
        }, status=status.HTTP_201_CREATED)
    else:
        return Response({
            'organization': OrganizationSerializer(organization).data,
            'message': _('Organization created successfully.')
        }, status=status.HTTP_201_CREATED)