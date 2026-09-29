# Release: Módulo Ordens de Serviço disponível por cliente

- data: 2026-09-29
- responsavel: James Flavio
- status: implementado
- versao: `0.0.40`
- app: `registry`
- arquivos modificados:
  - `core/settings.py` — `VERSION` 0.0.39 → 0.0.40
  - `registry/fixtures/initial_data.json` — novo `Modulo` slug `ordens_servico` ("Ordens de Serviço") + incluso no plano `enterprise`
  - `registry/templates/registry/admin_ajuda.html` — linha do módulo na tabela "Módulos disponíveis no ERP"
  - `CLAUDE.md` — tabela de módulos disponíveis

---

## 1. Objetivo

Disponibilizar no Painel de Controle o módulo **Ordens de Serviço** (slug `ordens_servico`) para ativação individual por cliente. O app já existia pronto no ERP, mas nunca tinha sido cadastrado no catálogo de módulos do CP — por isso não aparecia para seleção na tela do cliente (o próprio doc de escopo do módulo no repositório do ERP, `docs/modulos/ordens_servico/escopo-v1.md`, registrava essa pendência, citando `rh` como precedente do mesmo procedimento).

O módulo é genérico: O.S. em modo simples (aberta/em andamento/concluída/cancelada) ou por processo configurável, com etapas, decisões e ações automáticas (e-mail, WhatsApp, exigência de anexo).

## 2. Como funciona

- `Modulo` é um registro do banco do CP; a fixture `registry/fixtures/initial_data.json` é recarregada a cada deploy pelo `entrypoint.sh` (`manage.py loaddata`), então o módulo passa a existir no CP no próximo deploy — ou pode ser criado manualmente em **Clientes > Módulos** (slug `ordens_servico`, nome "Ordens de Serviço") sem esperar o deploy.
- O plano **Enterprise** passa a incluir `ordens_servico` por padrão. Starter e Pro não incluem — o admin pode adicionar individualmente em qualquer cliente.
- Para ativar em um cliente: tela do cliente → aba **Plano** → adicionar em **Módulos ativos** → salvar → **⚙ Aplicar Configurações**. Isso atualiza `MODULOS_ATIVOS` no serviço Docker da instância e reinicia o web (~30s).

## 3. Lado do ERP

- A instância lê `MODULOS_ATIVOS` (`core/settings.py`) e o acesso é controlado por `core/access_control.py::can_access_ordens_servico` (`modulo_ativo('ordens_servico')` + permissões `ordens_servico.*`), que gate a seção Ordens de Serviço na sidebar do admin.
- Distinto de `dedetizacao.OrdemServico`, que continua exclusivo do domínio de dedetização/controle de pragas (não foi tocado nem unificado — decisão deliberada, ver escopo do módulo).
- Documentação do módulo no repositório do ERP: `docs/modulos/ordens_servico/escopo-v1.md`.

## 4. Sem migração

`registry.Modulo`/`registry.Plano` já existiam; a mudança é só de dados (fixture). Confirmado com `makemigrations --check --dry-run`.

## 5. Deploy em produção

```bash
docker build -t emporioautomacao/ararasuite-cp:0.0.40 .
docker push emporioautomacao/ararasuite-cp:0.0.40

export $(cat /opt/cp/.env | xargs) && CP_VERSION=0.0.40 \
  docker stack deploy --with-registry-auth -c stack-prod.yml ararasuite-cp

docker service ps ararasuite-cp_web
```

O `entrypoint.sh` roda `manage.py loaddata registry/fixtures/initial_data.json` no boot do serviço `web`, então o módulo `ordens_servico` passa a existir no catálogo assim que o stack for atualizado para essa versão — sem precisar de ação manual adicional.

## 6. Como testar após o deploy

1. Admin do CP → **Clientes > Módulos** → confirmar que "Ordens de Serviço" aparece na lista.
2. Tela de um `Cliente` → confirmar que "Ordens de Serviço" aparece disponível em **Módulos ativos** e pode ser adicionado.
3. Cliente novo no plano Enterprise → confirmar que o módulo já vem incluso por padrão.
