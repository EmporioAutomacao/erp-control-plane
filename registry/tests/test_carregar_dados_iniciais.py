from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase

from registry.models import Modulo, Plano


class CarregarDadosIniciaisTests(TestCase):
    def test_cria_planos_e_modulos_ausentes(self):
        call_command('carregar_dados_iniciais', stdout=None)
        self.assertTrue(Plano.objects.filter(pk='pro').exists())
        self.assertTrue(Modulo.objects.filter(pk='financeiro').exists())
        self.assertIn('financeiro', Plano.objects.get(pk='pro').modulos_inclusos.values_list('slug', flat=True))

    def test_nao_sobrescreve_edicoes_do_admin(self):
        call_command('carregar_dados_iniciais')
        Plano.objects.filter(pk='pro').update(preco_mensal=Decimal('199.90'), nome='Pro Editado')
        Plano.objects.get(pk='pro').modulos_inclusos.remove('marketing')

        call_command('carregar_dados_iniciais')

        plano = Plano.objects.get(pk='pro')
        self.assertEqual(plano.preco_mensal, Decimal('199.90'))
        self.assertEqual(plano.nome, 'Pro Editado')
        self.assertNotIn('marketing', plano.modulos_inclusos.values_list('slug', flat=True))

    def test_modulo_novo_entra_nos_planos_existentes_que_o_listam(self):
        call_command('carregar_dados_iniciais')
        Modulo.objects.filter(pk='rh').delete()  # simula um release que adiciona o modulo

        call_command('carregar_dados_iniciais')

        self.assertIn('rh', Plano.objects.get(pk='enterprise').modulos_inclusos.values_list('slug', flat=True))
        self.assertNotIn('rh', Plano.objects.get(pk='starter').modulos_inclusos.values_list('slug', flat=True))
