import uuid
from django.core.validators import RegexValidator
from django.db import models

sha256_hex_validator = RegexValidator(
    regex=r"^[0-9A-Fa-f]{64}$",
    message="Deve ser o hash SHA256 em hexadecimal (64 caracteres) — o conteudo do arquivo "
    ".sha256, nao a URL dele.",
)


class Modulo(models.Model):
    slug = models.SlugField(primary_key=True)
    nome = models.CharField(max_length=100)
    descricao = models.TextField(blank=True)
    preco_mensal = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    ativo = models.BooleanField(default=True)

    class Meta:
        verbose_name = 'Módulo'
        verbose_name_plural = 'Módulos'
        ordering = ['nome']

    def __str__(self):
        return self.nome


class Plano(models.Model):
    slug = models.SlugField(primary_key=True)
    nome = models.CharField(max_length=100)
    descricao = models.TextField(blank=True)
    preco_mensal = models.DecimalField(max_digits=8, decimal_places=2)
    max_usuarios = models.PositiveIntegerField(default=0, help_text='0 = ilimitado')
    max_empresas = models.PositiveIntegerField(default=1)
    recursos_cpu = models.PositiveIntegerField(default=1)
    recursos_ram_gb = models.PositiveIntegerField(default=2)
    modulos_inclusos = models.ManyToManyField(Modulo, blank=True)
    ativo = models.BooleanField(default=True)
    destaque = models.BooleanField(default=False)
    ordem = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = 'Plano'
        verbose_name_plural = 'Planos'
        ordering = ['ordem']

    def __str__(self):
        return self.nome


class HostInfraestrutura(models.Model):
    TIPO_CHOICES = [('vps', 'VPS'), ('dedicado', 'Dedicado'), ('cloud_vm', 'Cloud VM')]
    REGIAO_CHOICES = [('anapolis', 'Anápolis'), ('brasilia', 'Brasília')]

    nome = models.CharField(max_length=100, unique=True)
    swarm_node_id = models.CharField(max_length=100, blank=True)
    ip = models.GenericIPAddressField()
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES, default='dedicado')
    regiao = models.CharField(max_length=20, choices=REGIAO_CHOICES, default='anapolis')
    cpu_total = models.PositiveIntegerField()
    ram_gb_total = models.PositiveIntegerField()
    ativo = models.BooleanField(default=True)
    observacoes = models.TextField(blank=True)

    class Meta:
        verbose_name = 'Host de Infraestrutura'
        verbose_name_plural = 'Hosts de Infraestrutura'
        ordering = ['regiao', 'nome']

    def __str__(self):
        return f'{self.nome} ({self.regiao})'


