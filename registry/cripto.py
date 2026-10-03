"""Criptografia de segredos guardados no banco (ex.: chave secreta do S3).

Fernet com chave derivada da `SECRET_KEY` do Django. Todos os serviços do CP
(web, celery, agente-no) compartilham a mesma `SECRET_KEY`, então qualquer um
deles consegue decifrar. Se a `SECRET_KEY` for trocada, os segredos já salvos
ficam ilegíveis e precisam ser cadastrados de novo.
"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


class SegredoIlegivel(Exception):
    """O valor cifrado não pôde ser decifrado (SECRET_KEY diferente da usada ao salvar)."""


def _fernet():
    chave = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode('utf-8')).digest())
    return Fernet(chave)


def cifrar(texto):
    if not texto:
        return ''
    return _fernet().encrypt(texto.encode('utf-8')).decode('ascii')


def decifrar(token):
    if not token:
        return ''
    try:
        return _fernet().decrypt(token.encode('ascii')).decode('utf-8')
    except InvalidToken as exc:
        raise SegredoIlegivel('Não foi possível decifrar a chave secreta (SECRET_KEY diferente da usada ao salvar).') from exc
