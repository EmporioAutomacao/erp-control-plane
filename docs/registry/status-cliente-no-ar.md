# Status do cliente e site no ar

O status salvo na tela do Cliente define se o site e o cron rodam. Antes, ele só era gravado e enviado ao ERP: um cliente "Suspenso" continuava no ar.

| De | Para | Efeito |
|---|---|---|
| Ativo, Trial, Atualizando | Suspenso, Cancelado, Trial Expirado | `{slug}_web` e `{slug}_cron` vão para 0 |
| Suspenso, Cancelado, Trial Expirado | Ativo, Trial | `{slug}_web` e `{slug}_cron` vão para 1 |
| Qualquer outra combinação | | Nada muda |

Banco, mídia e backups são mantidos. O visitante vê a página de `docs/registry/pagina-indisponivel.md`.

A tela pede confirmação antes de salvar uma mudança que tira o site do ar ou o religa, e mostra um aviso depois de salvar.

## Como funciona

1. `signals._cliente_entrou_ou_saiu_do_ar` compara o status anterior com o novo e dispara a task em `transaction.on_commit`.
2. `task_suspender_cliente` ou `task_reativar_cliente` confere o status atual e escala os serviços. Se o status já mudou de novo, não faz nada.
3. Em falha, a task retenta 3 vezes, com 60 s de intervalo. Durante uma migração de servidor, ela se reagenda a cada 5 min.

## Regras

- **As tasks não alteram o status.** Ele já foi gravado no admin e pode ser `cancelado` ou `trial`.
- **Status de provisionamento não mexem em réplicas.** Um cliente nunca implantado não tem serviço para escalar.
- **`.update()` não dispara o signal.** Só um `save()` do `Cliente` tem esse efeito.
- **As listas ficam em dois lugares.** `Cliente.STATUS_NO_AR` e `STATUS_FORA_DO_AR` estão espelhadas em `registry/static/registry/confirma_status_cliente.js`.

## Outras operações com o cliente fora do ar

- **Aplicar Configurações:** se precisar criar o cron, ele nasce em 0.
- **Atualizar versão:** troca a imagem sem esperar o web, e as migrations rodam ao religar.
- **Restauração:** devolve as réplicas de antes, então o cliente continua fora do ar.
- **Re-provisionar:** sobe o stack com 1 réplica e marca o cliente como Ativo. Ele volta ao ar de propósito.
- **Redeploy do stack do cliente pelo Portainer:** volta às réplicas do `stack.yml` sem mudar o status. Evite.

## Limitação

Se as retentativas falharem, a tela continua mostrando o status novo com o site ainda no ar, e o erro fica só no log do `celery`. Para conferir, veja as réplicas no Portainer.

## Clientes já suspensos

O signal só age na transição. Um cliente marcado como Suspenso antes desta versão continua como estiver. Para tirá-lo do ar, volte para Ativo, salve, mude para Suspenso e salve de novo, ou escale os serviços para 0 no Portainer.
