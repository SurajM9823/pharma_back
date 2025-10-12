from django.shortcuts import render
from django.db.models import Q, Count, Avg
from django.utils.translation import gettext_lazy as _
from rest_framework import status, generics, permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.views import APIView
from datetime import datetime, timedelta

from .models import Patient, MedicalRecord, PatientPrescription, PatientVisit
from .serializers import (
    PatientSerializer,
    PatientCreateSerializer,
    MedicalRecordSerializer,
    PatientPrescriptionSerializer,
    PatientVisitSerializer,
    PatientSummarySerializer,
    PatientStatsSerializer
)
from accounts.models import User


class PatientListCreateView(generics.ListCreateAPIView):
    """List and create patients."""
    serializer_class = PatientSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        """Filter patients based on user's organization and permissions."""
        user = self.request.user
        
        if user.role == User.SUPER_ADMIN:
            return Patient.objects.all()
        elif user.role == User.PHARMACY_OWNER:
            return Patient.objects.filter(organization_id=user.organization_id)
        elif user.role in [User.BRANCH_MANAGER, User.SENIOR_PHARMACIST]:
            return Patient.objects.filter(
                organization_id=user.organization_id,
                branch_id=user.branch_id
            )
        else:
            # Regular users can see all patients in their organization
            return Patient.objects.filter(organization_id=user.organization_id)
    
    def get_serializer_class(self):
        """Use different serializer for creation."""
        if self.request.method == 'POST':
            return PatientCreateSerializer
        return PatientSerializer
    
    def perform_create(self, serializer):
        """Set organization and branch based on current user with auto-generated patient ID."""
        user = self.request.user
        
        # Set organization and branch
        org_id = user.organization_id
        branch_id = user.branch_id
        
        serializer.validated_data['organization_id'] = org_id
        if branch_id:
            serializer.validated_data['branch_id'] = branch_id
        
        # Auto-generate patient ID based on organization and branch
        org_prefix = f"ORG{org_id:03d}"
        branch_prefix = f"BR{branch_id:02d}" if branch_id else "BR00"
        
        # Get last patient number for this organization
        last_org_patient = Patient.objects.filter(
            organization_id=org_id,
            patient_id__startswith=org_prefix
        ).order_by('-patient_id').first()
        
        # Get last patient number for this branch
        last_branch_patient = Patient.objects.filter(
            organization_id=org_id,
            branch_id=branch_id,
            patient_id__startswith=f"{org_prefix}-{branch_prefix}"
        ).order_by('-patient_id').first() if branch_id else None
        
        # Generate organization patient number
        if last_org_patient:
            try:
                last_num = int(last_org_patient.patient_id.split('-P')[1])
                org_patient_num = last_num + 1
            except (ValueError, IndexError):
                org_patient_num = 1
        else:
            org_patient_num = 1
        
        # Generate branch patient number
        if last_branch_patient:
            try:
                last_num = int(last_branch_patient.patient_id.split('-P')[1])
                branch_patient_num = last_num + 1
            except (ValueError, IndexError):
                branch_patient_num = 1
        else:
            branch_patient_num = 1
        
        # Use branch-specific ID if branch exists, otherwise org-level ID
        if branch_id:
            patient_id = f"{org_prefix}-{branch_prefix}-P{branch_patient_num:03d}"
        else:
            patient_id = f"{org_prefix}-P{org_patient_num:03d}"
        
        serializer.validated_data['patient_id'] = patient_id
        serializer.save(created_by=user)


class PatientDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a patient."""
    serializer_class = PatientSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        """Filter patients based on user's permissions."""
        user = self.request.user
        
        if user.role == User.SUPER_ADMIN:
            return Patient.objects.all()
        elif user.role == User.PHARMACY_OWNER:
            return Patient.objects.filter(organization_id=user.organization_id)
        else:
            return Patient.objects.filter(organization_id=user.organization_id)


