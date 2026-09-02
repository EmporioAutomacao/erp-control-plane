# Release: Ajuda de ativação do SyncAgent + limpeza do SAAS_DOMAIN

- data: 2026-09-01
- responsavel: James Flavio
- status: implementado
- versao: `0.0.29`
- app: `registry`

---

## 1. O que mudou

- `registry/templates/registry/admin_ajuda.html` — nova seção
  **"Sincronização — ativação do SyncAgent"**: o que é, pré-requisitos no CP
  (Atualizar versão ≥ 0.0.96 + Aplicar Configurações), e tabela de erros de
  ativação com causa e correção (`Configure o ID do cliente no CP`,
  `invalid_response`, `activation_code_used/_expired/_revoked`, `tenant_invalid`,
  `erp_unreachable`, `Client certificate is required`, `23505 operators_login_key`,
  `Aguardando ativação com o ERP`).
- `CLAUDE.md` — documenta o fix do `erp_api_base_url` (ERP 0.0.96 /
  `_resolve_external_base_url`), o mTLS (`RequireMutualTls=false`) e aponta para
  o guia canônico `docs/infra/sync-agent-ativacao-troubleshooting.md` do repo
  `erp`.
- `core/settings.py` — `SAAS_DOMAIN` tinha lixo de texto no default (paste
  acidental na cópia de trabalho, nunca commitado); restaurado para
  `os.getenv('SAAS_DOMAIN', 'ararasuite.com.br')`. O commitado já estava
  correto; sem diff.

## 2. Sem migração

Nenhuma mudança de modelo/banco. É só deploy da imagem.

## 3. Deploy em produção

```bash
docker build -t emporioautomacao/ararasuite-cp:0.0.29 .
docker push emporioautomacao/ararasuite-cp:0.0.29
docker tag emporioautomacao/ararasuite-cp:0.0.29 emporioautomacao/ararasuite-cp:latest
docker push emporioautomacao/ararasuite-cp:latest

export $(cat /opt/cp/.env | xargs) && CP_VERSION=0.0.29 \
  docker stack deploy --with-registry-auth -c stack-prod.yml ararasuite-cp
docker service ps ararasuite-cp_web
```

## 4. Como conferir

Admin do CP → sidebar **Ajuda** → seção "Sincronização — ativação do SyncAgent"
com a tabela de erros.

## 5. Contexto

Durante a homologação da ativação do SyncAgent contra
`https://demo.ararasuite.com.br` foram identificados e corrigidos: (1) o ERP
atrás do Traefik devolvia `erp_api_base_url` em `http://` no `activation:complete`
(fix no repo `erp` 0.0.96); (2) o instalador do agente forçava
`RequireMutualTls=true` sem o ERP fazer mTLS (fix no repo `pdv-local` 1.3.2);
(3) import de operadores abortava com colisão de `login` ao reaproveitar
instalação entre clientes (fix no `pdv-local` 1.3.1). Esta release do CP só
documenta tudo isso na Ajuda do admin.
