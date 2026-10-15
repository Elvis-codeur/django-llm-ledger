from rest_framework import serializers

from .models import Expense, ExpenseComment, LLMCall, ModelPricing, Provider, Run, Tag


class ProviderSerializer(serializers.ModelSerializer):
    class Meta:
        model = Provider
        fields = "__all__"


class ModelPricingSerializer(serializers.ModelSerializer):
    class Meta:
        model = ModelPricing
        fields = "__all__"


class TagSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = "__all__"


class RunSerializer(serializers.ModelSerializer):
    class Meta:
        model = Run
        fields = "__all__"


class LLMCallSerializer(serializers.ModelSerializer):
    class Meta:
        model = LLMCall
        fields = "__all__"
        read_only_fields = ["cost_usd", "pricing_used"]


class ExpenseCommentSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExpenseComment
        fields = "__all__"
        read_only_fields = ["expense"]


class ExpenseSerializer(serializers.ModelSerializer):
    comments = ExpenseCommentSerializer(many=True, read_only=True)

    class Meta:
        model = Expense
        fields = "__all__"
