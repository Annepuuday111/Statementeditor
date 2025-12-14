from django.db import models
from django.conf import settings

class Statement(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    original_file = models.FileField(upload_to='statements/original/')
    edited_file = models.FileField(upload_to='statements/edited/', null=True, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    data = models.JSONField(default=dict)

    # Persist the user's selected bank & layout (model)
    bank = models.CharField(max_length=32, default='SBI')
    layout = models.CharField(max_length=64, default='SBI_POST_VALUE')

    def __str__(self):
        return f"Statement #{self.id} by {self.user.username}"