class Cliente(models.Model):
    STATUS_CHOICES = [
        ('aguardando_provisao', 'Aguardando Provisionamento'),
        ('provisionando', 'Provisionando'),
        ('ativo', 'Ativo'),
        ('trial', 'Trial'),
        ('trial_expirado', 'Trial Expirado'),
        ('suspenso', 'Suspenso'),
        ('cancelado', 'Cancelado'),
        ('erro_provisao', 'Erro no Provisionamento'),
        ('atualizando', 'Atualizando'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(unique=True)
    nome = models.CharField(max_length=200)
    cnpj = models.CharField(max_length=18, unique=True)
    email_contato = models.EmailField()
    telefone = models.CharField(max_length=20, blank=True)

    host = models.ForeignKey(HostInfraestrutura, null=True, blank=True, on_delete=models.SET_NULL)
    versao_erp = models.CharField(max_length=20, blank=True)
    stack_path = models.CharField(max_length=500, blank=True)
    subdominio = models.CharField(max_length=100, unique=True)
    dominio_custom = models.CharField(max_length=200, blank=True)

    TEMA_CHOICES = [
        ('padrao', 'Padrão (e-commerce)'),
        ('dedetizadora', 'Dedetizadora (verde)'),
        ('dedetizadora2', 'Dedetizadora 2 (azul + animações)'),
        ('loja', 'Loja (claro premium)'),
    ]

    plano = models.ForeignKey(Plano, on_delete=models.PROTECT)
    modulos_ativos = models.ManyToManyField(Modulo, blank=True)
    tema_site = models.CharField(
        max_length=50,
        choices=TEMA_CHOICES,
        default='padrao',
        verbose_name='Tema do site',
        help_text='Template visual da landing page pública do cliente.',
    )

    asaas_customer_id = models.CharField(max_length=100, blank=True)
    asaas_subscription_id = models.CharField(max_length=100, blank=True)
    isento_cobranca = models.BooleanField(
        default=False,
        verbose_name='Isento de cobrança',
        help_text='Marque para clientes de teste ou parceiros — nenhuma cobrança será gerada.',
    )
    motivo_isencao = models.CharField(
        max_length=200, blank=True,
        verbose_name='Motivo da isenção',
    )

    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='aguardando_provisao', db_index=True)

    versoes_permitidas = models.ManyToManyField(
        'VersaoAgente', blank=True, related_name='clientes',
        verbose_name='Versões do SyncAgent/PDV permitidas',
        help_text='Conjunto que este cliente pode escolher para atualizar (curadoria da equipe). '
        'O dono da loja escolhe dentro deste conjunto na tela "Atualizar App" do Tray; nunca '
        'consegue fazer downgrade, mesmo que uma versão menor esteja marcada aqui (trava do '
        'lado erp/SyncAgent). Versões que exigem um ERP mais novo que "Versão ERP" abaixo '
        'chegam desabilitadas no Tray, com o motivo.',
    )
    integracao_secret = models.CharField(
        max_length=100, blank=True,
        verbose_name='Segredo de integração CP → ERP',
        help_text='Gerado no provisionamento. Usado como Bearer token nas chamadas HTTPS do CP '
        'para esta instância (ex.: sincronizar versões permitidas). Rotacione se suspeitar de '
        'vazamento — reprovisionar/reaplicar módulos gera um novo.',
    )

    trial_ate = models.DateField(null=True, blank=True)
    data_ativacao = models.DateField(null=True, blank=True)
    data_suspensao = models.DateField(null=True, blank=True)
    data_cancelamento = models.DateField(null=True, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)
    observacoes = models.TextField(blank=True)

    class Meta:
        verbose_name = 'Cliente'
        verbose_name_plural = 'Clientes'
        ordering = ['-criado_em']
        db_table = 'registry_cliente'

    def __str__(self):
        return f'{self.nome} ({self.slug})'

    @property
    def url(self):
        if self.dominio_custom:
            return f'https://{self.dominio_custom}'
        return f'https://{self.subdominio}'


class ProvisionamentoLog(models.Model):
    STATUS_CHOICES = [
        ('pendente', 'Pendente'),
        ('executando', 'Executando'),
        ('concluido', 'Concluído'),
        ('erro', 'Erro'),
    ]

    cliente = models.ForeignKey(Cliente, on_delete=models.CASCADE, related_name='logs')
    etapa = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pendente')
    mensagem = models.TextField(blank=True)
    iniciado_em = models.DateTimeField(auto_now_add=True)
    concluido_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Log de Provisionamento'
        verbose_name_plural = 'Logs de Provisionamento'
        ordering = ['iniciado_em']

    def __str__(self):
        return f'{self.cliente.slug} — {self.etapa} ({self.status})'


class AtualizacaoVersao(models.Model):
    STATUS_CHOICES = [
        ('agendada', 'Agendada'),
        ('executando', 'Executando'),
        ('concluida', 'Concluída'),
        ('revertida', 'Revertida'),
        ('erro', 'Erro'),
    ]

    cliente = models.ForeignKey(Cliente, on_delete=models.CASCADE, related_name='atualizacoes')
    versao_anterior = models.CharField(max_length=20)
    versao_nova = models.CharField(max_length=20)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='agendada')
    iniciada_em = models.DateTimeField(auto_now_add=True)
    concluida_em = models.DateTimeField(null=True, blank=True)
    iniciada_por = models.ForeignKey('auth.User', null=True, on_delete=models.SET_NULL)
    mensagem = models.TextField(blank=True)

    class Meta:
        verbose_name = 'Atualização de Versão'
        verbose_name_plural = 'Atualizações de Versão'
        ordering = ['-iniciada_em']

    def __str__(self):
        return f'{self.cliente.slug}: {self.versao_anterior} → {self.versao_nova}'


class VersaoAgente(models.Model):
    """Catálogo mestre de versões do SyncAgent/PDV Local (pdv-local) — global,
    não por cliente: download_url/sha256 apontam pro mesmo artefato do GitHub
    Releases pra todo mundo. Por cliente só varia qual subconjunto está
    permitido (ver Cliente.versoes_permitidas)."""

    versao = models.CharField(max_length=20, unique=True)
    erp_minimo = models.CharField(
        max_length=20, blank=True,
        verbose_name='Versão mínima do ERP exigida',
        help_text='Comparação semver X.Y.Z (mesmo formato de Cliente.versao_erp). Em branco = '
        'sem requisito de versão do ERP.',
    )
    download_url = models.URLField(max_length=500)
    sha256 = models.CharField(
        max_length=64,
        validators=[sha256_hex_validator],
        help_text='Hash SHA256 do ZIP em hexadecimal (64 caracteres) — abra o arquivo .sha256 '
        'publicado no Release e cole o conteúdo dele aqui, não a URL do arquivo.',
    )
    release_notes = models.TextField(blank=True)
    ativo = models.BooleanField(
        default=True,
        help_text='Desmarque para aposentar uma versão do catálogo (ex.: recall por bug) sem '
        'apagar histórico. Uma versão inativa nunca é enviada num push novo, mesmo que ainda '
        'esteja na curadoria de algum cliente.',
    )
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Versão do Agente (SyncAgent/PDV)'
        verbose_name_plural = 'Catálogo de Versões do Agente'
        ordering = ['-criado_em']

    def __str__(self):
        return self.versao


