# Release: preços dos planos voltavam para 10/20/30 sozinhos

- data: 2026-10-23
- responsavel: James Flavio
- status: implementado e testado localmente — pendente de deploy
- versao CP: `0.0.50`
- app: `registry`
- repo: `erp-control-plane` (nada muda no `erp`)

---

## 1. Problema

Os preços editados em **Clientes > Planos** voltavam, pouco tempo depois, para `Starter = R$ 10`, `Pro = R$ 20` e
`Enterprise = R$ 30`.

Causa: o `entrypoint.sh` do serviço `web` rodava `manage.py loaddata registry/fixtures/initial_data.json` **a cada boot**. O
`loaddata` grava os objetos da fixture pela PK e **sobrescreve** os existentes. Qualquer reinício do `web` (deploy, atualização
pelo painel, `restart_policy` do Swarm, troca de nó) revertia os planos para a fixture.

Não eram só os preços: nome, descrição, limites (`max_usuarios`, `max_empresas`, CPU/RAM), `ativo`, `destaque`, `ordem` e os
**módulos inclusos** de cada plano também voltavam, e o mesmo valia para os campos dos `Modulo`.

## 2. O que mudou

- **`registry/management/commands/carregar_dados_iniciais.py`** (novo): lê a mesma fixture e:
  - cria os `Modulo` e `Plano` que **ainda não existem** no banco;
  - **não altera** nenhum registro existente;
  - quando um `Modulo` é criado agora (release que adiciona um módulo, como foi com `rh` e `ordens_servico`), ele é incluído
    nos `Plano` já existentes que o listam em `modulos_inclusos` na fixture — preserva o fluxo de release de módulo novo;
  - tudo numa transação; imprime quantos registros foram criados.
- **`entrypoint.sh`**: `loaddata` trocado por `python manage.py carregar_dados_iniciais`.

A fixture `initial_data.json` continua sendo a fonte do **ambiente novo** (banco vazio): nele o comando cria tudo, como o
`loaddata` fazia.

## 3. Regras para o futuro

- **Não usar `loaddata` da `initial_data.json` em um banco que já tem dados** (nem em dev): reverte as edições do admin.
- Alterar o preço/limites de um plano **existente** = editar no admin. Mudar a fixture só afeta ambientes novos.
- Módulo novo: adicionar na fixture (e no `modulos_inclusos` dos planos que devem incluí-lo); entra no próximo boot.
- Remover um módulo de um plano na fixture **não** remove em bancos existentes (o comando só adiciona).

## 4. Testes

`registry/tests/test_carregar_dados_iniciais.py`:

| Teste | Verifica |
|---|---|
| `test_cria_planos_e_modulos_ausentes` | banco vazio → planos, módulos e M2M criados |
| `test_nao_sobrescreve_edicoes_do_admin` | preço `199.90`, nome e remoção de módulo do plano sobrevivem a uma nova execução |
| `test_modulo_novo_entra_nos_planos_existentes_que_o_listam` | módulo recriado entra no `enterprise` e não no `starter` |

## 5. Deploy

Imagem nova + atualizar o stack do CP. **Depois do deploy, ajustar os preços dos planos uma última vez** no admin — o boot da
versão nova não mexe mais neles. Até o deploy, qualquer reinício do `web` ainda reverte os valores.
