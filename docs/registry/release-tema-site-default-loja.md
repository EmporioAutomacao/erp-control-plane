# Release: Corrige tema padrão do provisionamento para "Loja (claro premium)"

- data: 2026-09-25
- responsavel: James Flavio
- status: implementado
- versao: `0.0.38`
- app: `registry`
- arquivos modificados:
  - `core/settings.py` — `VERSION` 0.0.37 → 0.0.38
  - `registry/models.py` — `Cliente.tema_site`: `default` `'padrao'` → `'loja'`, removida a opção `'padrao'` de `TEMA_CHOICES`
  - `registry/provisioning.py` — `MotorProvisionamento.executar()`: fallback `cliente.tema_site or 'padrao'` → `or 'loja'`
  - `registry/admin.py` — `_view_aplicar_modulos()` (botão "Aplicar Configurações"): mesmo fallback corrigido
  - `registry/migrations/0018_alter_cliente_tema_site.py`

---

## 1. Sintoma

Cliente novo, cadastrado pelo formulário público sem escolher tema explicitamente, era provisionado com o layout genérico de fallback (`core/templates/padrao/` no ERP — título "Soluções em Automação Comercial") em vez do tema **Loja (claro premium)**, que é o tema padrão do ERP desde a release
[`release-loja-padrao-multiestoque.md`](../../../erp/docs/modulos/site-produtos-erp/release-loja-padrao-multiestoque.md)
(ERP, 2026-07-14).

## 2. Causa

O ERP tornou `'loja'` o default de `ConfiguracaoSite.tema` e removeu `'padrao'` do próprio seletor nessa release — mas o **CP não foi atualizado junto**. `Cliente.tema_site` (`registry/models.py`) continuava com `default='padrao'`, e `MotorProvisionamento.executar()` (`registry/provisioning.py:48`) grava esse valor na variável de ambiente `TEMA_SITE` do stack do cliente:

```python
tema = self.cliente.tema_site or 'padrao'
...
env_vars = {..., 'TEMA_SITE': tema, ...}
```

O ERP lê `TEMA_SITE` como fallback quando não há `ConfiguracaoSite` gravada no banco (`core/settings.py::TEMA_SITE`, default `'loja'` só quando a env var **não está definida** — aqui ela chegava sempre definida como `'padrao'`, sobrepondo o default correto do próprio ERP). O mesmo fallback `'padrao'` também existia em `_view_aplicar_modulos()` (botão "Aplicar Configurações" no admin do CP).

Os dois repositórios (`erp` e `cp`) têm cada um sua própria cópia da lista de temas (`ConfiguracaoSite.TEMA_CHOICES` no ERP, `Cliente.TEMA_CHOICES` no CP) e ficaram dessincronizados.

## 3. Correção

- `Cliente.tema_site`: `default='loja'`, `'padrao'` removido de `TEMA_CHOICES` (espelha exatamente a decisão já tomada no ERP).
- Os dois fallbacks `cliente.tema_site or 'padrao'` (`provisioning.py` e `admin.py`) → `or 'loja'`.
- Migration `0018_alter_cliente_tema_site.py` (só `choices`/`default`, sem alteração física de schema).

**Clientes já provisionados com `tema_site='padrao'` continuam funcionando** — o `ThemeLoader` do ERP sempre inclui `core/templates/padrao/` como fallback de qualquer tema. Nenhum dado de cliente existente foi alterado por este release; o efeito é só nos próximos cadastros (e em qualquer "Aplicar Configurações"/"Re-provisionar" disparado manualmente para um cliente que ainda esteja com `tema_site` vazio).

## 4. Sem migração de dados

Só `AlterField` de metadado (`choices`/`default`). Nenhum `Cliente` existente é alterado por esta migration.

## 5. Deploy em produção

```bash
# 1. Build + push da imagem do CP com a nova tag
docker build -t emporioautomacao/ararasuite-cp:0.0.38 .
docker push emporioautomacao/ararasuite-cp:0.0.38

# 2. No servidor: apontar CP_VERSION e re-deployar o stack
export $(cat /opt/cp/.env | xargs) && CP_VERSION=0.0.38 \
  docker stack deploy --with-registry-auth -c stack-prod.yml ararasuite-cp

# 3. Conferir rollout
docker service ps ararasuite-cp_web
```

O `entrypoint.sh` roda `migrate` no start — aplica a migration 0018 (só metadado, instantânea).

## 6. Como testar após o deploy

1. Admin do CP → **Registry → Clientes → Adicionar**: campo "Tema do site" já nasce marcado em **Loja (claro premium)**, sem a opção "Padrão (e-commerce)" na lista.
2. Cadastrar um cliente novo pelo formulário público (`landing`) sem tocar em nenhuma opção de tema (campo não é exposto lá) → provisionar → abrir a URL do cliente → confirma tema Loja (não mais o fallback genérico).
3. Cliente já existente com `tema_site` vazio: rodar **"Aplicar Configurações"** → `.env` do stack passa a ter `TEMA_SITE=loja`.

## 7. Pendente (fora do escopo deste release)

Instâncias **já provisionadas antes deste fix** com `TEMA_SITE=padrao` gravado no `.env` do stack (ex.: cliente Ferragista Agrofonte) não são corrigidas automaticamente — não há backfill em massa aqui. Correção é manual, por cliente: **Configurações → Site** no ERP daquela instância → trocar "Tema da landing page" para **Loja (claro premium)** → salvar (o `ConfiguracaoSite` do banco tem prioridade sobre a env `TEMA_SITE`, então isso corrige o site sem precisar reprovisionar).
