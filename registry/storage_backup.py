"""Armazenamento dos backups em um bucket S3-compatível (RustFS).

Config por variáveis de ambiente (nunca no banco, como o resto da infra):
  BACKUP_S3_ENDPOINT, BACKUP_S3_BUCKET, BACKUP_S3_ACCESS_KEY, BACKUP_S3_SECRET_KEY,
  BACKUP_S3_REGION (padrão us-east-1) e, opcionalmente, BACKUP_S3_PUBLIC_ENDPOINT —
  endereço alcançável pelo navegador, usado só nos links temporários de download.

Um `BackupCliente.arquivo_path` que começa com `s3://` aponta para um objeto
(`s3://<chave>`); qualquer outro valor é um caminho de disco local (dev sem bucket,
e backups antigos).
"""
import os

PREFIXO = 's3://'


def configurado():
    return all(os.getenv(v) for v in (
        'BACKUP_S3_ENDPOINT', 'BACKUP_S3_BUCKET', 'BACKUP_S3_ACCESS_KEY', 'BACKUP_S3_SECRET_KEY',
    ))


def eh_remoto(caminho):
    return bool(caminho) and str(caminho).startswith(PREFIXO)


def _chave(caminho):
    return str(caminho)[len(PREFIXO):] if eh_remoto(caminho) else str(caminho)


def _cliente(endpoint=None):
    import boto3
    from botocore.config import Config

    return boto3.client(
        's3',
        endpoint_url=endpoint or os.environ['BACKUP_S3_ENDPOINT'],
        aws_access_key_id=os.environ['BACKUP_S3_ACCESS_KEY'],
        aws_secret_access_key=os.environ['BACKUP_S3_SECRET_KEY'],
        region_name=os.getenv('BACKUP_S3_REGION', 'us-east-1'),
        config=Config(signature_version='s3v4', s3={'addressing_style': 'path'}),
    )


def enviar(arquivo_local, chave):
    """Envia o arquivo para o bucket (multipart automático) e devolve o `s3://chave`."""
    _cliente().upload_file(str(arquivo_local), os.environ['BACKUP_S3_BUCKET'], chave)
    return f'{PREFIXO}{chave}'


def baixar(caminho, destino):
    _cliente().download_file(os.environ['BACKUP_S3_BUCKET'], _chave(caminho), str(destino))


def apagar(caminho):
    _cliente().delete_object(Bucket=os.environ['BACKUP_S3_BUCKET'], Key=_chave(caminho))


def url_download(caminho, nome_arquivo, expira=600):
    """Link temporário (presigned) para o navegador baixar o objeto."""
    endpoint = os.getenv('BACKUP_S3_PUBLIC_ENDPOINT') or None
    return _cliente(endpoint).generate_presigned_url(
        'get_object',
        Params={
            'Bucket': os.environ['BACKUP_S3_BUCKET'],
            'Key': _chave(caminho),
            'ResponseContentDisposition': f'attachment; filename="{nome_arquivo}"',
        },
        ExpiresIn=expira,
    )
