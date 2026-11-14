# Domínio próprio no ERP com o e-mail em servidor externo

Guia de operação: como fazer `loja.com.br` (e `www.loja.com.br`) abrir o ERP de um cliente
sem tirar o e-mail do provedor atual (cPanel, Google, Microsoft etc.). A parte de código
(modelo, labels, reconciliação) está em [dominios-multiplos.md](dominios-multiplos.md); aqui
ficam o fluxo de ponta a ponta, o que é manual e as armadilhas que já apareceram.

Caso de referência: cliente `emporioautomacao`, 14/11/2026 (seção 6).

## 1. Fluxo

```
Usuário  https://loja.com.br
  │
  ▼
Registrador (Registro.br)  ── nameservers ──▶  Cloudflare (zona loja.com.br, plano Free)
                                                 │  CNAME @ e www → <tunnel-id>.cfargotunnel.com (proxied)
                                                 │  mail, webmail, MX, SPF, DKIM... → servidor de e-mail (somente DNS)
                                                 ▼
                                        Cloudflare Tunnel "ararasuite.com.br"
                                                 │  ingress: hostname → http://traefik_traefik:80
                                                 ▼
                                        Traefik (roteia pelo Host)
                                                 │  routers {slug}-erp-custom-* (labels do {slug}_web)
                                                 ▼
                                        Stack do cliente ({slug}_web, no nó da região do host)
                                                 │  ALLOWED_HOSTS / CSRF_TRUSTED_ORIGINS
                                                 ▼
                                        Django do ERP
```

Quatro camadas precisam conhecer o hostname: **DNS** (CNAME), **ingress do túnel**, **Traefik**
e **Django**. Faltou uma, o domínio não abre.

O subdomínio `<slug>.ararasuite.com.br` não depende de nada disso: é coberto pelo wildcard
`*.ararasuite.com.br` do túnel e pelo router `{slug}-erp`.

## 2. O que o CP automatiza e o que é manual

| Etapa | Quem faz | Onde no código |
|---|---|---|
| Cadastrar domínio, modo, principal, www | Manual, aba **Domínios** do Cliente | `DominioCliente` (`registry/models.py`), inline no `ClienteAdmin` |
| `ALLOWED_HOSTS` / `CSRF_TRUSTED_ORIGINS` do ERP | **CP**, no ⚙ Aplicar Configurações | `dominios.allowed_hosts`/`csrf_origins` → `docker service update --env-add` em `_view_aplicar_modulos` (`registry/admin.py`); o ERP lê em `core/settings.py` |
| Routers e redirects do Traefik | **CP**, no ⚙ Aplicar Configurações | `dominios.labels_traefik` + `dominios.args_reconciliar` (remove os que saíram) |
| CNAME proxied → túnel (raiz e `www.`) | **CP**, depois do Aplicar com sucesso ou no ☁ Configurar Cloudflare | `cloudflare.publicar_cname` — também **apaga A/AAAA** no mesmo nome e **sobrescreve** um CNAME que aponte para outro lugar |
| Rota (ingress) no túnel | **CP**, idem | `cloudflare.novo_ingress` — hostnames do cliente vão para o topo, catch-all por último |
| Criar a zona | **CP**, só se não achar nenhuma — **sem importar registros** (`jump_start: False`) | `cloudflare.sincronizar` → `Api.criar_zona` |
| Importar DNS antigo para a zona (e-mail!) | **Manual**, no painel do Cloudflare | — |
| Conferir DKIM, tirar proxy do e-mail | **Manual**, no painel do Cloudflare | — |
| Trocar nameservers | **Manual**, no registrador | — (registradores não têm API padrão) |
| Limpar Cloudflare ao destruir cliente | **Não é feito**: remova os domínios e aplique antes de destruir | — |

O token fica em **Configurações → Cloudflare** (`ConfiguracaoCloudflare`, singleton). O nome do
túnel é opcional (vazio = primeiro túnel ativo da conta).

## 3. Passo a passo

A ordem importa: a zona com os registros de e-mail tem de existir **antes** do Aplicar
Configurações (ver armadilha 1).