class MedicalRecordListCreateView(generics.ListCreateAPIView):
    """List and create medical records."""
    serializer_class = MedicalRecordSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        """Filter medical records based on user's organization."""
        user = self.request.user
        patient_id = self.request.query_params.get('patient_id')
        
        queryset = MedicalRecord.objects.select_related('patient')
        
        if user.role == User.SUPER_ADMIN:
            pass  # Can see all records
        else:
            queryset = queryset.filter(patient__organization_id=user.organization_id)
        
        if patient_id:
            queryset = queryset.filter(patient_id=patient_id)
        
        return queryset
    
    def perform_create(self, serializer):
        """Auto-generate record ID and set creator."""
        user = self.request.user
        
        # Auto-generate record ID
        last_record = MedicalRecord.objects.order_by('-id').first()
        if last_record and last_record.record_id.startswith('MR'):
            try:
                last_num = int(last_record.record_id[2:])
                new_num = last_num + 1
            except ValueError:
                new_num = 1
        else:
            new_num = 1
        
        serializer.validated_data['record_id'] = f"MR{new_num:03d}"
        serializer.save(created_by=user)


class PatientPrescriptionListCreateView(generics.ListCreateAPIView):
    """List and create patient prescriptions."""
    serializer_class = PatientPrescriptionSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        """Filter prescriptions based on user's organization."""
        user = self.request.user
        patient_id = self.request.query_params.get('patient_id')
        status_filter = self.request.query_params.get('status')
        
        queryset = PatientPrescription.objects.select_related('patient')
        
        if user.role == User.SUPER_ADMIN:
            pass  # Can see all prescriptions
        else:
            queryset = queryset.filter(patient__organization_id=user.organization_id)
        
        if patient_id:
            queryset = queryset.filter(patient_id=patient_id)
        
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        
        return queryset
    
    def perform_create(self, serializer):
        """Auto-generate prescription ID and set creator."""
        user = self.request.user
        
        # Auto-generate prescription ID
        last_prescription = PatientPrescription.objects.order_by('-id').first()
        if last_prescription and last_prescription.prescription_id.startswith('RX'):
            try:
                last_num = int(last_prescription.prescription_id[2:])
                new_num = last_num + 1
            except ValueError:
                new_num = 1
        else:
            new_num = 1
        
        serializer.validated_data['prescription_id'] = f"RX{new_num:03d}"
        serializer.save(created_by=user)


