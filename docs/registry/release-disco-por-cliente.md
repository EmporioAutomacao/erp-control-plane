# Release: espaço em disco por cliente (coluna Disco)

- data: 2026-11-14
- responsavel: James Flavio
- status: implementado; suite `registry` verde
- versao CP: `0.0.62`
- app: `registry`

---

## 1. Problema
A lista de Clientes (`/admin/registry/cliente/`) não mostrava quanto cada cliente ocupa no
servidor. O único número de disco era o do host inteiro (Hosts de Infraestrutura → Disco), sem
saber qual cliente estava enchendo o nó.

## 2. O que mudou
- **Coluna Disco** na lista de Clientes (`ClienteAdmin.col_disco`), ordenável. Mostra o total; o
  tooltip traz o detalhe (Banco · Mídia · Backups do ERP) e a hora da medição. "—" = ainda não
  medido ou nenhum volume do cliente no nó do host.
- **O que é medido:** os volumes do stack no nó do host do cliente — `{slug}_pgdata`,
  `{slug}_media` e `{slug}_backups`. **Não** inclui os backups no bucket S3 nem a imagem Docker.
- **Como:** `volumes.tamanho_volumes_clientes()` roda `du -sk` num container `alpine` que monta
  `<DockerRootDir>/volumes` só leitura (o mesmo padrão do `disco_docker`, que mede o disco livre).
  `du` conta o espaço ocupado em disco (blocos), não a soma dos tamanhos dos arquivos.
- **Quando:** de carona no ping do host (`task_pingar_host`, a cada 5 min pela
  `task_verificar_hosts`), no máximo **a cada 30 min por host**
  (`hosts.medir_disco_clientes_agora`): o `du` percorre todos os arquivos. O manager decide e passa
  `medir_clientes` como 3º argumento; pings antigos na fila (2 argumentos) continuam funcionando.
- **Gravação:** `hosts.registrar_disco_clientes` grava em `Cliente.disco_bytes`, `disco_detalhe`
  (JSON por volume) e `disco_medido_em`, com `.update()`. Só recebem a medida os clientes cujo
  `host` é o host pingado: volumes residuais de uma migração no nó antigo não contam, e o volume do
  próprio CP (`cp_pgdata`) é ignorado. Cliente do host sem volume no nó fica com `disco_bytes=None`.
- **Migration `0036_cliente_disco`:** só adiciona os três campos (nulos), sem dados.
- Falha na medição só gera aviso no log do worker; o ping e o status do host seguem normais.

## 3. Verificação
- Unitários em `registry/tests/test_hosts.py::DiscoClientesTests`: leitura da saída do `du` (glob
  sem correspondência ignorado), gravação só nos clientes do host, intervalo de 30 min, ping com
  medição e a coluna da lista. Suite `registry` inteira verde.
- Docker real (dev): volumes `desenvolvimento_pgdata` (8 MB) e `desenvolvimento_media` (3 MB) →
  `task_pingar_host(..., True)` gravou 11.0 MB com o detalhe; a lista renderizou "11.0 MB" com o
  tooltip e a ordenação pela coluna respondeu 200. Volumes de teste removidos depois.

## 4. Deploy
Script `.claude/skills/cp-release-docker/scripts/release_docker.ps1` (imagem
`emporioautomacao/ararasuite-cp:0.0.62` e `:latest`). Atualizar pelo painel (Configurações >
Verificar Atualizações). **Tem migration** (`0036`, roda no boot do `web`). Ajustar `CP_VERSION`
no stack do Portainer depois.

## 5. Depois do deploy
- A coluna se preenche na primeira verificação dos hosts (até ~5 min). Para forçar: abrir o host
  em Hosts de Infraestrutura ou clicar em **Verificar agora**.
- Cliente que continua com "—": conferir se o `agente-no` roda no nó do host (status do host
  Online) e se o host do cliente é o nó onde estão os volumes.
- Conferir à mão no nó:
  `docker run --rm -v /var/lib/docker/volumes:/v:ro alpine du -sh /v/{slug}_pgdata/_data /v/{slug}_media/_data`
