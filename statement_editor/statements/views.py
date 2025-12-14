import json

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, HttpResponseForbidden, HttpResponseBadRequest

from .forms import LoginForm, StatementUploadForm
from .models import Statement
from .utils import parse_pdf_to_data, generate_pdf_from_data


def login_view(request):
    """
    Render login form on GET.
    On POST validate LoginForm (should be a subclass of AuthenticationForm or similar),
    authenticate and log the user in then redirect to dashboard.
    """
    # If already authenticated, go to dashboard
    if request.user.is_authenticated:
        return redirect('statements:dashboard')

    # Instantiate the form. Many AuthenticationForm implementations accept (request, data=...)
    form = LoginForm(request, data=request.POST or None)

    if request.method == 'POST':
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            return redirect('statements:dashboard')
        # If invalid fall through to re-render form with errors

    # For GET and invalid POST render the login page with the form
    return render(request, 'statements/login.html', {'form': form})


@login_required
def logout_view(request):
    """
    Simple logout view referenced by your URLs.
    """
    logout(request)
    return redirect('statements:login')


@login_required
def dashboard(request):
    statements = Statement.objects.filter(user=request.user).order_by('-uploaded_at')
    upload_form = StatementUploadForm()
    return render(request, 'statements/dashboard.html', {
        'statements': statements,
        'upload_form': upload_form,
    })


@login_required
def upload_statement(request):
    if request.method != 'POST':
        return redirect('statements:dashboard')

    form = StatementUploadForm(request.POST, request.FILES)
    if form.is_valid():
        stmt = form.save(commit=False)
        stmt.user = request.user

        # bank & layout are set by the dashboard JS into hidden fields
        bank = request.POST.get('bank') or 'SBI'
        layout = request.POST.get('layout') or 'SBI_POST_VALUE'

        uploaded_file = request.FILES['original_file']
        # rewind and parse using chosen layout
        uploaded_file.seek(0)
        parsed_data = parse_pdf_to_data(uploaded_file, layout=layout)

        if 'meta' not in parsed_data or parsed_data['meta'] is None:
            parsed_data['meta'] = {}
        parsed_data['meta']['bank'] = bank
        parsed_data['meta']['layout'] = layout

        # Persist explicit fields on model only if model defines them (avoids migrations)
        if hasattr(stmt, 'bank'):
            stmt.bank = bank
        if hasattr(stmt, 'layout'):
            stmt.layout = layout

        # assign file and data then save
        uploaded_file.seek(0)
        stmt.original_file = uploaded_file
        stmt.data = parsed_data
        stmt.save()

        return redirect('statements:edit', pk=stmt.pk)

    statements = Statement.objects.filter(user=request.user).order_by('-uploaded_at')
    return render(request, 'statements/dashboard.html', {
        'statements': statements,
        'upload_form': form,
    })


@login_required
def edit_statement(request, pk):
    stmt = get_object_or_404(Statement, pk=pk, user=request.user)
    data = stmt.data or {}
    return render(request, 'statements/edit_statement.html', {
        'statement': stmt,
        'data_json': json.dumps(data),
        'data': data,
    })


@login_required
def save_statement(request, pk):
    if request.method != 'POST':
        return HttpResponseForbidden("Only POST allowed")
    stmt = get_object_or_404(Statement, pk=pk, user=request.user)
    data_json = request.POST.get('data_json')
    if not data_json:
        return HttpResponseBadRequest("Missing data_json in POST")
    try:
        updated_data = json.loads(data_json)
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON data")
    stmt.data = updated_data
    # sync explicit fields only if model actually has those attributes
    meta = updated_data.get('meta') or {}
    if hasattr(stmt, 'bank') and meta.get('bank') is not None:
        stmt.bank = meta.get('bank', getattr(stmt, 'bank', 'SBI'))
    if hasattr(stmt, 'layout') and meta.get('layout') is not None:
        stmt.layout = meta.get('layout', getattr(stmt, 'layout', 'SBI_POST_VALUE'))
    stmt.save()
    action = request.POST.get('action', 'download')
    if action == 'save':
        return redirect('statements:edit', pk=stmt.pk)
    else:
        return redirect('statements:download', pk=stmt.pk)


@login_required
def delete_statement(request, pk):
    stmt = get_object_or_404(Statement, pk=pk, user=request.user)
    if stmt.original_file:
        stmt.original_file.delete(save=False)
    if stmt.edited_file:
        stmt.edited_file.delete(save=False)
    stmt.delete()
    return redirect('statements:dashboard')


@login_required
def download_statement(request, pk):
    stmt = get_object_or_404(Statement, pk=pk, user=request.user)
    pdf_file = generate_pdf_from_data(stmt)
    stmt.edited_file.save(pdf_file.name, pdf_file, save=True)
    return FileResponse(stmt.edited_file.open('rb'), as_attachment=True, filename=stmt.edited_file.name)