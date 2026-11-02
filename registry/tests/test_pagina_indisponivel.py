from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from registry.pagina_indisponivel import ROTA


class PaginaIndisponivelTests(TestCase):
    def test_responde_404_com_a_pagina_para_host_desconhecido(self):
        resposta = self.client.get(ROTA, HTTP_HOST='dominio-desconhecido.com')
        self.assertContains(resposta, 'Serviço indisponível', status_code=404)
        self.assertContains(resposta, 'prefers-reduced-motion', status_code=404)
        self.assertEqual(resposta['Cache-Control'], 'no-store')

    def test_nao_consulta_o_banco(self):
        with CaptureQueriesContext(connection) as consultas:
            self.client.get(ROTA, HTTP_HOST='dominio-desconhecido.com')
        self.assertEqual(len(consultas), 0)

    def test_outros_caminhos_continuam_recusando_host_desconhecido(self):
        self.client.raise_request_exception = False
        for caminho in ('/admin/', '/', '/_indisponivel'):
            with self.subTest(caminho=caminho):
                resposta = self.client.get(caminho, HTTP_HOST='dominio-desconhecido.com')
                self.assertEqual(resposta.status_code, 400)
