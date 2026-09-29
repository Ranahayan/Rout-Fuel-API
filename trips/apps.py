from django.apps import AppConfig


class TripsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "trips"

    def ready(self) -> None:
        # Registers the runtime-configuration system checks (see trips/checks.py).
        from . import checks  # noqa: F401
