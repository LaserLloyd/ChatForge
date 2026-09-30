"""In-memory keyring backend for testing."""

import keyring.backend
import keyring.errors


class FakeKeyringBackend(keyring.backend.KeyringBackend):
    """Simple in-memory keyring for testing."""

    priority = 1

    def __init__(self):
        self._storage = {}

    def get_password(self, service, username):
        """Get password from in-memory storage."""
        return self._storage.get((service, username))

    def set_password(self, service, username, password):
        """Store password in memory."""
        self._storage[(service, username)] = password

    def delete_password(self, service, username):
        """Delete password from in-memory storage."""
        if (service, username) not in self._storage:
            raise keyring.errors.PasswordDeleteError("Password not found")
        del self._storage[(service, username)]