class PatientVisitListCreateView(generics.ListCreateAPIView):
    """List and create patient visits."""
    serializer_class = PatientVisitSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        """Filter visits based on user's organization."""
        user = self.request.user
        patient_id = self.request.query_params.get('patient_id')
        
        queryset = PatientVisit.objects.select_related('patient', 'attended_by')
        
        if user.role == User.SUPER_ADMIN:
            pass  # Can see all visits
        else:
            queryset = queryset.filter(patient__organization_id=user.organization_id)
        
        if patient_id:
            queryset = queryset.filter(patient_id=patient_id)
        
        return queryset
    
    def perform_create(self, serializer):
        """Auto-generate visit ID and set attended_by."""
        user = self.request.user
        
        # Auto-generate visit ID
        last_visit = PatientVisit.objects.order_by('-id').first()
        if last_visit and last_visit.visit_id.startswith('V'):
            try:
                last_num = int(last_visit.visit_id[1:])
                new_num = last_num + 1
            except ValueError:
                new_num = 1
        else:
            new_num = 1
        
        serializer.validated_data['visit_id'] = f"V{new_num:04d}"
        serializer.save(attended_by=user)


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_patient_stats(request):
    """Get patient statistics."""
    user = request.user
    
    # Filter patients based on user's organization
    if user.role == User.SUPER_ADMIN:
        patients = Patient.objects.all()
    else:
        patients = Patient.objects.filter(organization_id=user.organization_id)
    
    # Calculate statistics
    total_patients = patients.count()
    active_patients = patients.filter(status='active').count()
    
    # New patients this month
    this_month = datetime.now().replace(day=1)
    new_patients_this_month = patients.filter(created_at__gte=this_month).count()
    
    # Average age
    average_age = patients.aggregate(avg_age=Avg('date_of_birth'))['avg_age']
    if average_age:
        from datetime import date
        today = date.today()
        average_age = today.year - average_age.year
    else:
        average_age = 0
    
    # Gender distribution
    gender_dist = patients.values('gender').annotate(count=Count('gender'))
    gender_distribution = {item['gender']: item['count'] for item in gender_dist}
    
    # Top medications (mock data for now)
    top_medications = [
        {'name': 'Metformin', 'prescriptions': 234, 'patients': 189},
        {'name': 'Amlodipine', 'prescriptions': 198, 'patients': 156},
        {'name': 'Losartan', 'prescriptions': 167, 'patients': 134},
    ]
    
    # Monthly visits (mock data for now)
    monthly_visits = [
        {'month': 'Jan', 'visits': 456, 'new_patients': 23},
        {'month': 'Feb', 'visits': 523, 'new_patients': 31},
        {'month': 'Mar', 'visits': 489, 'new_patients': 28},
    ]
    
    stats = {
        'total_patients': total_patients,
        'active_patients': active_patients,
        'new_patients_this_month': new_patients_this_month,
        'average_age': average_age,
        'gender_distribution': gender_distribution,
        'top_medications': top_medications,
        'monthly_visits': monthly_visits,
    }
    
    return Response(stats)


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def search_patients(request):
    """Search patients by name, phone, or patient ID."""
    user = request.user
    query = request.GET.get('q', '').strip()
    
    if not query:
        return Response({'patients': []})
    
    # Filter patients based on user's organization
    if user.role == User.SUPER_ADMIN:
        patients = Patient.objects.all()
    else:
        patients = Patient.objects.filter(organization_id=user.organization_id)
    
    # Search by name, phone, or patient ID
    patients = patients.filter(
        Q(first_name__icontains=query) |
        Q(last_name__icontains=query) |
        Q(phone__icontains=query) |
        Q(patient_id__icontains=query)
    )[:20]  # Limit to 20 results
    
    serializer = PatientSummarySerializer(patients, many=True)
    return Response({'patients': serializer.data})


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_patient_summary(request, patient_id):
    """Get patient summary with recent activity."""
    user = request.user
    
    try:
        # Filter patient based on user's organization
        if user.role == User.SUPER_ADMIN:
            patient = Patient.objects.get(id=patient_id)
        else:
            patient = Patient.objects.get(id=patient_id, organization_id=user.organization_id)
        
        # Get recent records, prescriptions, and visits
        recent_records = patient.medical_records.all()[:5]
        recent_prescriptions = patient.prescriptions.all()[:5]
        recent_visits = patient.visits.all()[:5]
        
        return Response({
            'patient': PatientSerializer(patient).data,
            'recent_records': MedicalRecordSerializer(recent_records, many=True).data,
            'recent_prescriptions': PatientPrescriptionSerializer(recent_prescriptions, many=True).data,
            'recent_visits': PatientVisitSerializer(recent_visits, many=True).data,
        })
    
    except Patient.DoesNotExist:
        return Response({
            'error': 'Patient not found.'
        }, status=status.HTTP_404_NOT_FOUND)


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_next_patient_numbers(request):
    """Get next available patient numbers for organization and branch."""
    user = request.user
    org_id = user.organization_id
    branch_id = user.branch_id
    
    org_prefix = f"ORG{org_id:03d}"
    branch_prefix = f"BR{branch_id:02d}" if branch_id else "BR00"
    
    # Get next organization patient number
    last_org_patient = Patient.objects.filter(
        organization_id=org_id,
        patient_id__startswith=org_prefix
    ).order_by('-patient_id').first()
    
    if last_org_patient:
        try:
            last_num = int(last_org_patient.patient_id.split('-P')[1])
            next_org_num = last_num + 1
        except (ValueError, IndexError):
            next_org_num = 1
    else:
        next_org_num = 1
    
    # Get next branch patient number
    if branch_id:
        last_branch_patient = Patient.objects.filter(
            organization_id=org_id,
            branch_id=branch_id,
            patient_id__startswith=f"{org_prefix}-{branch_prefix}"
        ).order_by('-patient_id').first()
        
        if last_branch_patient:
            try:
                last_num = int(last_branch_patient.patient_id.split('-P')[1])
                next_branch_num = last_num + 1
            except (ValueError, IndexError):
                next_branch_num = 1
        else:
            next_branch_num = 1
        
        branch_number = f"{org_prefix}-{branch_prefix}-P{next_branch_num:03d}"
    else:
        branch_number = f"{org_prefix}-P{next_org_num:03d}"
    
    return Response({
        'org_number': f"{org_prefix}-P{next_org_num:03d}",
        'branch_number': branch_number,
        'organization_id': org_id,
        'branch_id': branch_id
    })