1. **Levantar o DNS atual** no DNS que hoje responde (antes de qualquer mudança): NS, A/AAAA da
   raiz, CNAME/A do `www`, MX, SPF, DKIM (`<seletor>._domainkey`, normalmente `default`),
   DMARC (`_dmarc`), SRV e subdomínios do servidor (`mail`, `webmail`, `cpanel`, `ftp`,
   `autodiscover`...). Confirmar quem é o provedor real de e-mail pelo MX, SPF e PTR do IP do
   `mail` (armadilha 8).
2. **Criar a zona no Cloudflare** (Add a site, plano Free) com a importação automática ligada.
3. **Revisar a zona importada:**
   - acrescentar à mão o que não veio, a começar pelo **DKIM**;
   - todos os registros de e-mail/servidor em **Somente DNS** (nuvem cinza);
   - os A/CNAME antigos da raiz e do `www` podem ficar: o CP os troca no passo 5. Se for criar a
     rota do túnel pelo painel, apague-os antes (o painel recusa hostname com registro conflitante).
4. **Cadastrar no CP:** Cliente → aba **Domínios** → domínio sem `https://`, modo **Abre o ERP**,
   marcar **Principal** se ele passa a ser o endereço oficial, **Incluir www** ligado. Salvar.
   Salvar não publica nada.
5. **⚙ Aplicar Configurações.** Esperado: "Configurações aplicadas" + uma mensagem por domínio
   "Cloudflare: loja.com.br ok/criado/atualizado, www.loja.com.br ... → tunnel ...". Se o
   Cloudflare falhar, o resto já foi aplicado: corrija e use **☁ Configurar Cloudflare**, que
   refaz só a parte do Cloudflare.
6. **Trocar os nameservers no registrador** para os dois que o Cloudflare mostrou na zona. Com
   DNSSEC ligado no registrador, desligar antes (ou configurar o DS do Cloudflare).
7. **Depois que a zona ficar Ativa** (minutos a algumas horas):
   - `https://loja.com.br` abre o ERP e `https://www.loja.com.br` redireciona para ele;
   - certificado válido nos dois;
   - enviar e receber e-mail, abrir o webmail, conferir DKIM/SPF num e-mail recebido
     (cabeçalho `Authentication-Results`).

### Checklist

- [ ] DNS atual levantado (NS, A, CNAME www, MX, SPF, DKIM, DMARC, SRV, subdomínios de e-mail)
- [ ] Provedor real do e-mail confirmado (MX/PTR/SPF)
- [ ] Zona criada no Cloudflare **com importação**; DKIM conferido/acrescentado
- [ ] Registros de e-mail em "Somente DNS"
- [ ] Domínio cadastrado na aba Domínios e **⚙ Aplicar Configurações** sem erro do Cloudflare
- [ ] Nameservers trocados no registrador (DNSSEC desativado)
- [ ] Após ativação: ERP na raiz e no www, certificado, envio/recebimento, webmail

## 4. Preservar o e-mail

O e-mail só depende de registros que o CP **nunca toca**: o CP só mexe nos hostnames
cadastrados (a raiz e o `www.`). Tudo o que importa é a zona nova ter os mesmos registros de
e-mail que o DNS antigo no momento da troca de nameservers:

- `MX` → servidor de e-mail (ex.: `mail.loja.com.br`);
- `A` de `mail`, `webmail`, `cpanel`, `autodiscover`... → IP do servidor, **Somente DNS**;
- `TXT` SPF na raiz, DKIM, DMARC;
- `SRV` de autodiscover/caldav/carddav, se existiam.

**SPF:** se o SPF tiver o mecanismo `a`, ele passa a valer para os IPs do Cloudflare (a raiz
agora é proxied), o que não autoriza o servidor de e-mail. Quem garante a entrega é o `mx` ou o
`include:` do provedor; conferir que um deles cobre o servidor que envia.

Acessos que passavam pela raiz (`loja.com.br/webmail`, `/cpanel`, `:2083`, `:2096`) deixam de
funcionar, porque a raiz agora é o ERP. Usar os subdomínios (`webmail.loja.com.br`,
`cpanel.loja.com.br`) ou o hostname do servidor do provedor.

## 5. Armadilhas conhecidas

