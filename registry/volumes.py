"""Operações no daemon Docker do **nó do cliente**: volumes e Postgres.

Só funcionam no worker do nó onde os serviços do cliente rodam (fila `no.<hostname>`,
ver `registry/nos.py`). Nenhuma função aqui usa bind mount de pasta do host além do
diretório raiz do Docker (para medir espaço livre).

Regra: um volume é sempre checado com `docker volume inspect` antes de ser montado — o
`docker run/create -v <nome>:...` cria o volume vazio se ele não existir, o que mascararia
a perda dos dados.
"""
import hashlib
import os
import subprocess

# Volumes de arquivos do stack do cliente: nome no stack -> caminho dentro do web/cron.
VOLUMES_ARQUIVOS = {'media': '/app/media', 'backups': '/app/backups'}
VOLUMES_STACK = ('pgdata', 'media', 'backups')

IMAGEM_AUXILIAR = 'alpine'


def nome_volume(slug, volume):
    return f'{slug}_{volume}'


def volume_existe(slug, volume):
    r = subprocess.run(['docker', 'volume', 'inspect', nome_volume(slug, volume)], capture_output=True, timeout=30)
    return r.returncode == 0


def copiar_volume_para_pasta(slug, volume, destino, timeout=600):
    """Copia o conteúdo do volume para `destino` (pasta local do agente).

    Devolve None se o volume não existir; senão o `CompletedProcess` do primeiro passo que
    falhar ou o do `docker cp`. Funciona com o web parado (não depende do container do ERP).
    """
    if not volume_existe(slug, volume):
        return None
    criar = subprocess.run(
        ['docker', 'create', '-v', f'{nome_volume(slug, volume)}:/dados:ro', IMAGEM_AUXILIAR, 'true'],
        capture_output=True, timeout=60,
    )
    cid = (criar.stdout or b'').decode(errors='replace').strip()
    if criar.returncode != 0 or not cid:
        return criar
    try:
        return subprocess.run(['docker', 'cp', f'{cid}:/dados/.', str(destino)], capture_output=True, timeout=timeout)
    finally:
        subprocess.run(['docker', 'rm', '-f', cid], capture_output=True, timeout=60)


def restaurar_pasta_no_volume(slug, volume, origem, timeout=600):
    """Substitui o conteúdo do volume pelo de `origem` (limpa e copia por `docker cp`).

    O volume precisa existir (foi criado pelo stack). Devolve o `CompletedProcess` do primeiro
    passo que falhar ou o do `docker cp`.
    """
    alvo = f'{nome_volume(slug, volume)}:/dados'
    if not volume_existe(slug, volume):
        return subprocess.CompletedProcess(
            args=['docker', 'volume', 'inspect', nome_volume(slug, volume)], returncode=1,
            stdout=b'', stderr=f'volume {nome_volume(slug, volume)} não existe neste nó'.encode(),
        )
    limpar = subprocess.run(
        ['docker', 'run', '--rm', '-v', alvo, IMAGEM_AUXILIAR, 'sh', '-c', 'find /dados -mindepth 1 -delete'],
        capture_output=True, timeout=300,
    )
    if limpar.returncode != 0:
        return limpar
    criar = subprocess.run(['docker', 'create', '-v', alvo, IMAGEM_AUXILIAR, 'true'], capture_output=True, timeout=60)
    cid = (criar.stdout or b'').decode(errors='replace').strip()
    if criar.returncode != 0 or not cid:
        return criar
    try:
        # "<pasta>/." copia o CONTEÚDO da pasta (inclusive arquivos ocultos) para dentro do volume
        return subprocess.run(
            ['docker', 'cp', os.path.join(str(origem), '.'), f'{cid}:/dados/'], capture_output=True, timeout=timeout,
        )
    finally:
        subprocess.run(['docker', 'rm', '-f', cid], capture_output=True, timeout=60)


# Lista "sha256  ./caminho" de todos os arquivos, ordenada pelo caminho, e o total de bytes.
_SCRIPT_MANIFESTO_VOLUME = (
    'cd /dados && '
    'find . -type f -exec sha256sum {} + | sort -k2 > /tmp/lista && '
    'echo "ARQUIVOS $(wc -l < /tmp/lista)" && '
    'echo "BYTES $(find . -type f -exec stat -c %s {} + | awk \'{s+=$1} END {printf \"%d\", s}\')" && '
    'echo "HASH $(sha256sum /tmp/lista | cut -d" " -f1)"'
)

_SCRIPT_TAMANHO_VOLUME = (
    'cd /dados && '
    'echo "ARQUIVOS $(find . -type f | wc -l)" && '
    'echo "BYTES $(find . -type f -exec stat -c %s {} + | awk \'{s+=$1} END {printf \"%d\", s}\')"'
)


