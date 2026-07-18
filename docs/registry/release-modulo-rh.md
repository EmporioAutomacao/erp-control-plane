# Release: Módulo Recursos Humanos (RH) disponível por cliente

- data: 2026-07-17
- responsavel: James Flavio
- status: implementado
- app: `registry`
- arquivos modificados:
  - `registry/fixtures/initial_data.json` — novo `Modulo` slug `rh` ("Recursos Humanos") + incluso no plano `enterprise`
  - `registry/templates/registry/admin_ajuda.html` — linha do módulo na tabela "Módulos disponíveis no ERP"
  - `CLAUDE.md` — tabela de módulos disponíveis

---

## 1. Objetivo

Disponibilizar no Painel de Controle o módulo **Recursos Humanos** (slug `rh`) para ativação individual por cliente, acompanhando o lançamento do módulo RH no ERP (funcionários, folha de pagamento/contracheque, VT/VA, plano de saúde, adiantamentos, férias, 13º, atestados/advertências/documentos, ASO e EPI — títulos gerados em Financeiro > A Pagar).

## 2. Como funciona

- `Modulo` é um registro do banco do CP; a fixture `registry/fixtures/initial_data.json` é recarregada a cada deploy pelo `entrypoint.sh` (`manage.py loaddata`), então o módulo passa a existir no CP no próximo deploy — ou pode ser criado manualmente em **Clientes > Módulos** (slug `rh`, nome "Recursos Humanos") sem esperar o deploy.
- O plano **Enterprise** passa a incluir `rh` por padrão. Starter e Pro não incluem — o admin pode adicionar individualmente em qualquer cliente.
- Para ativar em um cliente: tela do cliente → aba **Plano** → adicionar em **Módulos ativos** → salvar → **⚙ Aplicar Configurações**. Isso atualiza `MODULOS_ATIVOS` no serviço Docker da instância e reinicia o web (~30s).

## 3. Lado do ERP

- A instância lê `MODULOS_ATIVOS` (`core/settings.py`) e o acesso é controlado por `core/access_control.py::can_access_rh` (`modulo_ativo('rh')` + permissões `rh.*`), que gate a seção RH na sidebar do admin.
- Documentação do módulo no repositório do ERP: `docs/modulos/rh/escopo-v1.md` e `docs/modulos/rh/manual-operacao.md`.
