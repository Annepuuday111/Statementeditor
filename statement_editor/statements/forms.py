from django import forms
from django.contrib.auth.forms import AuthenticationForm
from .models import Statement

class LoginForm(AuthenticationForm):
    username = forms.CharField(widget=forms.TextInput(attrs={
        "class": "form-control", "placeholder": "Username"
    }))
    password = forms.CharField(widget=forms.PasswordInput(attrs={
        "class": "form-control", "placeholder": "Password"
    }))

class StatementUploadForm(forms.ModelForm):
    class Meta:
        model = Statement
        fields = ['original_file']
        widgets = {
            'original_file': forms.ClearableFileInput(attrs={'accept': 'application/pdf'})
        }