def manifesto_volume(slug, volume, com_hash=True, timeout=900):
    """{'existe', 'arquivos', 'bytes', 'hash'} do conteúdo do volume. `hash` cobre caminho e
    conteúdo de cada arquivo (`com_hash=False` só conta, sem ler o conteúdo). Levanta
    RuntimeError se a leitura falhar."""
    if not volume_existe(slug, volume):
        return {'existe': False, 'arquivos': 0, 'bytes': 0, 'hash': ''}
    script = _SCRIPT_MANIFESTO_VOLUME if com_hash else _SCRIPT_TAMANHO_VOLUME
    r = subprocess.run(
        ['docker', 'run', '--rm', '-v', f'{nome_volume(slug, volume)}:/dados:ro', IMAGEM_AUXILIAR,
         'sh', '-c', script],
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(f'Falha ao ler o volume {nome_volume(slug, volume)}: {(r.stderr or "").strip()[:300]}')
    campos = dict(linha.split(' ', 1) for linha in (r.stdout or '').splitlines() if ' ' in linha)
    arquivos = int(campos.get('ARQUIVOS', '0').strip() or 0)
    return {
        'existe': True,
        'arquivos': arquivos,
        'bytes': int(campos.get('BYTES', '0').strip() or 0),
        # Volume vazio: a lista é vazia e o hash é o da string vazia — normalizado para ''.
        'hash': campos.get('HASH', '').strip() if arquivos else '',
    }


def _psql(db_ctr, slug, sql, timeout=600):
    r = subprocess.run(
        ['docker', 'exec', '-i', db_ctr, 'psql', '-v', 'ON_ERROR_STOP=1', '-At', '-F', '|',
         '-U', f'erp_{slug}', '-d', f'erp_{slug}'],
        input=sql, capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(f'psql falhou: {(r.stderr or "").strip()[:300]}')
    return [linha for linha in (r.stdout or '').splitlines() if linha.strip()]


def pg_pronto(db_ctr, slug):
    # Por TCP: durante o initdb o entrypoint do Postgres sobe um servidor temporário só no socket
    # local, que cai logo depois — conectar nele faria um psql morrer no meio.
    r = subprocess.run(['docker', 'exec', db_ctr, 'pg_isready', '-h', '127.0.0.1', '-U', f'erp_{slug}',
                        '-d', f'erp_{slug}'], capture_output=True, timeout=30)
    return r.returncode == 0


def tamanho_banco(db_ctr, slug):
    linhas = _psql(db_ctr, slug, f"SELECT pg_database_size('erp_{slug}');")
    return int(linhas[0]) if linhas else 0


def manifesto_banco(db_ctr, slug):
    """Contagem **exata** de linhas de cada tabela de usuário e hash de `django_migrations`.

    {'tabelas': {'schema.tabela': n, ...}, 'migrations': n, 'migrations_hash': sha256}
    """
    tabelas = _psql(
        db_ctr, slug,
        "SELECT quote_ident(schemaname) || '.' || quote_ident(tablename) FROM pg_tables "
        "WHERE schemaname NOT IN ('pg_catalog', 'information_schema') ORDER BY 1;",
    )
    contagens = {}
    if tabelas:
        sql = '\nUNION ALL\n'.join(
            f"SELECT '{t.replace(chr(39), chr(39) * 2)}', count(*) FROM {t}" for t in tabelas
        ) + ';'
        for linha in _psql(db_ctr, slug, sql, timeout=1800):
            nome, _, n = linha.rpartition('|')
            contagens[nome] = int(n)
    migrations = []
    if 'public.django_migrations' in contagens:
        migrations = _psql(db_ctr, slug, 'SELECT app || \'.\' || name FROM django_migrations ORDER BY 1;')
    return {
        'tabelas': contagens,
        'migrations': len(migrations),
        'migrations_hash': hashlib.sha256('\n'.join(migrations).encode()).hexdigest(),
    }


def espaco_livre_docker(timeout=60):
    """Bytes livres no disco do diretório raiz do Docker deste nó."""
    return disco_docker(timeout)[0]


def disco_docker(timeout=60):
    """(livres, total) em bytes do disco do diretório raiz do Docker deste nó."""
    raiz = subprocess.run(['docker', 'info', '--format', '{{.DockerRootDir}}'], capture_output=True, text=True,
                          timeout=timeout)
    if raiz.returncode != 0 or not (raiz.stdout or '').strip():
        raise RuntimeError('Não foi possível descobrir o diretório raiz do Docker')
    r = subprocess.run(
        ['docker', 'run', '--rm', '-v', f'{raiz.stdout.strip()}:/raiz:ro', IMAGEM_AUXILIAR, 'df', '-P', '-k', '/raiz'],
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(f'df falhou: {(r.stderr or "").strip()[:200]}')
    ultima = (r.stdout or '').strip().splitlines()[-1].split()
    return int(ultima[3]) * 1024, int(ultima[1]) * 1024


def stacks_de_clientes_no_no(ignorar=None, timeout=30):
    """Quantos stacks com banco (`<stack>_db.<n>.<id>`) rodam neste nó, exceto o `ignorar` (o do CP)."""
    r = subprocess.run(['docker', 'ps', '--format', '{{.Names}}'], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f'docker ps falhou: {(r.stderr or "").strip()[:200]}')
    stacks = set()
    for nome in (r.stdout or '').split():
        stack, sep, resto = nome.partition('_db.')
        if sep and stack and stack != ignorar:
            stacks.add(stack)
    return len(stacks)


def sha256_arquivo(caminho, bloco=1024 * 1024):
    h = hashlib.sha256()
    with open(caminho, 'rb') as f:
        for parte in iter(lambda: f.read(bloco), b''):
            h.update(parte)
    return h.hexdigest()
