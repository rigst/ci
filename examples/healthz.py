"""Sonda de saúde para o smoke-test do CD (RUNBOOK.md, seção 7.8).

Copie para o app (ex.: core/healthz.py), ligue em urls.py como `healthz/` e
deixe a rota fora de qualquer middleware que exija login ou aceite de termos.

Responde 200 só quando o processo consegue usar o que precisa para servir uma
página: banco, cache e (se o app usa Celery) o broker. Qualquer falha vira
503, que o cd-deploy.sh trata como deploy quebrado e reverte.

O corpo é só "ok" ou "indisponivel": a rota é pública, e qual componente caiu
fica no log, não na resposta.

Não cobre worker do Celery parado — perguntar aos workers (`control.ping`)
custa um round-trip por chamada numa rota pública. Worker parado é assunto do
systemd (Restart=) e do Sentry.
"""

import logging
import uuid

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.http import HttpResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)


def _banco():
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")


def _cache():
    chave = f"healthz:{uuid.uuid4().hex}"
    cache.set(chave, "1", 10)
    if cache.get(chave) != "1":
        raise RuntimeError("cache não devolveu o valor gravado")
    cache.delete(chave)


def _broker():
    if not getattr(settings, "CELERY_BROKER_URL", ""):
        return
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        return
    from config.celery import app  # import tardio: app sem Celery não precisa dele

    with app.connection_for_write() as conexao:
        conexao.ensure_connection(max_retries=1, timeout=3)


VERIFICACOES = (("banco", _banco), ("cache", _cache), ("broker", _broker))


@never_cache
@require_GET
def healthz(request):
    falhas = []
    for nome, verificar in VERIFICACOES:
        try:
            verificar()
        except Exception:  # sonda: qualquer falha é "indisponível", e vai para o log
            logger.exception("healthz: %s indisponível", nome)
            falhas.append(nome)
    if falhas:
        return HttpResponse("indisponivel\n", status=503, content_type="text/plain")
    return HttpResponse("ok\n", content_type="text/plain")
