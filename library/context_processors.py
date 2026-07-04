from .models import UITheme


def ui_theme(request):
    return {"ui_theme": UITheme.load()}
