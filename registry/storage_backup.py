"""Armazenamento dos backups em um bucket S3-compatível (RustFS).

A conexão é cadastrada em Admin > Configurações > Armazenamento
(`ConfiguracaoArmazenamento`, singleton). Não há fallback para variáveis de ambiente:
sem cadastro completo, enviar/baixar/apagar levantam `ArmazenamentoNaoConfigurado`.

Um `BackupCliente.arquivo_path` que começa com `s3://` aponta para um objeto
(`s3://<chave>`); qualquer outro valor é um caminho de disco local (backups antigos,
criados antes do bucket existir — continuam podendo ser baixados e restaurados).
"""
import logging
import os
import time
import uuid

PREFIXO = 's3://'
MSG_NAO_CONFIGURADO = (
    'Armazenamento de backups não configurado. '
    'Acesse Admin → Configurações → Armazenamento.'
)

logger = logging.getLogger(__name__)


class ArmazenamentoNaoConfigurado(RuntimeError):
    pass


def configuracao():
    """A `ConfiguracaoArmazenamento` cadastrada, ou None."""
    from .models import ConfiguracaoArmazenamento
    return ConfiguracaoArmazenamento.obter()


def configurado():
    cfg = configuracao()
    return bool(cfg and cfg.completa)


def exigir_configuracao():
    cfg = configuracao()
    if not cfg or not cfg.completa:
        raise ArmazenamentoNaoConfigurado(MSG_NAO_CONFIGURADO)
    return cfg


def eh_remoto(caminho):
    return bool(caminho) and str(caminho).startswith(PREFIXO)


def _chave(caminho):
    return str(caminho)[len(PREFIXO):] if eh_remoto(caminho) else str(caminho)


def _cliente(cfg, endpoint=None, curto=False):
    import boto3
    from botocore.config import Config

    opcoes = {'signature_version': 's3v4', 's3': {'addressing_style': 'path'}}
    if curto:
        # usado no teste de conexão: não pode travar a tela do admin
        opcoes.update(connect_timeout=5, read_timeout=10, retries={'max_attempts': 1})
    return boto3.client(
        's3',
        endpoint_url=endpoint or cfg.endpoint,
        aws_access_key_id=cfg.access_key,
        aws_secret_access_key=cfg.secret_key,
        region_name=cfg.regiao or 'us-east-1',
        config=Config(**opcoes),
    )


def enviar(arquivo_local, chave, progresso=None, cfg=None):
    """Envia o arquivo para o bucket (multipart automático) e devolve o `s3://chave`.

    `progresso` (opcional) é um `log_operacao.Progresso`, alimentado pelo callback do boto3.
    `cfg` permite resolver a configuração fora da thread (acesso ao banco só na principal).
    """
    cfg = cfg or exigir_configuracao()
    if progresso is not None and not progresso.total:
        progresso.total = os.path.getsize(arquivo_local)
    _cliente(cfg).upload_file(
        str(arquivo_local), cfg.bucket, chave,
        Callback=progresso.somar if progresso is not None else None,
    )
    return f'{PREFIXO}{chave}'


def baixar(caminho, destino, progresso=None, cfg=None):
    cfg = cfg or exigir_configuracao()
    cliente = _cliente(cfg)
    if progresso is not None and not progresso.total:
        try:
            progresso.total = cliente.head_object(Bucket=cfg.bucket, Key=_chave(caminho))['ContentLength']
        except Exception:  # só informativo
            pass
    cliente.download_file(
        cfg.bucket, _chave(caminho), str(destino),
        Callback=progresso.somar if progresso is not None else None,
    )


def apagar(caminho):
    cfg = exigir_configuracao()
    _cliente(cfg).delete_object(Bucket=cfg.bucket, Key=_chave(caminho))


def url_download(caminho, nome_arquivo, expira=600):
    """Link temporário (presigned) para o navegador baixar o objeto."""
    cfg = exigir_configuracao()
    endpoint = (cfg.endpoint_publico or '').strip() or None
    return _cliente(cfg, endpoint=endpoint).generate_presigned_url(
        'get_object',
        Params={
            'Bucket': cfg.bucket,
            'Key': _chave(caminho),
            'ResponseContentDisposition': f'attachment; filename="{nome_arquivo}"',
        },
        ExpiresIn=expira,
    )


# ── Teste de conexão / sinalizador de status ─────────────────────────────────

