# Página de serviço indisponível

Quando um host não tem roteador no Traefik, o visitante via `404 page not found` em texto puro. Agora o `web` do CP responde com uma página própria, sem serviço novo no Swarm.

As páginas de erro geradas pelo ERP no ar ficam no próprio ERP: `docs/infra/paginas-de-erro.md`, no repositório `erp`.

## Como funciona

1. O roteador `cp-indisponivel` (labels do `web` em `stack-prod.yml`) casa com qualquer host, com prioridade 1. Os roteadores `Host(...)` têm prioridade maior e continuam atendendo normalmente.
2. O middleware `replacepath` do Traefik troca qualquer caminho por `/_indisponivel/`.
3. `registry/pagina_indisponivel.py`, primeiro item de `MIDDLEWARE`, responde 404 com `registry/pagina_indisponivel.html`.

## Regras

- **O `replacepath` é a trava de segurança.** Sem ele, qualquer domínio apontado para o Traefik chegaria às rotas do CP. Não remova essa label.
- **O middleware fica em primeiro lugar.** O host não está no `ALLOWED_HOSTS`, e o `CommonMiddleware` responderia 400.
- **Sem banco nem sessão.** A página precisa ser leve, porque robôs varrem domínios desconhecidos.
- **Texto genérico.** Ela atende todos os clientes e não sabe de qual deles é o host.
- **Visual duplicado.** O CSS é o mesmo de `core/templates/padrao/erros/base_erro.html` do ERP. Mudança no visual precisa ser repetida nos dois.

## Efeito no painel

O CP decide pelo código HTTP ao consultar um cliente. A resposta continua 404, agora com HTML, então health check, coleta de versões e migração se comportam como antes.

## Publicação

1. Publique a imagem do CP com o middleware.
2. Só então aplique as labels: faça o redeploy do `stack-prod.yml` no Portainer.

Na ordem inversa, hosts desconhecidos chegam a uma imagem sem o middleware e recebem 400.

## Como verificar

```sh
curl -si -H 'Host: dominio-desconhecido.com' http://<traefik>/admin/
```

A resposta esperada é 404 com a página, nunca o login do admin. Repita com um cliente suspenso, cenário ainda não testado, e confirme que um cliente ativo e o próprio CP abrem normalmente.

## Limitações

- Com o CP fora do ar, volta o erro padrão do Traefik.
- O `web` do CP tem 1 réplica. Um volume alto de hosts desconhecidos ocupa os workers do gunicorn. Se isso acontecer, some um `ratelimit` do Traefik ao roteador.

## Fora do escopo (não implementado)

- **502, 503 e 504 de um cliente:** exigem o middleware `errors` nas labels de cada `{slug}_web`, via `registry/provisioning.py` e "Aplicar Configurações". Limite o middleware a esses códigos: incluir 4xx ou 500 substituiria as respostas JSON da API, e o SyncAgent depende delas (por exemplo, `401 refresh_token_invalid`).
- **521, 522, 523 e similares:** são gerados pelo Cloudflare e dependem do plano de cada domínio.