1. **Aplicar Configurações antes de criar a zona apaga o e-mail na troca de nameservers.** Sem
   zona na conta, o CP cria uma vazia (`jump_start: False`, sem importação), com só os CNAMEs do
   ERP. A mensagem pede para trocar os nameservers; quem trocar nessa hora fica sem MX. Criar
   a zona com importação primeiro (passo 2). Se já aconteceu: importar/acrescentar os registros
   na zona **antes** de trocar os nameservers.
2. **Registros de e-mail nunca com proxy** (nuvem laranja). SMTP/IMAP/POP não passam pelo proxy
   do Cloudflare. Observado em 14/11/2026: `mail.emporiodistribuidora.com.br` está **com proxy** —
   provavelmente a corrigir (ver seção 7).
3. **DKIM não vem na importação automática** do Cloudflare. Conferir `default._domainkey` e
   outros seletores à mão.
4. **O editor de zona do cPanel/WHM deixa de valer** depois da troca de nameservers. Toda
   alteração de DNS (MX, SPF, DKIM, subdomínio novo) passa a ser feita no Cloudflare.
5. **AutoSSL do cPanel:** a validação da raiz falha (aponta para o ERP); `mail`, `webmail` e
   `cpanel` continuam validando porque apontam direto para o servidor.
6. **Rota do túnel pelo painel exige hostname sem A/CNAME conflitante.** Pelo CP não: o
   `publicar_cname` apaga A/AAAA e sobrescreve o CNAME do mesmo nome. Fora a raiz e o `www.`,
   nada é apagado.
7. **DNS e túnel certos sem o domínio no CP (ou sem Aplicar)** → o Traefik não tem router para o
   host e cai no catch-all `cp-indisponivel`: página "serviço indisponível" com HTTP 404
   ([pagina-indisponivel.md](pagina-indisponivel.md)). Domínio no Traefik mas fora do
   `ALLOWED_HOSTS` (env antigo) → 400 do Django; Aplicar Configurações corrige os dois.
8. **Origem do e-mail:** confirmar pelo DNS (MX, PTR do IP do `mail`, SPF) antes de mexer. No
   caso `emporioautomacao` o cliente achava que o e-mail estava na KingHost; o DNS mostrou cPanel
   da HostGator (PTR `unifiedlayer.com`, SPF `include:websitewelcome.com`).
9. **Ordem das rotas do túnel:** a primeira que casa vence. O CP põe os hostnames do cliente no
   topo a cada sincronização, acima dos curingas. Rota criada à mão no painel fica onde foi posta
   até o próximo Aplicar/Configurar Cloudflare daquele cliente.
10. **`www` em modo Abre o ERP não serve o ERP: redireciona** (301, 308 em POST) para o domínio
    sem `www`. É esperado.
11. **Redireciona para o principal** não serve para domínio onde um SyncAgent/PDV foi ativado:
    o .NET descarta o `Authorization` ao seguir o redirect (401).
12. **Certificado universal do Cloudflare** cobre `zona` e `*.zona`. Com subdomínio cadastrado
    (`erp.loja.com.br`), o `www.erp.loja.com.br` dá erro de certificado: desligar Incluir www.

## 6. Caso real: `emporioautomacao` (14/11/2026)

**Objetivo:** `emporioautomacao.com.br` e `www` abrem o ERP; o e-mail continua no servidor
cPanel atual (HostGator).

1. **Zona no Cloudflare:** `emporioautomacao.com.br` adicionada (Free) com importação automática.
   - Removidos: A da raiz (`170.82.173.10`, `170.82.174.10`) e CNAME `www` → GoCache (site antigo).
   - Mantidos em **Somente DNS**, todos → `216.172.173.241`: `mail`, `webmail`, `cpanel`, `whm`,
     `webdisk`, `ftp`, `autoconfig`, `autodiscover`, `cpcalendars`, `cpcontacts` (+ `ns1`/`ns2`,
     sem uso).
   - `MX` → `mail.emporioautomacao.com.br` (prioridade 0); SPF `v=spf1 a mx include:websitewelcome.com ~all`;
     `_dmarc` e SRV (`_autodiscover._tcp`, `_caldav(s)._tcp`, `_carddav(s)._tcp`) importados.
   - DKIM `default._domainkey` **não veio na importação**: copiado à mão do DNS antigo.
