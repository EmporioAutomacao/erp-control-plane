# Padrao: Encoding UTF-8 com PostgreSQL e Django no Windows

- data: 2026-05-14
- componente: padroes
- responsavel: Time ERP
- status: documentado-as-is

## 1. Contexto

Em 2026-05-14 foi identificado que os nomes de dois modulos estavam corrompidos na plataforma SaaS:

- `registry.Modulo(slug='notificacoes')`: `nome` com 14 caracteres em vez de 12
- `registry.Modulo(slug='cobranca')`: `nome` com 9 caracteres em vez de 8

Os caracteres `ç` (U+00E7) e `õ` (U+00F5) estavam armazenados como dois codepoints separados cada: `Ã` (U+00C3) + `§` (U+00A7) e `Ã` (U+00C3) + `µ` (U+00B5). Esse padrao e chamado de **double-encoding** ou **Mojibake**.

## 2. Sintoma no navegador

O navegador exibia `NotificaÃ§Ãµes` e `CobranÃ§a` mesmo com o header HTTP `Content-Type: text/html; charset=utf-8` correto e a tag `<meta charset="UTF-8">` presente no HTML.

A resposta HTTP continha os bytes `c3 83 c2 a7` onde deveria haver apenas `c3 a7` (UTF-8 de `ç`):

```
# errado (double-encoded):
hex:  4e6f746966696361 c383c2a7 c383c2b5 6573
utf8: Notifica         Ã§       Ãµ        es

# correto:
hex:  4e6f746966696361 c3a7 c3b5 6573
utf8: Notifica         ç    õ    es
```

## 3. Causa raiz

Os bytes UTF-8 dos caracteres acentuados (`c3 a7` para `ç`) foram armazenados no banco como **dois codepoints Latin-1 separados** (`0xC3` = Ã e `0xA7` = §), em vez de um unico codepoint Unicode (`0xE7` = ç).

Isso ocorreu porque ao gravar dados (via `manage.py loaddata` ou insercao manual) em um ambiente Windows sem especificar `encoding='utf-8'` explicitamente, o Python usou o encoding padrao do sistema (`Windows-1252` / `cp1252`), causando interpretacao errada dos bytes.

### Como detectar

Usar o shell do Django para inspecionar codepoints:

```python
from registry.models import Modulo
m = Modulo.objects.get(slug='notificacoes')
print(len(m.nome))  # correto: 12 | corrompido: 14
print([hex(ord(c)) for c in m.nome])
# correto:    [..., '0xe7', '0xf5', ...]
# corrompido: [..., '0xc3', '0xa7', '0xc3', '0xb5', ...]
```

Codepoints no intervalo `0x80`–`0xBF` em strings de texto sao bytes de continuacao UTF-8 que vazaram como caracteres — sinal inequivoco de double-encoding.

Verificar tambem via SQL:

```sql
SELECT slug, char_length(nome), octet_length(nome)
FROM registry_modulo
WHERE slug IN ('notificacoes', 'cobranca');
-- correto:    chars=12, bytes=14  (4 bytes para ç e õ em UTF-8)
-- corrompido: chars=14, bytes=18  (8 bytes para os 4 codepoints corrompidos)
```

## 4. Correcao aplicada

Os registros foram corrigidos com UPDATE SQL via Django ORM, usando codepoints Unicode corretos e lendo o script de correcao com `encoding='utf-8'` explicitamente:

```python
from django.db import connection

fixes = {
    'notificacoes': 'Notificações',  # ç=U+00E7, õ=U+00F5
    'cobranca': 'Cobrança',
}

for slug, correct_nome in fixes.items():
    with connection.cursor() as cur:
        cur.execute(
            "UPDATE registry_modulo SET nome = %s WHERE slug = %s",
            [correct_nome, slug]
        )
```

Usar escapes Unicode (`ç`) no codigo-fonte elimina qualquer ambiguidade de encoding do arquivo `.py` — o Python sempre interpreta `\uXXXX` como o codepoint correspondente, independente do encoding do arquivo.

## 5. Regra: como regerar fixtures com encoding correto

### Problema

`manage.py dumpdata` grava no arquivo usando o encoding padrao do sistema. No Windows, isso frequentemente resulta em `Windows-1252` ao usar `>` ou `Out-File` no PowerShell, corrompendo caracteres acentuados no fixture.

### Solucao correta

Capturar a saida do `dumpdata` via `io.StringIO` dentro do Python e gravar o arquivo com `encoding='utf-8'` explicitamente:

```python
import io
from django.core.management import call_command

buf = io.StringIO()
call_command('dumpdata', 'registry.Modulo', 'registry.Plano', '--indent', '2', stdout=buf)
json_data = buf.getvalue()

with open('registry/fixtures/initial_data.json', 'w', encoding='utf-8', newline='\n') as f:
    f.write(json_data)
```

Executar via:

```
python manage.py shell -c "exec(open('script.py', encoding='utf-8').read())"
```

### Verificar o arquivo gerado

```python
data = open('registry/fixtures/initial_data.json', 'rb').read()
print('BOM:', data[:3].hex())       # NAO deve ser efbbbf (UTF-8 BOM)
idx = data.find(b'Notifica')
print(data[idx:idx+20].hex())       # deve conter c3a7 e c3b5, nao e7 nem f5 isolados
```

### O que NAO fazer

```powershell
# ERRADO - usa encoding do sistema (Windows-1252 no Windows):
python manage.py dumpdata registry > fixture.json
python manage.py dumpdata registry | Out-File fixture.json

# ERRADO - adiciona BOM UTF-8 e pode corromper caracteres via console:
python manage.py dumpdata registry | Out-File -Encoding utf8 fixture.json
```

## 6. Regra: abrir arquivos com encoding explicito

Sempre que ler ou gravar arquivos de texto com conteudo potencialmente acentuado no ambiente Windows, especificar `encoding='utf-8'` explicitamente:

```python
# correto:
with open('arquivo.json', 'r', encoding='utf-8') as f:
    data = f.read()

with open('arquivo.json', 'w', encoding='utf-8', newline='\n') as f:
    f.write(data)

# tambem correto para scripts Python executados com exec():
exec(open('script.py', encoding='utf-8').read())
```

## 7. Regra: usar escapes Unicode em strings criticas

Em scripts de migracao ou correcao de dados que contenham caracteres acentuados, preferir escapes Unicode para evitar dependencia do encoding do arquivo-fonte:

```python
# preferir:
nome = 'Notificações'   # inequivoco em qualquer ambiente

# evitar (depende do encoding do .py):
nome = 'Notificações'
```

## 8. Pontos de atencao

- O banco PostgreSQL e o Django estavam ambos configurados com `client_encoding = UTF8` e `server_encoding = UTF8`; o problema era exclusivamente na escrita/leitura de arquivos no sistema operacional Windows.
- O fixture `registry/fixtures/initial_data.json` foi regerado corretamente apos a correcao e verificado com inspeção direta dos bytes (`c3 a7` e `c3 b5` para `ç` e `õ`).
- O ambiente de producao (Linux) nao apresenta este problema porque o encoding padrao do sistema e UTF-8. A atencao e necessaria apenas em desenvolvimento e scripts executados no Windows.

## 9. Documentos relacionados

- [registry/visao-geral.md](../registry/visao-geral.md) — app onde o bug foi encontrado
- [landing/visao-geral.md](../landing/visao-geral.md) — landing page que exibia os nomes corrompidos
