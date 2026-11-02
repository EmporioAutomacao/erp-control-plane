# Trava anti-downgrade

No CP desde a versão 0.0.58. O ERP e o SyncAgent/PDV só ficam protegidos a partir da primeira versão publicada com a trava.

Nenhum caminho do sistema baixa a versão do CP, do ERP ou do SyncAgent/PDV. Quando não dá para comparar versões, a operação é recusada.

**Não existe desvio:** nenhuma variável, flag ou botão libera um downgrade. Para corrigir uma versão ruim, publique um número maior, mesmo que seja o código antigo com a versão nova.

## Barreiras

| Onde | O que faz |
|---|---|
| Boot da imagem (`manage.py guarda_versao`, ERP e CP) | Encerra o container se a imagem for mais antiga que a `versao_maxima` da tabela `guarda_versao`, ou se o banco tiver migration à frente do código. Pega redeploy no Portainer, `docker service update` e `docker service rollback`. |
| Painel (`registry/versoes.py`) | Tela do Cliente, Re-provisionar, `executar()` (inclusive migração de servidor), `atualizar_versao()` e cron recusam versão menor antes de gravar arquivos ou chamar o Docker. |
| Restauração | Backup de versão mais nova que a atual é recusado, sem parar o web. Backup mais antigo continua permitido. |
| Atualização do CP | `--update-failure-action pause`: o Swarm não volta o código depois das migrations. |
| SyncAgent/PDV | ERP, agente, instalador (wizard e `--auto`) e `self-update.ps1` recusam pacote mais antigo ou que não dá para comparar. |

## Detecção

Imagens publicadas antes da trava não têm a barreira de boot, e as tags continuam no Docker Hub. Um `docker service update` manual para uma delas ainda sobe. Nesse caso:

- a lista de clientes e a seção Versão Atual mostram **Versão regredida**;
- o log do CP registra `Versao do ERP regrediu`.

Um SyncAgent que reporta versão menor gera `Versao do SyncAgent regrediu` no log do ERP, sem bloquear a sincronização da loja.

## Regras de comparação

**ERP no painel:** a referência é a maior entre `Cliente.versao_erp_maxima` (só sobe), `versao_erp_detectada` e a tag em execução no Swarm.

- `Cliente.versao_erp` fica de fora da referência porque é só o alvo pedido: um número digitado errado, nunca implantado, precisa poder ser corrigido.
- `latest` ou alvo vazio em cliente com versão conhecida é fixado nessa versão.
- `latest` só é aceito na primeira implantação, quando o banco é novo. É o caso de `ERP_LATEST_VERSION=latest`.
- Se o Swarm não responder, a operação é recusada. Só "serviço não existe" é tratado como cliente novo.

**Migrations no boot:** só bloqueia migration aplicada com número **maior** que o último em disco, em app que ainda tem migrations numeradas. Migration apagada do repositório não é sinal de downgrade: bancos antigos ainda guardam as `0002`–`0009` do `mercadolivre`, que saíram do código.

**Instalador do PDV:** a versão instalada é o maior número entre o arquivo `VERSION` e o `ProductVersion` do `SyncAgent.exe`, porque qualquer um dos dois pode atrasar. O mesmo commit continua permitindo reparo.

**O que continua permitido:** implantar a mesma versão (re-provisionar, migração de servidor, reparo do PDV) e o rollback do `self-update.ps1` quando uma atualização não chega a subir.

## Quando a trava dispara

O container sai com `[guarda_versao] DOWNGRADE RECUSADO` e o Swarm tenta de novo em loop. O cliente fica fora do ar até subir uma imagem igual ou maior que a informada na mensagem.

O caso esperado é uma versão nova com defeito depois de começar a migrar: como `registrar` roda antes do `migrate`, voltar não é possível. Publique uma correção com número maior.

Restaurar um backup de versão desconhecida e mais nova também cai aqui. Atualize o cliente para a versão do backup ou maior.

## Limites

- A trava só protege a partir da primeira versão publicada com ela: cada cliente precisa ser atualizado para o ERP com a trava, e cada agente para o PDV com a trava.
- Quem tem root no servidor ou no PC da loja pode contornar qualquer trava.
- O `self-update.ps1` que roda é o do pacote: um pacote antigo traz o script antigo. A barreira nesse caso é o instalador e o agente já instalados, antes de chegar ao script.
