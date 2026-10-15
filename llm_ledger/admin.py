from django.contrib import admin

from .models import Expense, ExpenseComment, LLMCall, ModelPricing, Provider, Run, Tag

admin.site.register(Provider)
admin.site.register(ModelPricing)
admin.site.register(Tag)
admin.site.register(Run)
admin.site.register(LLMCall)
admin.site.register(Expense)
admin.site.register(ExpenseComment)
