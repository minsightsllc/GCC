from django.core.files.storage import Storage, storages
from django.utils.module_loading import import_string
from django.utils.deconstruct import deconstructible

from .media_sanitization import sanitized_storage_content


@deconstructible
class MetadataStrippingStorage(Storage):
    """Sanitize uploaded photos/videos before delegating to selected storage.

    The configured inner backend remains local /media in development and
    Supabase Storage in production. Only the sanitized bytes are written there.
    """

    def __init__(self, backend, options=None):
        self.backend_path = backend
        self.options = options or {}
        self._backend = None

    @property
    def backend(self):
        if self._backend is None:
            self._backend = import_string(self.backend_path)(**self.options)
        return self._backend

    def open(self, name, mode="rb"):
        return self.backend.open(name, mode)

    def save(self, name, content, max_length=None):
        cleaned = sanitized_storage_content(name, content)
        return self.backend.save(name, cleaned, max_length=max_length)

    def delete(self, name):
        return self.backend.delete(name)

    def exists(self, name):
        return self.backend.exists(name)

    def listdir(self, path):
        return self.backend.listdir(path)

    def size(self, name):
        return self.backend.size(name)

    def url(self, name):
        return self.backend.url(name)

    def path(self, name):
        return self.backend.path(name)

    def get_accessed_time(self, name):
        return self.backend.get_accessed_time(name)

    def get_created_time(self, name):
        return self.backend.get_created_time(name)

    def get_modified_time(self, name):
        return self.backend.get_modified_time(name)

    def get_valid_name(self, name):
        return self.backend.get_valid_name(name)

    def get_available_name(self, name, max_length=None):
        return self.backend.get_available_name(name, max_length=max_length)

    def generate_filename(self, filename):
        return self.backend.generate_filename(filename)


@deconstructible
class ContactSubmissionStorage(Storage):
    """Resolve the contact-only storage alias at operation time.

    Keeping this as a proxy lets tests and deployments swap the named alias
    without changing the storage used by unrelated upload fields.
    """

    alias = "contact_submissions"

    @property
    def backend(self):
        return storages[self.alias]

    def open(self, name, mode="rb"):
        return self.backend.open(name, mode)

    def save(self, name, content, max_length=None):
        return self.backend.save(name, content, max_length=max_length)

    def delete(self, name):
        return self.backend.delete(name)

    def exists(self, name):
        return self.backend.exists(name)

    def listdir(self, path):
        return self.backend.listdir(path)

    def size(self, name):
        return self.backend.size(name)

    def url(self, name):
        return self.backend.url(name)

    def path(self, name):
        return self.backend.path(name)

    def get_accessed_time(self, name):
        return self.backend.get_accessed_time(name)

    def get_created_time(self, name):
        return self.backend.get_created_time(name)

    def get_modified_time(self, name):
        return self.backend.get_modified_time(name)

    def get_valid_name(self, name):
        return self.backend.get_valid_name(name)

    def get_available_name(self, name, max_length=None):
        return self.backend.get_available_name(name, max_length=max_length)

    def generate_filename(self, filename):
        return self.backend.generate_filename(filename)