class SincronizacaoVersoesAgente(models.Model):
    """Log de cada push do CP pro erp de um cliente com as versões
    atualmente permitidas — mesmo padrão de auditoria de AtualizacaoVersao."""

    STATUS_CHOICES = [
        ('enviando', 'Enviando'),
        ('concluida', 'Concluída'),
        ('erro', 'Erro'),
    ]

    cliente = models.ForeignKey(Cliente, on_delete=models.CASCADE, related_name='sincronizacoes_versoes')
    versoes_enviadas = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='enviando')
    iniciada_em = models.DateTimeField(auto_now_add=True)
    concluida_em = models.DateTimeField(null=True, blank=True)
    resposta_http_status = models.PositiveIntegerField(null=True, blank=True)
    mensagem_erro = models.TextField(blank=True)

    class Meta:
        verbose_name = 'Sincronização de Versões (CP → ERP)'
        verbose_name_plural = 'Sincronizações de Versões (CP → ERP)'
        ordering = ['-iniciada_em']

    def __str__(self):
        return f'{self.cliente.slug} — {self.status} em {self.iniciada_em:%d/%m/%Y %H:%M}'


class ConfiguracaoEmail(models.Model):
    email_host = models.CharField(max_length=200, verbose_name='Servidor SMTP')
    email_port = models.PositiveIntegerField(default=587, verbose_name='Porta')
    email_use_tls = models.BooleanField(default=True, verbose_name='Usar TLS')
    email_verificar_ssl = models.BooleanField(default=True, verbose_name='Verificar SSL', help_text='Desative se o certificado do servidor não corresponder ao hostname.')
    email_host_user = models.CharField(max_length=200, verbose_name='Usuário SMTP')
    email_host_password = models.CharField(max_length=200, verbose_name='Senha SMTP')
    default_from_email = models.EmailField(verbose_name='E-mail remetente padrão')
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Configuração de E-mail'
        verbose_name_plural = 'Configuração de E-mail'

    def __str__(self):
        return f'{self.email_host_user} via {self.email_host}:{self.email_port}'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def obter(cls):
        return cls.objects.filter(pk=1).first()


class ConfiguracaoCloudflare(models.Model):
    cf_api_token = models.CharField(max_length=500, verbose_name='API Token', help_text='Token com permissões Zone:Read + DNS:Edit + Account:Cloudflare Tunnel:Edit em All Accounts.')
    tunnel_name = models.CharField(max_length=200, blank=True, verbose_name='Nome do Tunnel', help_text='Nome do Cloudflare Tunnel (deixe vazio para usar o primeiro tunnel ativo encontrado).')
    server_ip = models.GenericIPAddressField(null=True, blank=True, verbose_name='IP do Servidor (legado)', help_text='Não utilizado — mantido apenas para referência.')
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Configuração Cloudflare'
        verbose_name_plural = 'Configuração Cloudflare'

    def __str__(self):
        return f'Cloudflare — {self.server_ip}'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def obter(cls):
        return cls.objects.filter(pk=1).first()


class BackupCliente(models.Model):
    STATUS_CHOICES = [
        ('em_andamento', 'Em andamento'),
        ('concluido', 'Concluído'),
        ('erro', 'Erro'),
    ]
    ORIGEM_CHOICES = [
        ('automatico', 'Automático'),
        ('manual', 'Importado manualmente'),
    ]
    cliente = models.ForeignKey(Cliente, on_delete=models.CASCADE, related_name='backups')
    criado_em = models.DateTimeField(auto_now_add=True)
    arquivo_path = models.CharField(max_length=500, blank=True)
    tamanho_bytes = models.BigIntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='em_andamento')
    progresso = models.PositiveSmallIntegerField(default=0)
    restaurando = models.BooleanField(default=False)
    mensagem = models.TextField(blank=True)
    origem = models.CharField(max_length=20, choices=ORIGEM_CHOICES, default='automatico')
    mensagem_restauracao = models.TextField(blank=True)

    class Meta:
        verbose_name = 'Backup'
        verbose_name_plural = 'Backups'
        ordering = ['-criado_em']

    def __str__(self):
        return f'Backup {self.cliente.slug} {self.criado_em:%d/%m/%Y %H:%M}'

    @property
    def tamanho_fmt(self):
        if self.tamanho_bytes < 1024 * 1024:
            return f'{self.tamanho_bytes / 1024:.1f} KB'
        return f'{self.tamanho_bytes / 1024 / 1024:.1f} MB'


class VerificacaoSaude(models.Model):
    cliente = models.ForeignKey(Cliente, on_delete=models.CASCADE, related_name='verificacoes_saude')
    verificado_em = models.DateTimeField(auto_now_add=True)
    status_http = models.PositiveIntegerField(default=0)
    latencia_ms = models.PositiveIntegerField(null=True, blank=True)
    online = models.BooleanField(default=False)

    class Meta:
        verbose_name = 'Verificação de Saúde'
        verbose_name_plural = 'Verificações de Saúde'
        ordering = ['-verificado_em']
        get_latest_by = 'verificado_em'
        db_table = 'registry_verificacao_saude'

    def __str__(self):
        return f'{self.cliente.slug} — {"OK" if self.online else "FALHA"} em {self.verificado_em:%d/%m %H:%M}'
