import uuid
from django.db import models


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

    plano = models.ForeignKey(Plano, on_delete=models.PROTECT)
    modulos_ativos = models.ManyToManyField(Modulo, blank=True)

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