def _traduzir_erro(exc):
    """Mensagem em português para os erros mais comuns do boto3/botocore."""
    from botocore.exceptions import (
        ClientError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError,
    )

    if isinstance(exc, ClientError):
        erro = exc.response.get('Error', {})
        codigo = str(erro.get('Code', ''))
        if codigo in ('404', 'NoSuchBucket', 'NotFound'):
            return 'Bucket não encontrado. Crie o bucket no console do RustFS ou corrija o nome.'
        if codigo in ('InvalidAccessKeyId', 'SignatureDoesNotMatch', 'InvalidSecretKey'):
            return 'Credenciais recusadas: confira a access key e a secret key.'
        if codigo in ('403', 'AccessDenied', 'Forbidden'):
            return 'Acesso negado: a chave não tem permissão de leitura, escrita e exclusão neste bucket.'
        return f'O servidor respondeu com erro {codigo or "desconhecido"}: {erro.get("Message", "")}'.strip()
    if isinstance(exc, (EndpointConnectionError, ConnectTimeoutError)):
        return 'Não foi possível conectar ao endpoint. Confira o endereço, a porta e se o RustFS está no ar.'
    if isinstance(exc, ReadTimeoutError):
        return 'O endpoint demorou demais para responder (timeout).'
    if isinstance(exc, ValueError):
        return f'Endpoint inválido: {exc}'
    return f'{type(exc).__name__}: {exc}'


def testar_conexao(cfg=None):
    """Testa a conexão e devolve `(status, mensagem)`.

    Etapas: bucket acessível (head_bucket) → grava, lê e apaga um objeto minúsculo
    (valida as permissões) → o endpoint público responde (só aviso: afeta o ⬇ Baixar).
    Status: `conectado`, `atencao` (bucket ok, endpoint público não), `erro`, `nao_testado`.
    """
    from .cripto import SegredoIlegivel

    cfg = cfg or configuracao()
    if not cfg or not cfg.completa:
        return 'nao_testado', 'Configuração incompleta: preencha endpoint, bucket, access key e secret key.'

    try:
        cfg.secret_key
    except SegredoIlegivel as exc:
        return 'erro', f'{exc} Informe a secret key novamente e salve.'

    inicio = time.monotonic()
    try:
        c = _cliente(cfg, curto=True)
        c.head_bucket(Bucket=cfg.bucket)
        chave = f'_cp-healthcheck/{uuid.uuid4().hex}.txt'
        c.put_object(Bucket=cfg.bucket, Key=chave, Body=b'ok')
        try:
            c.get_object(Bucket=cfg.bucket, Key=chave)['Body'].read()
        finally:
            c.delete_object(Bucket=cfg.bucket, Key=chave)
    except Exception as exc:
        return 'erro', _traduzir_erro(exc)
    ms = int((time.monotonic() - inicio) * 1000)

    publico = (cfg.endpoint_publico or '').strip().rstrip('/')
    if publico and publico != cfg.endpoint.strip().rstrip('/'):
        import requests
        try:
            r = requests.get(f'{publico}/health', timeout=8)
            if r.status_code >= 400:
                raise RuntimeError(f'HTTP {r.status_code}')
        except Exception as exc:
            return 'atencao', (
                f'Bucket "{cfg.bucket}" acessível ({ms} ms), mas o endpoint público não respondeu '
                f'({str(exc)[:120]}). Os backups funcionam; o ⬇ Baixar pode falhar no navegador.'
            )
    return 'conectado', f'Conexão OK: bucket "{cfg.bucket}" acessível, leitura e escrita verificadas ({ms} ms).'


def registrar_status(status, mensagem):
    """Grava o resultado no sinalizador (`filter().update()`: seguro dentro de tasks)."""
    from django.utils import timezone
    from .models import ConfiguracaoArmazenamento

    ConfiguracaoArmazenamento.objects.filter(pk=1).update(
        status_conexao=status, status_mensagem=mensagem[:1000], verificado_em=timezone.now(),
    )


def registrar_envio_ok(slug):
    """Um envio de backup deu certo: prova a conexão interna, mas não o endpoint público,
    então não apaga um aviso de "Atenção" já registrado."""
    from django.utils import timezone
    from .models import ConfiguracaoArmazenamento

    ConfiguracaoArmazenamento.objects.filter(pk=1).exclude(status_conexao='atencao').update(
        status_conexao='conectado', verificado_em=timezone.now(),
        status_mensagem=f'Último envio de backup concluído ({slug}).',
    )


def verificar_e_registrar():
    status, mensagem = testar_conexao()
    if configuracao():
        registrar_status(status, mensagem)
    return status, mensagem
