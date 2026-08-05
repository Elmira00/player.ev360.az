from django.shortcuts import render

from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib import messages
from django.shortcuts import render, redirect
from django.views.decorators.http import require_POST
from .forms import EmailRegisterForm, EmailLoginForm


def register_view(request):
    """Public registration is disabled — accounts are created via the admin
    panel only. Redirect anyone hitting this URL straight to login."""
    messages.info(request, "New accounts are created by an administrator. Please log in.")
    return redirect("login")


def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    if request.method == "POST":
        form = EmailLoginForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data["email"].lower().strip()
            password = form.cleaned_data["password"]
            user = authenticate(request, username=email, password=password)
            if user is not None:
                login(request, user)
                next_url = request.GET.get("next") or "dashboard"
                return redirect(next_url)
            messages.error(request, "Invalid email or password.")
    else:
        form = EmailLoginForm()

    return render(request, "accounts/login.html", {"form": form})


@require_POST
@login_required
def logout_view(request):
    logout(request)
    return redirect("login")


@login_required
def profile_view(request):
    password_form = PasswordChangeForm(user=request.user)

    if request.method == "POST":
        password_form = PasswordChangeForm(user=request.user, data=request.POST)
        if password_form.is_valid():
            user = password_form.save()
            update_session_auth_hash(request, user)  # keep the user logged in after password change
            messages.success(request, "Password changed successfully.")
            return redirect("profile")

    return render(
        request,
        "accounts/profile.html",
        {"user": request.user, "password_form": password_form},
    )




