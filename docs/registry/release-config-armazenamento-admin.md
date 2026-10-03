# Release: Conexão do S3 (backups) em Configurações > Armazenamento, com sinalizador de status

- data: 2026-10-03
- responsavel: James Flavio
- status: implementado e validado localmente com RustFS — pendente de deploy
- versao CP: `0.0.43`
- app: `registry`
- repo: `erp-control-plane` (nada muda no `erp`)
- guia completo do backup multi-nó: `docs/registry/backup-multi-no.md`

---

## 1. Objetivo

A conexão com o bucket dos backups (RustFS) vinha de variáveis `BACKUP_S3_*` no `.env`/stack. Agora é
cadastrada **no admin** (Configurações > Armazenamento), com um **sinalizador de status** que mostra se o CP
está conseguindo falar com o bucket.

## 2. O que mudou

- **`ConfiguracaoArmazenamento`** (`registry/models.py`, singleton pk=1, migration `0020`): `endpoint`,
  `endpoint_publico`, `bucket`, `access_key`, `secret_key_cifrada`, `regiao` + sinalizador (`status_conexao`,
  `status_mensagem`, `verificado_em`).
- **Secret key criptografada** (`registry/cripto.py`): Fernet com chave derivada da `SECRET_KEY`. Nunca é
  reexibida no formulário; em branco ao editar = mantém a atual. Nova dependência: `cryptography`.
- **`registry/storage_backup.py`** lê a configuração do banco (nada de `os.environ`). Novo
  `testar_conexao()`, `registrar_status()`, `registrar_envio_ok()`, `verificar_e_registrar()` e mensagens de
  erro do boto3 traduzidas para português.
- **Sem fallback para o `.env`**: sem cadastro completo, `task_iniciar_backup_cliente` e `task_backup_cliente`
  abortam **antes do dump** com *"Armazenamento de backups não configurado. Acesse Admin → Configurações →
  Armazenamento."*. Upload manual e download também avisam em vez de quebrar. Não existe mais o modo
  "backup só no disco do nó" para backups novos; backups antigos com caminho local continuam podendo ser
  baixados e restaurados.
- **`task_verificar_armazenamento`**: testa a conexão e atualiza o sinalizador (agendar no beat).
- **Admin** (`ConfiguracaoArmazenamentoAdmin`): pill de status na tela e na lista, consulta ao vivo ao abrir a
  tela (`verificar/`, JSON), botão **🔌 Testar conexão agora** (`testar-conexao/`), teste automático ao salvar.
- **Sidebar**: item "Armazenamento" em Configurações.
- **Infra**: `BACKUP_S3_*` removidas de `stack-prod.yml` e `.env.example`.

## 3. O sinalizador

| Status | Quando |
|---|---|
| 🟢 Conectado | `head_bucket` ok **e** um objeto de teste (`_cp-healthcheck/…`) foi gravado, lido e apagado |
| 🟡 Atenção | bucket ok, mas `GET <endpoint público>/health` falhou — backups funcionam, ⬇ Baixar pode falhar |
| 🔴 Erro | endpoint inacessível, bucket inexistente, credenciais recusadas, sem permissão, timeout ou secret ilegível |
| ⚪ Não testado | configuração incompleta |

Atualiza: ao abrir a tela, ao salvar, no botão de teste, a cada envio de backup (um envio bem-sucedido recupera
de *Erro*, mas **não** apaga um *Atenção*, pois não prova o endpoint público) e pela task agendada.
Timeouts curtos (connect 5s / read 10s, sem retries) para o admin não travar.

## 4. Migração / deploy

1. Imagem nova (traz `cryptography`); redeploy do stack do CP. A migration `0020` roda no `celery`.
2. **Admin > Configurações > Armazenamento**: endpoint de acesso `http://rustfs-s3:9000`, endpoint público
   `https://<S3_DOMAIN>`, bucket `cp-backups`, a access key dedicada. Conferir 🟢 Conectado.
3. Opcional: `PeriodicTask` para `registry.tasks.task_verificar_armazenamento` (ex.: a cada 15 min).
4. Os `BACKUP_S3_*` que existirem no `.env` de produção passam a ser **ignorados**.

> Trocar a `SECRET_KEY` do CP deixa a secret do S3 ilegível (status *Erro*): informe a secret key de novo e salve.

## 5. Validação (local, Docker Desktop + RustFS, 2026-10-03)

| Cenário | Resultado |
|---|---|
| Backup sem configuração | `erro` imediato com a mensagem de configuração |
| Endpoint inalcançável | status 🔴 com mensagem em português; secret cifrada no banco |
| Corrigir o endpoint (secret em branco) | 🟢 Conectado; secret anterior mantida |
| Endpoint público inválido | 🟡 Atenção; um backup bem-sucedido não apaga o aviso |
| Backup, link de download, restauração | ok (28 MB; 465 → 460 → 465 linhas restauradas) |
| RustFS parado | tela mostra 🔴 ao vivo; backup vai para `erro` sem resíduo; ao religar volta a 🟢 |

Testes automatizados: `manage.py test registry` — 122 testes OK (novos: `test_armazenamento.py` com
criptografia, `testar_conexao`, admin, tasks sem configuração; `helpers_storage.py` com bucket falso para os
testes de backup).

## 6. Fora de escopo

- Validação em Swarm real (`agente-no`, filas `no.<hostname>`, rede `storage-net`) segue pendente.
- Rotação da chave de criptografia (hoje atrelada à `SECRET_KEY`).
