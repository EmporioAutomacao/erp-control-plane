import ssl

from django.core.mail import EmailMessage, get_connection
from django.core.mail.backends.smtp import EmailBackend as _SmtpBackend


class _SmtpSemVerificacaoSSL(_SmtpBackend):
    """Backend SMTP que ignora erros de certificado SSL (hostname mismatch, self-signed, etc.)."""

    @property
    def ssl_context(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx


def obter_conexao_email():
    from .models import ConfiguracaoEmail
    config = ConfiguracaoEmail.obter()
    if not config:
        raise RuntimeError('Configuração de e-mail não cadastrada. Acesse Admin → Configurações → E-Mail.')

    params = dict(
        host=config.email_host,
        port=config.email_port,
        username=config.email_host_user,
        password=config.email_host_password,
        use_tls=config.email_use_tls,
    )

    if config.email_verificar_ssl:
        return get_connection(backend='django.core.mail.backends.smtp.EmailBackend', **params)

    return _SmtpSemVerificacaoSSL(**params)


def enviar_email(assunto, corpo, destinatarios, *, html=False):
    from .models import ConfiguracaoEmail
    config = ConfiguracaoEmail.obter()
    if not config:
        raise RuntimeError('Configuração de e-mail não cadastrada.')
    conexao = obter_conexao_email()
    msg = EmailMessage(
        subject=assunto,
        body=corpo,
        from_email=config.default_from_email,
        to=destinatarios if isinstance(destinatarios, list) else [destinatarios],
        connection=conexao,
    )
    if html:
        msg.content_subtype = 'html'
    msg.send()


def enviar_email_boas_vindas(cliente):
    from django.template.loader import render_to_string

    log = cliente.logs.filter(etapa='criar_superuser').order_by('-iniciado_em').first()
    senha = None
    if log and 'senha_temp:' in log.mensagem:
        senha = log.mensagem.split('senha_temp:')[-1].strip()

    corpo_html = render_to_string('registry/email_boas_vindas.html', {
        'cliente': cliente,
        'senha': senha,
    })

    assunto = f'Bem-vindo ao AraraSuite, {cliente.nome}!'
    enviar_email(assunto, corpo_html, cliente.email_contato, html=True)
