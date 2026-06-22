from django.contrib.auth import views as auth_views
from django.urls import path

from .forms import CaseInsensitivePasswordResetForm
from . import views

urlpatterns = [
    # Healthcheck (Railway)
    path('healthz', views.healthz, name='healthz'),

    # Dashboard Pages
    path('', views.home, name='home'),
    path('leads/', views.leads, name='leads'),
    path('leads/nuova/', views.lead_create, name='lead_create'),
    path('leads/<str:pk>/modifica/', views.lead_update, name='lead_update'),
    path('leads/<str:pk>/elimina/', views.lead_delete, name='lead_delete'),
    path('progetti/', views.progetti, name='progetti'),
    path('partnerships/', views.partnerships, name='partnerships'),

    # --- CRUD de Progetti ---
    path('progetti/nuova/', views.progetto_create, name='progetto_create'),
    path('progetti/<str:pk>/modifica/', views.progetto_update, name='progetto_update'),
    path('progetti/<str:pk>/elimina/', views.progetto_delete, name='progetto_delete'),
    
    # --- CRUD de Partnerships ---
    path('partnerships/nuova/', views.partnership_create, name='partnership_create'),
    path('partnerships/nuova/<str:kind>/', views.partnership_create, name='partnership_create_kind'),
    path('partnerships/<str:pk>/modifica/', views.partnership_update, name='partnership_update'),
    path('partnerships/<str:pk>/elimina/', views.partnership_delete, name='partnership_delete'),
    path('partnerships/<str:pk>/sposta/', views.partnership_change_status, name='partnership_change_status'),

    # Soci (read-only: write avviene via sync Sheets -> Supabase)
    path('soci/', views.soci, name='soci'),
    path('soci/admin-promote/', views.admin_promote, name='admin_promote'),
    path('soci/admin-demote/', views.admin_demote, name='admin_demote'),

    # Authentication & Registration
    path('login/', views.login_view, name='login'),
    path('register/', views.register_step1, name='register_step1'), # Step 1: Email check
    path('register/step2/<str:token>/', views.register_step2, name='register_step2'), # Step 2: Password setup

    # Logout
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),

    # Password reset (custom views with email error handling)
    # from_email=None → usa DEFAULT_FROM_EMAIL (Resend onboarding@resend.dev)
    path(
        'password-reset/',
        views.CustomPasswordResetView.as_view(
            form_class=CaseInsensitivePasswordResetForm,
            template_name='dashboard/password_reset_form.html',
            email_template_name='registration/password_reset_email.html',
            subject_template_name='registration/password_reset_subject.txt',
            from_email=None,
        ),
        name='password_reset',
    ),
    path(
        'password-reset/done/',
        auth_views.PasswordResetDoneView.as_view(
            template_name='dashboard/password_reset_done.html',
        ),
        name='password_reset_done',
    ),
    path(
        'reset/<uidb64>/<token>/',
        views.CustomPasswordResetConfirmView.as_view(
            template_name='dashboard/password_reset_confirm.html',
        ),
        name='password_reset_confirm',
    ),
    path(
        'reset/done/',
        auth_views.PasswordResetCompleteView.as_view(
            template_name='dashboard/password_reset_complete.html',
        ),
        name='password_reset_complete',
    ),
]