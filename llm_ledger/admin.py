from django.contrib import admin

from .models import LLMCall, ModelPricing, Run

admin.site.register(ModelPricing)
admin.site.register(Run)
admin.site.register(LLMCall)
