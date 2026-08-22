from django.contrib import admin

from isadoraair.tts.models import PiperVoiceModel, StationTTSVoice


@admin.register(StationTTSVoice)
class StationTTSVoiceAdmin(admin.ModelAdmin):
    list_display = ["name", "enabled", "engine", "provider_identity", "language", "speed"]
    list_filter = ["enabled", "engine", "language"]
    search_fields = ["name", "provider_voice", "piper_model__model_id"]

    @admin.display(description="Provider voice/model")
    def provider_identity(self, obj):
        return obj.provider_voice or (obj.piper_model.model_id if obj.piper_model_id else "")


@admin.register(PiperVoiceModel)
class PiperVoiceModelAdmin(admin.ModelAdmin):
    list_display = ["model_id", "language", "sample_rate_hz", "model_filename"]
    search_fields = ["model_id", "model_filename"]
