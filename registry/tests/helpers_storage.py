"""Bucket S3 falso para os testes de backup (sem rede e sem ConfiguracaoArmazenamento no banco)."""
import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class FakeBucket:
    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix='fake-bucket-'))
        self.registrar_status = MagicMock()
        self.registrar_envio_ok = MagicMock()

    @staticmethod
    def _chave(caminho):
        caminho = str(caminho)
        return caminho[len('s3://'):] if caminho.startswith('s3://') else caminho

    def enviar(self, arquivo_local, chave, progresso=None, cfg=None):
        destino = self.dir / chave
        destino.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(arquivo_local, destino)
        if progresso is not None:
            progresso.total = destino.stat().st_size
            progresso.somar(progresso.total)
        return f's3://{chave}'

    def baixar(self, caminho, destino, progresso=None, cfg=None):
        shutil.copy(self.dir / self._chave(caminho), destino)
        if progresso is not None:
            progresso.somar(os.path.getsize(destino))

    def apagar(self, caminho):
        (self.dir / self._chave(caminho)).unlink(missing_ok=True)

    def url_download(self, caminho, nome_arquivo, expira=600):
        return f'http://fake-bucket/{self._chave(caminho)}'

    def existe(self, caminho):
        return (self.dir / self._chave(caminho)).exists()

    def arquivos(self, prefixo='', padrao='*'):
        base = self.dir / prefixo
        return sorted(base.glob(padrao)) if base.exists() else []


def instalar_fake_bucket(testcase):
    """Faz `storage_backup` falar com um bucket em diretório temporário durante o teste."""
    bucket = FakeBucket()
    testcase.addCleanup(shutil.rmtree, bucket.dir, True)
    for alvo, valor in [
        ('registry.storage_backup.configurado', lambda: True),
        ('registry.storage_backup.exigir_configuracao', lambda: SimpleNamespace(bucket='fake')),
        ('registry.storage_backup.enviar', bucket.enviar),
        ('registry.storage_backup.baixar', bucket.baixar),
        ('registry.storage_backup.apagar', bucket.apagar),
        ('registry.storage_backup.url_download', bucket.url_download),
        ('registry.storage_backup.registrar_status', bucket.registrar_status),
        ('registry.storage_backup.registrar_envio_ok', bucket.registrar_envio_ok),
    ]:
        p = patch(alvo, side_effect=valor) if callable(valor) else patch(alvo, valor)
        p.start()
        testcase.addCleanup(p.stop)
    return bucket
