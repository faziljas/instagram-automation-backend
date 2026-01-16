import os
from cryptography.fernet import Fernet


def get_encryption_key() -> bytes:
    key = os.getenv("ENCRYPTION_KEY")
    if not key:
        raise ValueError("ENCRYPTION_KEY environment variable not set")
    return key.encode()


def encrypt_credentials(credentials: str) -> str:
    key = get_encryption_key()
    fernet = Fernet(key)
    encrypted = fernet.encrypt(credentials.encode())
    return encrypted.decode()


def decrypt_credentials(encrypted_credentials: str) -> str:
    key = get_encryption_key()
    fernet = Fernet(key)
    decrypted = fernet.decrypt(encrypted_credentials.encode())
    return decrypted.decode()
