from rest_framework import serializers

from .models import LLMCall, ModelPricing, Run


class ModelPricingSerializer(serializers.ModelSerializer):
    class Meta:
        model = ModelPricing
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
