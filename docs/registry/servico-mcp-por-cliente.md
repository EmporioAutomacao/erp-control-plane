# Serviço `<slug>_mcp`: Servidor MCP do ERP por cliente

Fase M2b do plano do MCP. O plano completo, com invariantes, fases e verificações, está no repositório do ERP em `docs/projeto-agentes/mcp-erp-plano.md`.

## O que é

Cada cliente pode ter um serviço Swarm `<slug>_mcp`. Ele roda o Servidor MCP do ERP (`python -m mcp_erp`, uvicorn na porta 8001), publicado em `https://<host do cliente>/mcp`. O agente Hermes chama esse endereço com um `AgentToken`. O MCP só traduz o protocolo para o gateway de agentes do ERP (`http://web:8000/api/v2/agentes/`). Permissão, catálogo e auditoria ficam todos no ERP.

## Como liga e desliga

- **Módulo `agents`** do cliente (`Cliente.modulos_ativos`), marcado no admin e aplicado com **⚙ Aplicar Configurações**. O módulo não faz parte de nenhum plano: é opt-in por cliente.
- Réplicas (`registry/mcp.py::replicas_desejadas`): 1 com o módulo e o cliente fora de `STATUS_FORA_DO_AR` (mesma regra do cron); senão 0.
- Sem o módulo, o serviço existe com 0 réplicas, e o gateway do ERP também nega tudo, porque `MODULOS_ATIVOS` não tem `agents`.

## Segurança

- **O env não é copiado do web**, diferente do cron. O serviço recebe só `ERP_INTERNAL_URL`, `MCP_PUBLIC_URL`, `MCP_ALLOWED_HOSTS`, `MCP_PORT` e `ERP_VERSION` (`registry/mcp.py::env`). Ele não tem volumes. O `/mcp` é exposto na internet e não pode carregar `POSTGRES_PASSWORD`, `DJANGO_SECRET_KEY`, `CP_SHARED_SECRET` nem a chave mestra.
- Nenhum token fica no serviço: o `AgentToken` vive no Hermes e é validado no ERP a cada requisição.
- O `ALLOWED_HOSTS` do web ganha `web` (`dominios.allowed_hosts`). Sem isso, o Django recusaria a chamada interna do MCP. `web` não entra em CSRF, Cloudflare nem `MCP_ALLOWED_HOSTS`.

## Roteamento (Traefik)

- Um router `Host(<host>) && PathPrefix(/mcp)` para cada host que **serve** o ERP: o subdomínio e os domínios `servir`. Não há router `/mcp` em domínio `redirecionar` nem no `www.`. Como a regra é mais longa que `Host(...)`, ela venceria o router de redirect.
- Limite de taxa por IP real (`CF-Connecting-IP`): o tráfego chega pelo Cloudflare. O limite por agente fica no ERP.
- O "Aplicar Configurações" reconcilia as labels com os prefixos `<slug>-mcp`: o router `/mcp` de um domínio removido sai.
- `traefik.enable` segue o do web.

## Onde o CP sincroniza

| Momento | O que acontece |
|---|---|
| Provisionamento / Re-provisionar | serviço `mcp` no `stack.yml` |
| Atualizar versão | `sincronizar_servico_mcp(versao)` depois do web (migrations) e do cron |
| Aplicar Configurações | `sincronizar_servico_mcp()`: réplicas pelo módulo, env e routers pelos domínios atuais (falha vira aviso) |
| Suspender / Cancelar / Trial expirado | `<slug>_mcp` escalado para 0 |
| Reativar | `sincronizar_servico_mcp()` (cria em cliente antigo) |
| Migração entre servidores | stack de migração com o MCP em 0 e fora do Traefik; religado ao final de `abrir()` e de `finalizar_reversao()` (falha vira aviso no log, não intervenção) |
| Restauração de backup | não mexe: o MCP não escreve em banco nem em mídia, e com o web parado responde 503 |

## Desligar em emergência

`docker service scale <slug>_mcp=0` funciona na hora, mas o próximo "Aplicar Configurações" ou "Atualizar versão" volta a seguir o módulo. O desligamento definitivo é tirar o módulo `agents` do cliente e clicar em Aplicar Configurações.
