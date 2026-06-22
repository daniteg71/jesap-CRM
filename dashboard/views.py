from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.http import JsonResponse
from django.db import connection
from django.views.decorators.http import require_GET
from django.contrib.auth.models import User
from django.contrib.auth import authenticate, login
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.paginator import Paginator
from django.db.models import Q
from urllib.parse import urlencode
from .models import Partnership
from .forms import (
    PartnershipForm,
    PartnershipFullForm,
    LeadPartnershipForm,
    NonFinalizzataForm,
    ProgettoForm,
    LeadForm,
)
from . import choices as ch

# These are for generating secure email links
from django.core.mail import send_mail, EmailMultiAlternatives
from django.template.loader import render_to_string
from django.core import signing
from .models import Eventi, Formazioni, Progetti, Soci, Socio, Partnership, Lead
from django.conf import settings# Adicione este import lá no topo junto com os outros
from django.contrib.auth.decorators import user_passes_test

# --- IMPORTS per password reset custom views ---
from django.contrib.auth.views import PasswordResetView as DjangoPasswordResetView
from django.contrib.auth.views import PasswordResetConfirmView as DjangoPasswordResetConfirmView
from django.contrib.auth.forms import SetPasswordForm
from django.http import HttpResponseRedirect
import logging

logger = logging.getLogger(__name__)

# RBAC: Editor = gruppo 'Editori' | staff admin | superuser
def is_editor(user):
    return (
        user.is_authenticated
        and (user.is_superuser or user.is_staff or user.groups.filter(name='Editori').exists())
    )


# ============================================================
# SORTING HELPERS — sort server-side cross-page
# Le date in DB sono salvate come stringhe (DD/MM/YYYY o YYYY-MM-DD).
# `order_by` di Django sui TextField ordina alfabeticamente — sbagliato.
# Soluzione: load full queryset → sort in Python con parser type-aware → paginate.
# ============================================================

import re as _re
from functools import cmp_to_key as _cmp_to_key

_DATE_DMY = _re.compile(r'^(\d{1,2})/(\d{1,2})/(\d{4})$')
_DATE_ISO = _re.compile(r'^(\d{4})-(\d{1,2})-(\d{1,2})$')
_DATE_DMY_DASH = _re.compile(r'^(\d{1,2})-(\d{1,2})-(\d{4})$')


