# Release: Servidor MCP do ERP por cliente (`<slug>_mcp`)

- data: 2026-11-15
- responsavel: James Flavio
- status: implementado; suíte completa verde (543 testes, 1 pulado); falta o roteiro no demo
- versao CP: `0.0.63`
- app: `registry`
- plano: `docs/projeto-agentes/mcp-erp-plano.md` (repositório do ERP), fase M2b

---

## 1. Problema
O ERP já tem o gateway de agentes (`/api/v2/agentes/`, M1) e o Servidor MCP (`mcp_erp`, M2) na
imagem, mas nenhum cliente tinha o serviço que o publica. Sem o CP criar e manter esse serviço,
o Hermes não tem como falar com o ERP de cada cliente por MCP.

## 2. O que mudou
- **Módulo `agents` ("Agentes (Servidor MCP)")** no catálogo da fixture, **fora de todos os
  planos**. O `carregar_dados_iniciais` acrescenta módulo novo aos planos que o listam, e isso
  ligaria o MCP sozinho. Ele é opt-in por cliente.
- **`registry/mcp.py`** (novo): regra de réplicas (1 com o módulo e o cliente fora de
  `STATUS_FORA_DO_AR`), env próprio e labels do Traefik.
- **Env sem segredos:** o serviço recebe só `ERP_INTERNAL_URL=http://web:8000`, `MCP_PUBLIC_URL`,
  `MCP_ALLOWED_HOSTS`, `MCP_PORT` e `ERP_VERSION`. Ele **nunca** copia o env do web e não tem volumes.
- **Traefik:**
  - um router `Host(<host>) && PathPrefix(/mcp)` → porta 8001 em cada host que serve o ERP (subdomínio e domínios `servir`); nada em domínio `redirecionar` nem no `www.`;
  - limite de taxa pelo `CF-Connecting-IP`;
  - o "Aplicar Configurações" remove o router de domínio que saiu.
- **`MotorProvisionamento`:**
  - bloco `mcp` no `stack.yml`, parado e fora do Traefik no stack de migração;
  - `sincronizar_servico_mcp()` (create/update), chamado em Atualizar versão (depois do cron), Aplicar Configurações (falha vira aviso) e reativar;
  - `suspender()` escala o MCP para 0.
- **Migração entre servidores:** religa o MCP ao final de `abrir()` e de `finalizar_reversao()`
  (com host trocado). Falha vira aviso no log, não intervenção.
- **`ALLOWED_HOSTS` do web ganha `web`** (`dominios.allowed_hosts`): sem isso o Django recusaria a
  chamada interna do MCP. `web` não entra em CSRF, Cloudflare nem `MCP_ALLOWED_HOSTS`.
- **Ajuda:** parágrafo do módulo Agentes em "Módulos e tema do site". **Doc:** `docs/registry/servico-mcp-por-cliente.md`.

## 3. Verificação
- `registry/tests/test_mcp.py` (21 testes):
  - réplicas por módulo × status;
  - módulo fora dos planos da fixture;
  - env sem segredo;
  - labels só em hosts `servir` e limite pelo IP do Cloudflare;
  - reconciliação de labels;
  - bloco do `stack.yml` (sem volumes, modo migração);
  - create/update do serviço, `traefik.enable` seguindo o do web;
  - suspender/reativar.
- Migração: o MCP é religado no destino e na volta, e a falha não pede intervenção.
- Testes antigos ajustados ao `ALLOWED_HOSTS` com `web` e à contagem de réplicas do stack de migração.
- Suíte completa 543 OK (1 pulado); `manage.py check` OK.

## 4. Deploy
- Script `.claude/skills/cp-release-docker/scripts/release_docker.ps1` (imagem `emporioautomacao/ararasuite-cp:0.0.63` e `:latest`).
- Atualizar pelo painel (Configurações > Verificar Atualizações).
- **Sem migration.** O módulo novo entra pelo `carregar_dados_iniciais` no boot.
- Ajustar `CP_VERSION` no stack do Portainer depois.

## 5. Depois do deploy
- Nenhum cliente muda sozinho. Só o próximo "Atualizar versão", "Aplicar Configurações" ou reativação
  de cada cliente cria o `<slug>_mcp`, com 0 réplicas sem o módulo, e acrescenta `web` ao `ALLOWED_HOSTS`
  (no "Aplicar Configurações").
- Roteiro no demo (saída da M2b):
  - marcar o módulo num cliente de teste e Aplicar Configurações;
  - `/mcp/health` 200 e `/mcp` sem token 401;
  - `docker service inspect <slug>_mcp` sem segredos nem volumes;
  - domínio `redirecionar` continua redirecionando;
  - suspender e reativar;
  - tirar o módulo e aplicar de novo;
  - chamada a `/mcp` com o cliente real do Hermes passando pelo Cloudflare.
- Emergência: `docker service scale <slug>_mcp=0`, depois tirar o módulo no CP, porque o próximo sync volta a seguir o módulo.
