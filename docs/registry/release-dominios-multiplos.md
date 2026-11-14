# Release: vários domínios próprios por cliente

- data: 2026-11-14
- responsavel: James Flavio
- status: implementado; suite `registry` verde (1 E2E opcional pulado)
- versao CP: `0.0.61`
- app: `registry`
- doc de referência: `docs/registry/dominios-multiplos.md`

---

## 1. Problema
Cada cliente tinha só um `Cliente.dominio_custom`. Um segundo domínio só entrava por label à mão
no Swarm, e o próximo **Aplicar Configurações** reescrevia `ALLOWED_HOSTS` e o derrubava com
`DisallowedHost`. Limpar ou trocar o domínio também não removia os routers `{slug}-erp-custom*`,
o ingress do tunnel nem o CNAME — o domínio antigo continuava chegando ao container.

## 2. O que mudou
- **Modelo `DominioCliente`** (aba **Domínios** do Cliente): `dominio`, `modo`
  (`servir` | `redirecionar`), `principal` (no máximo um, sempre `servir`), `incluir_www`.
  `Cliente.dominio_custom` sai; `Cliente.host_publico`/`url` = principal ou subdomínio;
  `Cliente.dominios_aplicados` guarda o que foi publicado no Cloudflare.
- **Migration `0035_dominios_cliente`:** cada `dominio_custom` vira domínio principal, "abre o ERP",
  com www (exatamente o que estava publicado), e `dominios_aplicados = [d, www.d]`. Reversível.
- **`registry/dominios.py`:** normalização (esquema, caminho, porta, maiúsculas, punycode), env,
  labels do Traefik e reconciliação (`--label-rm` do que saiu, nos prefixos gerenciados).
- **`registry/cloudflare.py`** (saiu de `admin._configurar_dominio_cloudflare`): todos os domínios,
  zona achada subindo os níveis, remoção dos que saíram (só CNAME que aponta para o tunnel; zona
  nunca), nada é removido se o hostname hoje é de outro cliente, aviso de certificado para nome dois
  níveis abaixo da zona.
- **Admin:** inline Domínios (somente leitura com migração ativa), aviso para aplicar ao salvar,
  troca de principal num salvamento só, painel de acesso lista os endereços. **Aplicar
  Configurações** passa a reconciliar env + Traefik + Cloudflare; **☁ Configurar Cloudflare** refaz
  só o Cloudflare.
- **`abas_cliente.js`:** lembra também a aba dos inlines e abre o inline com erro de validação.
- `swarm.labels_do_servico`; `cp_pull`/`cp_push` usam `Cliente.host_publico`.
- Ajuda: seção **Domínios próprios** reescrita (como funciona, fluxo, exemplos práticos, como
  conferir, casos especiais).

## 3. Verificação
- Unitários e admin: `registry/tests/test_dominios.py`; navegador: `test_dominios_playwright.py`.
- Traefik real v2.11 e v3.6 com as labels geradas: 200 / 301 (www e redirect, caminho e
  querystring mantidos) / 308 em POST/PUT / 404 sem www.
- `docker stack config` do `stack.yml` gerado: `$${1}` interpolado para `${1}`.
- Swarm local real (`test_dominios_swarm_e2e.py`, `CP_E2E_SWARM=1`): serviço com labels da 0.0.60
  → Aplicar Configurações → routers legados removidos, novos adicionados, segundo Aplicar remove o
  domínio tirado da lista; Traefik em modo Swarm serve só o esperado.
- Migration 0035 ida e volta no banco de dev com domínio preenchido.

## 4. Deploy
Script `.claude/skills/cp-release-docker/scripts/release_docker.ps1` (imagem
`emporioautomacao/ararasuite-cp:0.0.61` e `:latest`). Atualizar pelo painel (Configurações >
Verificar Atualizações). **Tem migration** (`0035`, roda no boot do `web`). Ajustar `CP_VERSION`
no stack do Portainer depois.

## 5. Depois do deploy
- Nada muda nos clientes até alguém clicar em **Aplicar Configurações**: os routers antigos
  continuam valendo e equivalem aos novos.
- Primeiro Aplicar num cliente com domínio próprio, de baixo risco; conferir
  `docker service inspect {slug}_web --format '{{json .Spec.Labels}}'` (routers
  `{slug}-erp-custom-<dominio>`, nenhum `{slug}-erp-custom.` nem `{slug}-www-redirect`) e abrir
  o domínio e o `www.` no navegador.
- Não pôr em "Redireciona" domínio onde um SyncAgent/PDV foi ativado (o redirect descarta o token).

## 6. Limitações conhecidas
- **Destruir** cliente não limpa o Cloudflare (já não limpava).
- API do Cloudflare testada só com simulação.