2. **Túnel:** rotas "Aplicativo publicado" criadas pelo painel no túnel `ararasuite.com.br` para
   `emporioautomacao.com.br` e `www.emporioautomacao.com.br` → `http://traefik_traefik:80`; o
   Cloudflare criou os CNAMEs para `<tunnel-id>.cfargotunnel.com`. (Passo redundante: o Aplicar do
   passo 3 faz o mesmo. Não causa problema.)
3. **CP:** cliente `emporioautomacao`, aba Domínios:
   - `emporiodistribuidora.com.br` — Abre o ERP, **Principal**, com www;
   - `emporioautomacao.com.br` — Abre o ERP, não principal, com www (novo).

   ⚙ Aplicar Configurações → "Configurações aplicadas". Como o principal continua sendo
   `emporiodistribuidora.com.br`, é ele que o health check, o `cp_pull`/`cp_push` e o e-mail
   usam (`Cliente.host_publico`); `SITE_URL` continua o subdomínio.
4. **Registro.br:** nameservers de `ns1/ns2.emporioautomacao.com.br` (glue `108.167.188.175/176`)
   para `lennox.ns.cloudflare.com` e `treasure.ns.cloudflare.com`. DNSSEC estava desativado.
5. **Pendente:** depois de a zona ficar Ativa, testar `https://emporioautomacao.com.br`,
   `https://www.emporioautomacao.com.br`, envio/recebimento de e-mail e webmail.

**Acessos de e-mail depois da mudança:**

| Uso | Endereço |
|---|---|
| Webmail | `https://webmail.emporioautomacao.com.br` |
| cPanel (contas de e-mail) | `https://cpanel.emporioautomacao.com.br` ou `https://srv136.prodns.com.br:2083` |
| WHM | `https://whm.emporioautomacao.com.br` |
| IMAP/SMTP nos clientes de e-mail | `mail.emporioautomacao.com.br` (sem mudança) |

Não usar mais `emporioautomacao.com.br/webmail`, `/cpanel`, `:2083`, `:2096`.

## 7. Divergências e pontos a confirmar

Encontrados ao comparar o caso acima com o código (14/11/2026):

- **Rotas do túnel e CNAMEs são automáticos.** O relato tratava a criação das rotas como manual;
  o `cloudflare.sincronizar` (chamado pelo Aplicar Configurações) cria rota e CNAME, apaga A/AAAA
  e substitui o CNAME antigo do `www`. Manual de verdade é só a zona com importação, a revisão
  dos registros de e-mail e os nameservers.
- **Serviço das rotas do curinga.** Segundo o relato, `*.ararasuite.com.br` e `ararasuite.com.br`
  apontam para `http://traefik:80`, e as demais para `http://traefik_traefik:80` (o valor que o CP
  grava, `SERVICO_TRAEFIK` em `registry/cloudflare.py`). Os dois resolvem para o Traefik se o
  `cloudflared` estiver na mesma rede overlay, mas convém confirmar e padronizar.
- **Posição das rotas no túnel.** O relato lista as rotas do `emporioautomacao` no fim; depois
  de um Aplicar com o Cloudflare configurado elas passam para o topo. Se continuarem no fim, o
  Cloudflare do Aplicar não rodou ou falhou: conferir as mensagens e usar ☁ Configurar Cloudflare.
- **`mail.emporiodistribuidora.com.br` com proxy.** Não é feito pelo CP (ele só publica a raiz e
  o `www.`). Conferir se o e-mail desse domínio funciona e, se usar SMTP/IMAP direto, passar para
  Somente DNS.
- **Permissões do token.** O texto de ajuda de `ConfiguracaoCloudflare.cf_api_token` cita Zone:Read
  + DNS:Edit + Tunnel:Edit; a mensagem de erro de `Api.conectar` acrescenta **Zone:Edit**, que é
  necessária para o CP criar zona. Sem ela, a criação automática falha (o que, pela armadilha 1,
  não é ruim).
- **Rótulo "Redireciona (301)".** Para GET é 301; POST/PUT recebem 308.
