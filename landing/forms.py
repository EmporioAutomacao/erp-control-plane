import re
from django import forms
from registry.models import Plano, Cliente


def _validar_cnpj(cnpj):
    cnpj = re.sub(r'\D', '', cnpj)
    if len(cnpj) != 14 or cnpj == cnpj[0] * 14:
        return False

    pesos1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    pesos2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]

    def digito(nums, pesos):
        total = sum(int(d) * p for d, p in zip(nums, pesos))
        r = total % 11
        return 0 if r < 2 else 11 - r

    return int(cnpj[12]) == digito(cnpj[:12], pesos1) and int(cnpj[13]) == digito(cnpj[:13], pesos2)


def _formatar_cnpj(cnpj):
    c = re.sub(r'\D', '', cnpj)
    return f'{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}'


class FormCadastro(forms.Form):
    nome = forms.CharField(
        max_length=200, label='Nome da empresa',
        widget=forms.TextInput(attrs={'placeholder': 'Ex: Minha Empresa Ltda', 'autocomplete': 'organization'}),
    )
    cnpj = forms.CharField(
        max_length=18, label='CNPJ',
        widget=forms.TextInput(attrs={'placeholder': '00.000.000/0000-00'}),
    )
    email = forms.EmailField(
        label='E-mail de contato',
        widget=forms.EmailInput(attrs={'placeholder': 'contato@suaempresa.com.br', 'autocomplete': 'email'}),
    )
    telefone = forms.CharField(
        max_length=20, required=False, label='Telefone (opcional)',
        widget=forms.TextInput(attrs={'placeholder': '(62) 99999-9999'}),
    )
    slug = forms.SlugField(
        max_length=30, label='Endereço de acesso',
        widget=forms.TextInput(attrs={'placeholder': 'minhaempresa'}),
        help_text='Somente letras minúsculas, números e hífens.',
    )
    plano = forms.ModelChoiceField(
        queryset=Plano.objects.filter(ativo=True).order_by('ordem'),
        label='Plano escolhido',
        empty_label=None,
    )

    def clean_cnpj(self):
        cnpj = self.cleaned_data['cnpj']
        limpo = re.sub(r'\D', '', cnpj)
        if not _validar_cnpj(limpo):
            raise forms.ValidationError('CNPJ inválido. Verifique e tente novamente.')
        formatado = _formatar_cnpj(limpo)
        if Cliente.objects.filter(cnpj=formatado).exists():
            raise forms.ValidationError('Este CNPJ já possui um cadastro.')
        return formatado

    def clean_slug(self):
        slug = self.cleaned_data['slug'].lower()
        if Cliente.objects.filter(slug=slug).exists():
            raise forms.ValidationError('Este endereço já está em uso. Escolha outro.')
        return slug
