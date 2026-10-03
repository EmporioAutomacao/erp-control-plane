"""Log "ao vivo" das operações de backup/restauração e heartbeat de comandos longos.

O admin mostra esse log num console com polling (ver `lista_backups`). Cada linha é persistida
na hora com `.update()` (regra do projeto dentro de tasks Celery) e renova `atividade_em`, o
heartbeat que permite ao console distinguir "ainda executando" de "travou".

Comandos longos e silenciosos (pg_dump, docker cp, upload ao bucket) rodam numa thread por
`rodar_com_pulso`, enquanto a thread principal registra um pulso a cada poucos segundos. **Todo
acesso ao banco fica na thread principal**: a thread só executa o comando bloqueante.
"""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

from django.db.models import F, TextField, Value
from django.db.models.functions import Concat
from django.utils import timezone


def fmt_bytes(n):
    n = float(n or 0)
    for unidade in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unidade == 'GB':
            return f'{int(n)} {unidade}' if unidade == 'B' else f'{n:.1f} {unidade}'
        n /= 1024


def tamanho_pasta(caminho):
    """Soma dos arquivos de uma pasta (para mostrar o crescimento durante o `docker cp`)."""
    total = 0
    for raiz, _, arquivos in os.walk(caminho):
        for nome in arquivos:
            try:
                total += os.path.getsize(os.path.join(raiz, nome))
            except OSError:
                pass
    return total


class Progresso:
    """Contador de bytes seguro entre threads (callback do boto3 -> thread principal)."""

    def __init__(self, total=0):
        self.total = total
        self._feito = 0
        self._lock = threading.Lock()

    def somar(self, n):
        with self._lock:
            self._feito += n

    @property
    def feito(self):
        with self._lock:
            return self._feito

    def texto(self):
        if self.total:
            pct = min(100, int(self.feito * 100 / self.total))
            return f'{fmt_bytes(self.feito)} de {fmt_bytes(self.total)} ({pct}%)'
        return fmt_bytes(self.feito)


class LogOperacao:
    """Anexa linhas ao log de um `BackupCliente` (`log` ou `log_restauracao`)."""

    def __init__(self, backup_id, campo='log'):
        assert campo in ('log', 'log_restauracao')
        self.backup_id = backup_id
        self.campo = campo

    def _qs(self):
        from .models import BackupCliente
        return BackupCliente.objects.filter(pk=self.backup_id)

    def linha(self, texto):
        ts = timezone.localtime().strftime('%H:%M:%S')
        self._qs().update(
            **{self.campo: Concat(F(self.campo), Value(f'[{ts}] {texto}\n'), output_field=TextField())},
            atividade_em=timezone.now(),
        )

    def pulso(self):
        self._qs().update(atividade_em=timezone.now())

    def reiniciar(self):
        """Começa um log novo (ex.: nova restauração do mesmo backup)."""
        agora = timezone.now()
        extra = {'restauracao_iniciada_em': agora} if self.campo == 'log_restauracao' else {}
        self._qs().update(**{self.campo: ''}, atividade_em=agora, **extra)


def rodar_com_pulso(log, rotulo, fn, *args, medir=None, intervalo=3.0, linha_a_cada=6.0, **kwargs):
    """Executa `fn` numa thread e registra pulsos enquanto ela não termina.

    - a cada `intervalo` s renova o heartbeat (`atividade_em`);
    - a cada `linha_a_cada` s escreve "<rotulo> em execução há Ns · <medir()>" no log;
    - a exceção de `fn` (inclusive TimeoutError) é propagada para quem chamou.
    """
    inicio = time.monotonic()
    ultima_linha = inicio
    with ThreadPoolExecutor(max_workers=1) as executor:
        futuro = executor.submit(fn, *args, **kwargs)
        while True:
            wait([futuro], timeout=intervalo)
            if futuro.done():
                return futuro.result()
            agora = time.monotonic()
            if agora - ultima_linha >= linha_a_cada:
                extra = ''
                if medir is not None:
                    try:
                        extra = f' · {medir()}'
                    except Exception:  # medir é só informativo
                        extra = ''
                log.linha(f'{rotulo} em execução há {int(agora - inicio)}s{extra}')
                ultima_linha = agora
            else:
                log.pulso()
