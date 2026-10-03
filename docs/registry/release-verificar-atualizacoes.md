# Release: Configurações > Verificar Atualizações (versão atual × disponível + botão Atualizar)

- data: 2026-10-03
- responsavel: James Flavio
- status: implementado — verificação de versões validada contra o Docker Hub real; **atualização pelo painel ainda não validada em Swarm real**
- versao CP: `0.0.44`
- app: `registry`
- repo: `erp-control-plane` (nada muda no `erp`)

---

## 1. Objetivo

O CP não tinha como saber se havia uma versão nova de si mesmo. Agora **Configurações > Verificar Atualizações**
mostra a **versão atual** (`settings.VERSION`), a **versão disponível** (maior tag `X.Y.Z` da imagem no Docker Hub)
e permite **atualizar pelo painel**.

## 2. O que mudou

- **`ConfiguracaoAtualizacao`** (`registry/models.py`, singleton pk=1, migration `0021`): repositório
  (padrão `emporioautomacao/ararasuite-cp`), usuário e token do Docker Hub (token **criptografado**,
  `registry/cripto.py`), status da conexão, cache da última consulta e registro da última atualização iniciada.
- **`registry/atualizacoes.py`** (novo): consulta ao registry (token → `tags/list`, com paginação), comparação
  numérica de versões, descoberta do stack/serviços do Swarm e o disparo da atualização.
- **`ConfiguracaoAtualizacaoAdmin`**: tela com versão atual, disponível, situação (**Atualizado / Atualização
  disponível / Não foi possível verificar / Não verificado**), versões recentes, botões **Verificar agora** e
  **Atualizar para X**, status da conexão ao vivo e andamento da última atualização (com log).
- **`task_verificar_atualizacao`**: atualiza a versão disponível (agendável no beat).
- Sidebar: item **Verificar Atualizações** em Configurações; tela de confirmação
  `registry/templates/registry/admin_confirmar_atualizacao.html`.

## 3. Docker Hub

- A lista de tags de `emporioautomacao/ararasuite-cp` é **pública**: a consulta funciona sem credenciais
  (verificado em 2026-10-03). Usuário e token são **opcionais** — só necessários se o repositório for tornado
  privado. Nesse caso use um *Personal Access Token* **somente leitura**.
- Só tags no formato `X.Y.Z` entram (ignora `latest`, `dev`, pré-releases). A comparação é numérica
  (`0.0.10 > 0.0.9`).
- A tela consulta ao abrir (resultado reaproveitado por 60 s); **Verificar agora** força a consulta.

## 4. Como a atualização funciona

O CP não pode atualizar o próprio `web`/`celery` dentro da requisição (o processo seria encerrado no meio).
Por isso o botão dispara um **container auxiliar destacado** (`docker run -d`, mesma imagem atual, com
`docker.sock` e `/root/.docker`) que executa, em ordem:

```
docker service update --with-registry-auth --update-failure-action rollback --detach=false \
    --image emporioautomacao/ararasuite-cp:<versão> <stack>_<serviço>
```

para `celery` → `beat` → `agente-no` → `web` (a própria tela, por último). `db` e `redis` nunca são tocados.
O stack é descoberto pelo label `com.docker.stack.namespace` do próprio container.

**Travas:** só superusuário; só versões que existem no Docker Hub e são **maiores** que a atual (sem downgrade —
migrations não são reversíveis); só em Swarm manager (em dev o botão não aparece); sem atualização em andamento;
confirmação em tela antes de executar.

**Acompanhamento:** o auxiliar fica (sem `--rm`) guardando o log; a tela mostra *Em andamento / Concluída /
Falhou* e as últimas linhas. O auxiliar anterior é removido na próxima execução.
Log manual: `docker logs cp-atualizador-<versão>-<timestamp>`.

> ⚠ **Ajuste `CP_VERSION` no stack do Portainer** para a nova versão depois de atualizar. O painel muda os
> serviços direto no Swarm; o Portainer continua com a versão antiga e um redeploy do stack **voltaria o CP**.
> A tela e a mensagem de sucesso repetem esse aviso.

## 5. Validação

| Verificação | Resultado |
|---|---|
| `library/redis` (público, 1196 tags) | paginação (>1000) ok; 199 tags semver; maior `8.10.2`; token anônimo ok |
| `emporioautomacao/ararasuite-cp` sem credenciais | 35 versões, maior `0.0.40` (as versões 0.0.41–0.0.43 ainda não foram publicadas) |
| Repositório inexistente | mensagem "não encontrado ou sem permissão" |
| Tela no servidor real | versões, situação, JSON ao vivo, confirmação, recusa de downgrade, item na sidebar |
| Testes | `manage.py test registry` — todos OK (novo `test_atualizacoes.py`: semver, registry mockado, script de atualização, travas, estados do auxiliar, admin, permissões) |

## 6. Não validado / validar em produção

O `docker service update` real **não foi exercitado** (precisa de Swarm). Roteiro sugerido: publicar uma versão
trivial no Docker Hub; no painel, **Verificar agora** → *Atualização disponível*; **Atualizar**; acompanhar
`docker service ps <stack>_web` e o log do auxiliar; conferir *Concluída* e a nova versão; ajustar `CP_VERSION`
no Portainer.

## 7. Limites conhecidos

- Rollback automático (`--update-failure-action rollback`) cobre falha de inicialização do serviço, não
  migrations já aplicadas.
- Com `agente-no` em muitos nós, a atualização desse serviço roda em sequência pelo Swarm e pode demorar.
- Se o stack for renomeado sem os labels padrão do Swarm, a atualização é recusada com mensagem clara.