def _make_sort_key(value):
    """
    Converte un valore (qualsiasi tipo) in una chiave sortabile.
    Gestisce date DD/MM/YYYY, ISO YYYY-MM-DD, numeri/currency, percentuali, testo.
    Ritorna `None` per valori vuoti (sinkati in fondo dal comparator).
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s or s in ("-", "None", "N/A"):
        return None

    # Date DD/MM/YYYY → ISO sortable
    m = _DATE_DMY.match(s)
    if m:
        return f"{m.group(3)}{m.group(2).zfill(2)}{m.group(1).zfill(2)}"
    # Date YYYY-MM-DD
    m = _DATE_ISO.match(s)
    if m:
        return f"{m.group(1)}{m.group(2).zfill(2)}{m.group(3).zfill(2)}"
    # Date DD-MM-YYYY
    m = _DATE_DMY_DASH.match(s)
    if m:
        return f"{m.group(3)}{m.group(2).zfill(2)}{m.group(1).zfill(2)}"

    # Number/currency/percent: estrai cifre
    cleaned = _re.sub(r'[^0-9.,-]+', '', s)
    if cleaned and _re.search(r'\d', cleaned):
        try:
            # IT format: 1.234,56 → 1234.56
            if ',' in cleaned and '.' in cleaned:
                cleaned = cleaned.replace('.', '').replace(',', '.')
            elif ',' in cleaned:
                cleaned = cleaned.replace(',', '.')
            return float(cleaned)
        except ValueError:
            pass

    # Text fallback
    return s.lower()


def _sort_records(records, sort_key, sort_dir, sort_map, default_sort):
    """
    Ordina lista records in Python.
    sort_map: dict {sort_key: callable_or_attrname}
    default_sort: tuple (key, dir) usato se sort_key non valido
    Empty values sinkano sempre in fondo (asc + desc).
    Ritorna (records_sorted, sort_key_effettivo, sort_dir_effettivo).
    """
    if sort_key not in sort_map:
        sort_key, sort_dir = default_sort
    if sort_dir not in ("asc", "desc"):
        sort_dir = "asc"

    accessor = sort_map[sort_key]

    def get_key(obj):
        try:
            val = accessor(obj) if callable(accessor) else getattr(obj, accessor, None)
        except Exception:
            val = None
        return _make_sort_key(val)

    sign = 1 if sort_dir == "asc" else -1

    def cmp(a, b):
        ka = get_key(a)
        kb = get_key(b)
        # Empty values sempre in fondo (regardless dir)
        if ka is None and kb is None:
            return 0
        if ka is None:
            return 1
        if kb is None:
            return -1
        # Tipi diversi (es. float vs str): confronta come stringa
        try:
            if ka < kb:
                return -1 * sign
            if ka > kb:
                return 1 * sign
        except TypeError:
            sa, sb = str(ka), str(kb)
            if sa < sb:
                return -1 * sign
            if sa > sb:
                return 1 * sign
        return 0

    records.sort(key=_cmp_to_key(cmp))
    return records, sort_key, sort_dir


def _read_sort_params(request, allowed_keys, default_sort):
    """Legge ?sort= e ?dir= dalla query string, valida contro allowed_keys."""
    sort_key = request.GET.get("sort", "").strip()
    sort_dir = request.GET.get("dir", "asc").strip().lower()
    if sort_key not in allowed_keys:
        sort_key = default_sort[0]
        sort_dir = default_sort[1]
    if sort_dir not in ("asc", "desc"):
        sort_dir = "asc"
    return sort_key, sort_dir

# --- 1. LOGIN (username o email + password) ---
def login_view(request):
    if request.method == "POST":
        # Sanitizzazione: trim + lowercase su identificativo
        u = (request.POST.get("username") or "").strip().lower()
        p = request.POST.get("password") or ""

        user = authenticate(request, username=u, password=p) if u and p else None
        # is_active controllato dal backend (user_can_authenticate)
        if user is not None and user.is_active:
            login(request, user)
            return redirect(reverse("home"))

        # Messaggio generico: non rivelare se l'utente esiste o la password è sbagliata
        messages.error(request, "Credenziali non valide.")

    return render(request, "dashboard/login.html")


# --- 2. SUBSCRIBE STEP 1 (Check Database & Send Email) ---
def register_step1(request):
    if request.method == "POST":
        # Sanitizzazione: trim + lowercase
        email = (request.POST.get("email") or "").strip().lower()

        if Socio.objects.filter(email_jesap__iexact=email).exists():
            if User.objects.filter(email__iexact=email).exists():
                # Messaggio generico per non rivelare se l'account esiste già
                messages.success(
                    request,
                    "Se l'email è valida, riceverai un link per completare la registrazione.",
                )
                return redirect("register_step1")

            # Creazione Token Sicuro
            signer = signing.TimestampSigner()
            signed_token = signer.sign(email) 
            verification_link = request.build_absolute_uri(f"/register/step2/{signed_token}/")

            try:
                ctx = {'verification_link': verification_link}
                text_body = render_to_string('registration/registration_verify_email.txt', ctx)
                html_body = render_to_string('registration/registration_verify_email.html', ctx)

                msg = EmailMultiAlternatives(
                    subject="Completa la tua registrazione al Gestionale JESAP",
                    body=text_body,
                    from_email=None,
                    to=[email],
                )
                msg.attach_alternative(html_body, 'text/html')
                msg.send(fail_silently=False)
                
                messages.success(
                    request,
                    "Ti abbiamo inviato un'email! Controlla la casella di posta (o il terminale) per impostare la password.",
                )
                return redirect("register_step1")

            except Exception as e:
                # Se la mail fallisce, logga l'errore nel terminale senza far crashare la pagina
                print(f"❌ ERRORE INVIO EMAIL: {e}")
                messages.error(
                    request,
                    "Errore tecnico con il server di posta. Riprova più tardi o contatta l'amministratore.",
                )
                return redirect("register_step1")

        # Messaggio generico anti-enumerazione: stessa risposta sia che l'email esista o no
        messages.success(
            request,
            "Se l'email è valida, riceverai un link per completare la registrazione.",
        )
        return redirect("register_step1")

    return render(request, "dashboard/register_step1.html")


# --- 3. SUBSCRIBE STEP 2 (Double Password & Create Account) ---
def register_step2(request, token):
    signer = signing.TimestampSigner()
    
    try:
        email = signer.unsign(token, max_age=86400) # Scade dopo 24h
    except (signing.SignatureExpired, signing.BadSignature):
        messages.error(request, "Link non valido o scaduto.")
        return redirect("register_step1")

    if request.method == "POST":
        password = request.POST.get("password")
        password_confirm = request.POST.get("password_confirm")

        if password != password_confirm:
            messages.error(request, "Le password non coincidono.")
        elif len(password) < 8:
            messages.error(request, "La password deve essere di almeno 8 caratteri.")
        else:
            email_clean = email.strip().lower()
            # Email già validata in step1 (formato nome.cognome@jesap.it firmato).
            short_username = email_clean.split("@", 1)[0]

            if User.objects.filter(username__iexact=short_username).exists():
                messages.error(
                    request,
                    "Questo username è già in uso. Contatta l'amministratore se hai bisogno di assistenza.",
                )
                return render(request, "dashboard/register_step2.html", {"email": email})

            User.objects.create_user(
                username=short_username,
                email=email_clean,
                password=password,
            )

            messages.success(request, f"Account creato! Il tuo username è: {short_username}")
            return redirect("login")

    return render(request, "dashboard/register_step2.html", {"email": email})


# --- PASSWORD RESET (Custom with email error handling) ---
class CustomPasswordResetView(DjangoPasswordResetView):
    """Password reset con error handling. Email via Resend HTTP API (no SMTP)."""

    def form_valid(self, form):
        try:
            return super().form_valid(form)
        except Exception as e:
            logger.error(f"Email sending failed in password reset: {type(e).__name__}: {e}")
            messages.error(
                self.request,
                "Errore durante l'invio della email. Riprova più tardi o contatta l'amministratore.",
            )
            return self.form_invalid(form)


class CustomPasswordResetConfirmView(DjangoPasswordResetConfirmView):
    """Password reset confirm with graceful error handling."""

    def form_valid(self, form):
        try:
            return super().form_valid(form)
        except Exception as e:
            logger.error(f"Password reset error: {type(e).__name__}: {e}")
            messages.error(
                self.request,
                "Errore durante il reset della password. Riprova più tardi.",
            )
            return self.form_invalid(form)


# --- DASHBOARD VIEWS ---
@login_required(login_url="login")
def home(request):
    return render(request, "dashboard/home.html")

# Sort map per Leads
LEADS_SORT_MAP = {
    "lead_id": "lead_id",
    "azienda": "azienda",
    "referente": "referente",
    "owner": "owner",
    "fase": "fase_attuale",
    "stato": "stato_lead",
    "contratto": "stato_contratto",
    "priorita": "priorita",
    "valore_stimato": "valore_stimato",
    "valore_ponderato": "valore_ponderato",
    "probabilita": "probabilita",
    "data_creazione": "data_creazione",
    "data_primo_contatto": "data_primo_contatto",
    "data_prossima_azione": "data_prossima_azione",
}
LEADS_SORT_DEFAULT = ("data_creazione", "desc")


@login_required(login_url="login")
def leads(request):
    """
    BD Lead Control — lista pipeline con filtri e sort server-side cross-page.

    Query params supportati:
      ?q=...                 ricerca su azienda / referente / email
      ?fase=Nuovo            filtro fase pipeline
      ?stato=Attiva          filtro stato lead
      ?owner=...             filtro PM assegnato
      ?alert=1               solo lead con follow-up scaduto
      ?sort=key&dir=asc|desc ordinamento server-side
      ?page=N                paginazione
    """
    search_query = request.GET.get("q", "").strip()
    fase_filter = request.GET.get("fase", "").strip()
    stato_filter = request.GET.get("stato", "").strip()
    owner_filter = request.GET.get("owner", "").strip()
    alert_only = request.GET.get("alert", "") == "1"

    queryset = Lead.objects.all()

    if search_query:
        queryset = queryset.filter(
            Q(azienda__icontains=search_query)
            | Q(referente__icontains=search_query)
            | Q(nome_referente__icontains=search_query)
            | Q(cognome_referente__icontains=search_query)
            | Q(email_referente__icontains=search_query)
            | Q(lead_id__icontains=search_query)
        )

    if fase_filter:
        queryset = queryset.filter(fase_attuale__iexact=fase_filter)
    if stato_filter:
        queryset = queryset.filter(stato_lead__iexact=stato_filter)
    if owner_filter:
        queryset = queryset.filter(owner__icontains=owner_filter)
    if alert_only:
        queryset = queryset.filter(alert_follow_up=True)

    # Sort server-side cross-page
    sort_key, sort_dir = _read_sort_params(request, LEADS_SORT_MAP, LEADS_SORT_DEFAULT)
    all_records = list(queryset)
    all_records, sort_key, sort_dir = _sort_records(
        all_records, sort_key, sort_dir, LEADS_SORT_MAP, LEADS_SORT_DEFAULT
    )

    # KPI rapidi sopra la tabella (sull'intero dataset filtrato, non sulla pagina)
    kpi = {
        "total": len(all_records),
        "attive": sum(1 for l in all_records if (l.stato_lead or "").lower() == "attiva"),
        "vinte": sum(1 for l in all_records if (l.stato_lead or "").lower() == "vinta"),
        "alert": sum(1 for l in all_records if l.alert_follow_up),
        "valore_ponderato_tot": sum(
            (l.valore_ponderato or 0) for l in all_records
        ),
    }

    paginator = Paginator(all_records, 25)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(request, "dashboard/leads.html", {
        "leads": page_obj,
        "page_obj": page_obj,
        "search_query": search_query,
        "fase_filter": fase_filter,
        "stato_filter": stato_filter,
        "owner_filter": owner_filter,
        "alert_only": alert_only,
        "current_sort": sort_key,
        "current_dir": sort_dir,
        "kpi": kpi,
        "fasi": ch.LEAD_FASE_VALUES,
        "stati": ch.LEAD_STATO_VALUES,
        "is_editor": is_editor(request.user),
    })


# ============================================================
# LEAD CRUD (Editor-only)
# ============================================================
@user_passes_test(is_editor)
def lead_create(request):
    if request.method == 'POST':
        form = LeadForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Lead creata con successo!")
            return redirect('leads')
    else:
        form = LeadForm()
    return render(request, 'dashboard/lead_form.html', {
        'form': form, 'azione': 'Nuova',
    })


@user_passes_test(is_editor)
def lead_update(request, pk):
    lead = get_object_or_404(Lead, lead_id=pk)
    if request.method == 'POST':
        form = LeadForm(request.POST, instance=lead)
        if form.is_valid():
            form.save()
            messages.success(request, "Lead aggiornata con successo!")
            return redirect('leads')
    else:
        form = LeadForm(instance=lead)
    return render(request, 'dashboard/lead_form.html', {
        'form': form, 'azione': 'Modifica', 'lead': lead,
    })


@user_passes_test(is_editor)
def lead_delete(request, pk):
    lead = get_object_or_404(Lead, lead_id=pk)
    if request.method == 'POST':
        lead.delete()
        messages.success(request, "Lead eliminata con successo!")
        return redirect('leads')
    return render(request, 'dashboard/lead_confirm_delete.html', {'lead': lead})


# Sort map per Partnership (tab "partnership")
PARTNERSHIP_SORT_MAP = {
    "id": "id_codice",
    "nome": "partnership",
    "status": "status_partnership",
    "data_firma": "data_firma",
    "anno": "anno",
}
PARTNERSHIP_SORT_DEFAULT = ("nome", "asc")

# Sort map per Partnership tab "non_finalizzate" e "lead"
PARTNERSHIP_NF_SORT_MAP = {
    "nome": "partnership",
    "realta": "partnership",
    "contatti": "contatti",
    "data_firma": "data_firma",
    "anno": "anno",
}
PARTNERSHIP_NF_SORT_DEFAULT = ("nome", "asc")


@login_required(login_url="login")
def partnerships(request):
    # Captura a tab atual (padrão: partnership)
    tab = request.GET.get("tab", "partnership")

    # Captura os parâmetros de busca e filtro
    search_query = request.GET.get("q", "").strip()
    status_filter = request.GET.get("status", "").strip()

    context = {
        "current_tab": tab,
        "search_query": search_query,
        "status_filter": status_filter,
        "is_editor": is_editor(request.user),
    }

    if tab == "partnership":
        queryset = Partnership.objects.filter(
            status_partnership__in=Partnership.STATUS_PARTNERSHIP_TAB
        )

        stati_partnership = list(Partnership.STATUS_PARTNERSHIP_TAB)
        context["stati_partnership"] = stati_partnership

        if search_query:
            queryset = queryset.filter(
                Q(partnership__icontains=search_query) |
                Q(contatti__icontains=search_query) |
                Q(id_codice__icontains=search_query)
            )

        if status_filter:
            queryset = queryset.filter(status_partnership=status_filter)

        sort_key, sort_dir = _read_sort_params(request, PARTNERSHIP_SORT_MAP, PARTNERSHIP_SORT_DEFAULT)
        all_records = list(queryset)
        all_records, sort_key, sort_dir = _sort_records(
            all_records, sort_key, sort_dir, PARTNERSHIP_SORT_MAP, PARTNERSHIP_SORT_DEFAULT
        )

        paginator = Paginator(all_records, 25)
        page_obj = paginator.get_page(request.GET.get("page"))
        context["partnerships"] = page_obj
        context["dati_tabella"] = page_obj
        context["page_obj"] = page_obj
        context["current_sort"] = sort_key
        context["current_dir"] = sort_dir

    elif tab == "non_finalizzate":
        queryset = Partnership.objects.filter(
            status_partnership__iexact=Partnership.STATUS_NON_FINALIZZATA
        )

        if search_query:
            queryset = queryset.filter(
                Q(partnership__icontains=search_query) |
                Q(contatti__icontains=search_query)
            )

        sort_key, sort_dir = _read_sort_params(request, PARTNERSHIP_NF_SORT_MAP, PARTNERSHIP_NF_SORT_DEFAULT)
        all_records = list(queryset)
        all_records, sort_key, sort_dir = _sort_records(
            all_records, sort_key, sort_dir, PARTNERSHIP_NF_SORT_MAP, PARTNERSHIP_NF_SORT_DEFAULT
        )

        paginator = Paginator(all_records, 25)
        page_obj = paginator.get_page(request.GET.get("page"))
        context["non_finalizzate"] = page_obj
        context["dati_tabella"] = page_obj
        context["page_obj"] = page_obj
        context["current_sort"] = sort_key
        context["current_dir"] = sort_dir

    elif tab == "lead":
        queryset = Partnership.objects.filter(
            status_partnership__iexact=Partnership.STATUS_TRATTATIVA
        )

        if search_query:
            queryset = queryset.filter(partnership__icontains=search_query)

        sort_key, sort_dir = _read_sort_params(request, PARTNERSHIP_NF_SORT_MAP, PARTNERSHIP_NF_SORT_DEFAULT)
        all_records = list(queryset)
        all_records, sort_key, sort_dir = _sort_records(
            all_records, sort_key, sort_dir, PARTNERSHIP_NF_SORT_MAP, PARTNERSHIP_NF_SORT_DEFAULT
        )

        paginator = Paginator(all_records, 25)
        page_obj = paginator.get_page(request.GET.get("page"))
        context["leads"] = page_obj
        context["dati_tabella"] = page_obj
        context["page_obj"] = page_obj
        context["current_sort"] = sort_key
        context["current_dir"] = sort_dir

    else:
        context["dati_tabella"] = []

    return render(request, "dashboard/partnerships.html", context)

# Sort map per Progetti: chiave URL → attrname o lambda
PROGETTI_SORT_MAP = {
    "progetto": "nome_progetto",
    "stato": "stato",
    "pm": "pm",
    "provenienza": "provenienza",
    "data_inizio": "data_inizio",
    "data_fine": "data_fine_contratto",
    "fatturato": "fatturato_senza_iva",
}
PROGETTI_SORT_DEFAULT = ("progetto", "asc")


@login_required(login_url="login")
def progetti(request):
    search_query = request.GET.get("q", "").strip()
    stato_filter = request.GET.get("stato", "").strip()

    queryset = Progetti.objects.all()
    stati = ch.STATO_PROGETTO_VALUES

    if search_query:
        queryset = queryset.filter(
            Q(nome_progetto__icontains=search_query)
            | Q(pm__icontains=search_query)
            | Q(cliente__icontains=search_query)
        )

    if stato_filter:
        queryset = queryset.filter(stato=stato_filter)

    # Sort server-side: load all → sort Python → paginate
    sort_key, sort_dir = _read_sort_params(request, PROGETTI_SORT_MAP, PROGETTI_SORT_DEFAULT)
    all_records = list(queryset)
    all_records, sort_key, sort_dir = _sort_records(
        all_records, sort_key, sort_dir, PROGETTI_SORT_MAP, PROGETTI_SORT_DEFAULT
    )

    paginator = Paginator(all_records, 25)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(request, "dashboard/progetti.html", {
        "progetti": page_obj,
        "page_obj": page_obj,
        "search_query": search_query,
        "stato_filter": stato_filter,
        "stati": stati,
        "is_editor": is_editor(request.user),
        "current_sort": sort_key,
        "current_dir": sort_dir,
    })


@user_passes_test(is_editor)
def progetto_create(request):
    if request.method == 'POST':
        form = ProgettoForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Progetto creato con successo!")
            return redirect('progetti')
    else:
        form = ProgettoForm()

    return render(request, 'dashboard/progetto_form.html', {'form': form, 'azione': 'Nuovo'})


@user_passes_test(is_editor)
def progetto_update(request, pk):
    progetto = get_object_or_404(Progetti, codice_progetto=pk)

    if request.method == 'POST':
        form = ProgettoForm(request.POST, instance=progetto)
        if form.is_valid():
            form.save()
            messages.success(request, "Progetto aggiornato con successo!")
            return redirect('progetti')
    else:
        form = ProgettoForm(instance=progetto)

    return render(request, 'dashboard/progetto_form.html', {'form': form, 'azione': 'Modifica'})


@user_passes_test(is_editor)
def progetto_delete(request, pk):
    progetto = get_object_or_404(Progetti, codice_progetto=pk)

    if request.method == 'POST':
        progetto.delete()
        messages.success(request, "Progetto eliminato con successo!")
        return redirect('progetti')

    return render(request, 'dashboard/progetto_confirm_delete.html', {'progetto': progetto})

@login_required(login_url="login")
def eventi(request):
    eventi_list = Eventi.objects.all()
    return render(request, "dashboard/pages/eventi.html", {"eventi": eventi_list})

@login_required(login_url="login")
def formazioni(request):
    formazioni_list = Formazioni.objects.all()
    return render(request, "dashboard/pages/formazioni.html", {"formazioni": formazioni_list})

@login_required(login_url="login")
def soci(request):
    tab = request.GET.get("tab", "da")
    search_query = request.GET.get("q", "").strip()

    AREA_TABS = {
        "da":  "D&A",
        "bd":  "BD",
        "hr":  "HR",
        "mc":  "M&C",
    }

    # Redirect unknown tabs to default
    valid_tabs = set(AREA_TABS) | {"board", "admin"}
    if tab not in valid_tabs:
        tab = "da"

    context = {
        "current_tab": tab,
        "search_query": search_query,
        "is_admin_tab": tab == "admin",
        "is_editor": is_editor(request.user),
    }

    if tab == "admin":
        admin_users = User.objects.filter(Q(is_staff=True) | Q(is_superuser=True))
        if search_query:
            admin_users = admin_users.filter(
                Q(username__icontains=search_query)
                | Q(email__icontains=search_query)
                | Q(first_name__icontains=search_query)
                | Q(last_name__icontains=search_query)
            )
        context["admin_users"] = admin_users.order_by("username")

        # Admin (is_staff o superuser) può promuovere/rimuovere altri admin.
        if request.user.is_staff or request.user.is_superuser:
            all_non_admin = User.objects.filter(is_staff=False, is_superuser=False).order_by("username")
            context["non_admin_users"] = all_non_admin
            context["can_manage_admins"] = True
        else:
            context["can_manage_admins"] = False

        context["soci_list"] = []
    else:
        # Base filter: only active members
        queryset = Soci.objects.filter(status__iexact="Associato")

        if tab == "board":
            queryset = queryset.filter(
                Q(ruolo__icontains="board")
                | Q(ruolo__icontains="presidente")
                | Q(ruolo__icontains="vicepresidente")
                | Q(ruolo__icontains="segretario")
                | Q(ruolo__icontains="tesoriere")
                | Q(ruolo_esteso__icontains="board")
            )
        elif tab in AREA_TABS:
            queryset = queryset.filter(area_di_appartenenza__iexact=AREA_TABS[tab])

        if search_query:
            queryset = queryset.filter(
                Q(nome_e_cognome__icontains=search_query)
                | Q(ruolo__icontains=search_query)
                | Q(email_jesap__icontains=search_query)
            )

        # Sort server-side cross-page
        sort_key, sort_dir = _read_sort_params(request, SOCI_SORT_MAP, SOCI_SORT_DEFAULT)
        all_records = list(queryset)
        all_records, sort_key, sort_dir = _sort_records(
            all_records, sort_key, sort_dir, SOCI_SORT_MAP, SOCI_SORT_DEFAULT
        )

        paginator = Paginator(all_records, 25)
        page_obj = paginator.get_page(request.GET.get("page"))
        context["soci_list"] = page_obj
        context["page_obj"] = page_obj
        context["current_sort"] = sort_key
        context["current_dir"] = sort_dir

    return render(request, "dashboard/soci.html", context)


# Sort map per Soci
SOCI_SORT_MAP = {
    "nome": "nome_e_cognome",
    "ruolo": "ruolo",
    "area": "area_di_appartenenza",
}
SOCI_SORT_DEFAULT = ("nome", "asc")


def _is_admin_user(u):
    return u.is_authenticated and (u.is_staff or u.is_superuser)


@login_required(login_url="login")
@user_passes_test(_is_admin_user)
def admin_promote(request):
    if request.method == "POST":
        user_id = request.POST.get("user_id")
        try:
            target = User.objects.get(pk=user_id)
            target.is_staff = True
            target.save(update_fields=["is_staff"])
            messages.success(request, f"{target.username} promosso ad admin.")
        except User.DoesNotExist:
            messages.error(request, "Utente non trovato.")
    return redirect(reverse("soci") + "?tab=admin")


@login_required(login_url="login")
@user_passes_test(_is_admin_user)
def admin_demote(request):
    if request.method == "POST":
        user_id = request.POST.get("user_id")
        try:
            target = User.objects.get(pk=user_id)
            if target == request.user:
                messages.error(request, "Non puoi rimuovere te stesso.")
            elif target.is_superuser and not request.user.is_superuser:
                # Solo un superuser può rimuovere un altro superuser.
                messages.error(request, "Non puoi rimuovere un superuser.")
            else:
                target.is_staff = False
                if request.user.is_superuser:
                    target.is_superuser = False
                    target.save(update_fields=["is_staff", "is_superuser"])
                else:
                    target.save(update_fields=["is_staff"])
                messages.success(request, f"{target.username} rimosso dagli admin.")
        except User.DoesNotExist:
            messages.error(request, "Utente non trovato.")
    return redirect(reverse("soci") + "?tab=admin")

_KIND_TO_FORM = {
    Partnership.KIND_FULL:    PartnershipFullForm,
    Partnership.KIND_LEAD:    LeadPartnershipForm,
    Partnership.KIND_NON_FIN: NonFinalizzataForm,
}


def _redirect_to_tab(kind):
    tab = Partnership.KIND_TO_TAB.get(kind, 'partnership')
    return redirect(reverse('partnerships') + f'?tab={tab}')


@user_passes_test(is_editor)
def partnership_create(request, kind=Partnership.KIND_FULL):
    if kind not in _KIND_TO_FORM:
        kind = Partnership.KIND_FULL
    FormClass = _KIND_TO_FORM[kind]

    if request.method == 'POST':
        form = FormClass(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Partnership creata con successo!")
            # Per le full la tab dipende dallo status scelto
            if kind == Partnership.KIND_FULL:
                status = form.cleaned_data.get('status_partnership') or ''
                if status == Partnership.STATUS_TRATTATIVA:
                    return redirect(reverse('partnerships') + '?tab=lead')
                if status == Partnership.STATUS_NON_FINALIZZATA:
                    return redirect(reverse('partnerships') + '?tab=non_finalizzate')
                return redirect('partnerships')
            return _redirect_to_tab(kind)
    else:
        form = FormClass()

    azione_map = {
        Partnership.KIND_FULL:    'Nuova Partnership',
        Partnership.KIND_LEAD:    'Nuovo Lead',
        Partnership.KIND_NON_FIN: 'Nuova Non Finalizzata',
    }
    context = {
        'form': form,
        'azione': azione_map[kind],
        'kind': kind,
    }
    return render(request, 'dashboard/partnership_form.html', context)


@user_passes_test(is_editor)
def partnership_update(request, pk):
    partnership = get_object_or_404(Partnership, partnership=pk)

    # Scegli il form in base allo status corrente
    status = (partnership.status_partnership or '').strip()
    if status == Partnership.STATUS_TRATTATIVA:
        FormClass = LeadPartnershipForm
        kind = Partnership.KIND_LEAD
    elif status == Partnership.STATUS_NON_FINALIZZATA:
        FormClass = NonFinalizzataForm
        kind = Partnership.KIND_NON_FIN
    else:
        FormClass = PartnershipFullForm
        kind = Partnership.KIND_FULL

    if request.method == 'POST':
        form = FormClass(request.POST, instance=partnership)
        if form.is_valid():
            form.save()
            messages.success(request, "Partnership aggiornata con successo!")
            return _redirect_to_tab(kind)
    else:
        form = FormClass(instance=partnership)

    context = {'form': form, 'azione': 'Modifica', 'kind': kind}
    return render(request, 'dashboard/partnership_form.html', context)


@user_passes_test(is_editor)
def partnership_delete(request, pk):
    partnership = get_object_or_404(Partnership, partnership=pk)

    if request.method == 'POST':
        partnership.delete()
        messages.success(request, "Partnership eliminata con successo!")
        return redirect('partnerships')

    return render(request, 'dashboard/partnership_confirm_delete.html', {'partnership': partnership})


@user_passes_test(is_editor)
def partnership_change_status(request, pk):
    """POST-only: sposta una Partnership in un altro tab cambiando lo status."""
    if request.method != 'POST':
        return redirect('partnerships')

    partnership = get_object_or_404(Partnership, partnership=pk)
    new_status = (request.POST.get('status') or '').strip()

    valid_statuses = dict(Partnership.STATUS_CHOICES)
    if new_status not in valid_statuses:
        messages.error(request, "Status non valido.")
        return redirect('partnerships')

    partnership.status_partnership = new_status
    partnership.save(update_fields=['status_partnership'])
    messages.success(request, f"Spostata in '{new_status}'.")

    # Redirect al tab di destinazione
    if new_status == Partnership.STATUS_TRATTATIVA:
        return redirect(reverse('partnerships') + '?tab=lead')
    if new_status == Partnership.STATUS_NON_FINALIZZATA:
        return redirect(reverse('partnerships') + '?tab=non_finalizzate')
    return redirect(reverse('partnerships') + '?tab=partnership')


@require_GET
def healthz(request):
    """Healthcheck per Railway. Verifica DB raggiungibile."""
    try:
        with connection.cursor() as c:
            c.execute("SELECT 1")
        return JsonResponse({"status": "ok"})
    except Exception as e:
        return JsonResponse({"status": "error", "detail": str(e)}, status=503)
