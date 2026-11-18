import ctypes

import pytest

from pdf_annotator.util import windows_credentials as credentials


class NativeApi:
    def __init__(self):
        self.record = self.buffer = None
        self.frees = 0
        self.last_error = credentials.ERROR_NOT_FOUND

    def CredWriteW(self, pointer, flags):
        record = ctypes.cast(pointer, credentials._CredentialPointer).contents
        assert not flags
        assert record.TargetName == credentials.TARGET and record.Type == credentials.CRED_TYPE_GENERIC
        assert record.Persist == credentials.CRED_PERSIST_LOCAL_MACHINE
        data = ctypes.string_at(record.CredentialBlob, record.CredentialBlobSize)
        self.buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        self.record = credentials._Credential(CredentialBlobSize=len(data), CredentialBlob=self.buffer)
        return 1

    def CredReadW(self, target, kind, flags, pointer):
        assert (target, kind, flags) == (credentials.TARGET, credentials.CRED_TYPE_GENERIC, 0)
        if self.record is None:
            return 0
        ctypes.cast(pointer, ctypes.POINTER(credentials._CredentialPointer))[0] = ctypes.pointer(self.record)
        return 1

    def CredDeleteW(self, target, kind, flags):
        assert (target, kind, flags) == (credentials.TARGET, credentials.CRED_TYPE_GENERIC, 0)
        if self.record is None:
            return 0
        self.record = self.buffer = None
        return 1

    def CredFree(self, pointer):
        assert pointer.contents.CredentialBlobSize > 0
        self.frees += 1


def test_generic_local_user_credential_replacement_and_idempotent_delete(monkeypatch):
    api = NativeApi()
    monkeypatch.setattr(credentials.ctypes, "get_last_error", lambda: api.last_error, raising=False)
    store = credentials.WindowsCredentialStore(api_factory=lambda: api)
    assert store.read() is None
    store.write("dummy-first")
    assert store.read() == "dummy-first"
    store.write("dummy-sostituzione")
    assert store.read() == "dummy-sostituzione" and api.frees == 2
    store.delete()
    store.delete()
    assert store.read() is None


def test_vault_precedes_environment_and_access_failure_is_classified(monkeypatch):
    api = NativeApi()
    monkeypatch.setattr(credentials.ctypes, "get_last_error", lambda: api.last_error, raising=False)
    store = credentials.WindowsCredentialStore(api_factory=lambda: api)
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-development")
    assert credentials.resolve_key(store) == ("dummy-development", "environment")
    store.write("dummy-vault")
    assert credentials.resolve_key(store) == ("dummy-vault", "vault")
    store.delete()
    api.last_error = 5
    with pytest.raises(credentials.CredentialError, match="^vault_read_failed$"):
        credentials.resolve_key(store)


@pytest.mark.parametrize("value", ["", "contains spaces", "non-valida-è", "no\0token", "x" * 2561])
def test_validation_error_carries_only_safe_code(value):
    with pytest.raises(credentials.CredentialError, match="^invalid_key$"):
        credentials.validate_key(value)
