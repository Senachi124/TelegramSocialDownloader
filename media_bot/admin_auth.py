"""Portable password verifiers for the service administrator."""
import getpass
import hashlib
import hmac
import os
import re
import secrets


def hash_password(password, salt):
    return hashlib.scrypt(password.encode(),salt=salt,n=16384,r=8,p=1,dklen=64).hex()


def _verifier():
    value = os.environ.get('MEDIA_BOT_ADMIN_PASSWORD_HASH','')
    match = re.fullmatch(r'scrypt\$([a-fA-F0-9]{16,512})\$([a-fA-F0-9]{128})',value)
    if not match or len(match[1])%2:
        raise ValueError('Configure MEDIA_BOT_ADMIN_PASSWORD_HASH before starting the web service')
    return bytes.fromhex(match[1]),match[2].lower()


def verify(username,password):
    if not password or len(password)>512:
        return False
    salt,expected = _verifier()
    valid = hmac.compare_digest(hash_password(password,salt),expected)
    return valid and hmac.compare_digest(username.encode(),os.environ.get('MEDIA_BOT_ADMIN_USER','sena').encode())


def main():
    password = getpass.getpass()
    if not 1 <= len(password) <= 512:
        raise SystemExit('Supply a password of 1 to 512 characters')
    salt = secrets.token_bytes(16)
    print('scrypt$' + salt.hex() + '$' + hash_password(password,salt))


if __name__=='__main__':
    main()
