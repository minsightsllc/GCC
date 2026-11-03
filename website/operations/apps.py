from django.apps import AppConfig


class OperationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "operations"

    def ready(self):
        from .storage_cleanup import register_storage_cleanup_signals

        register_storage_cleanup_signals()
