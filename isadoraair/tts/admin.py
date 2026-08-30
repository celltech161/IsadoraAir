from django import forms
from django.contrib import admin

from isadoraair.tts.models import PiperVoiceModel, StationTTSVoice
from isadoraair.tts.voice_catalog import KOKORO_PROVIDER_VOICE_CHOICES


class StationTTSVoiceAdminForm(forms.ModelForm):
    provider_voice = forms.ChoiceField(
        required=False,
        choices=(),
        help_text="Kokoro's native voice ID. Blank for Piper voices.",
    )

    class Meta:
        model = StationTTSVoice
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.is_bound:
            engine = self.data.get(self.add_prefix("engine"))
        else:
            engine = self.initial.get("engine") or getattr(self.instance, "engine", "")
        if not engine:
            engine = StationTTSVoice.Engine.KOKORO

        choices = [("", "---------")]
        if engine == StationTTSVoice.Engine.KOKORO:
            choices.extend(KOKORO_PROVIDER_VOICE_CHOICES)
        self.fields["provider_voice"].choices = choices


@admin.register(StationTTSVoice)
class StationTTSVoiceAdmin(admin.ModelAdmin):
    form = StationTTSVoiceAdminForm
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
