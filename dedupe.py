"""
========================================================================
 DEDUPE.PY — PROTEÇÃO CONTRA INTERAÇÃO/MENSAGEM DUPLICADA
========================================================================
Bug real observado: o Discord (app mobile, reconexão de gateway, ou até
duas instâncias do bot rodando com o mesmo token — ver comentários em
bot.py sobre esse cenário) às vezes entrega o MESMO evento (interaction
OU message) mais de uma vez pro bot. Quando o handler faz algo com efeito
colateral (dar coin, postar um embed, cobrar um valor), isso duplica a
ação de verdade.

Isso é DIFERENTE do que responder_seguro() (em licenca.py) resolve —
aquilo só evita a MENSAGEM DE ERRO quando a resposta falha, mas não
impede o handler de rodar (e agir) duas vezes.

Uso: logo no início do handler, ANTES de qualquer efeito colateral
(escrita no banco, envio de mensagem/embed), chamar:

    import dedupe
    if dedupe.ja_processado(interaction.id):
        return
    # (ou dedupe.ja_processado(message.id) em handlers de mensagem)

Guarda só os IDs vistos recentemente (JANELA_SEGUNDOS) em memória — não
precisa de banco nem de setup, e limpa sozinho pra não vazar memória num
bot que fica rodando por dias.
========================================================================
"""

import time

# Discord reenvia o duplicado quase sempre em menos de 1s, mas alguns
# casos (retry de conexão) podem levar alguns segundos. 10s cobre folgado
# sem risco de bloquear uma ação legítima repetida de propósito pelo
# usuário (ex: chamar /addcoin de novo, minutos depois).
JANELA_SEGUNDOS = 10

_vistos: dict[int, float] = {}


def ja_processado(id_evento: int) -> bool:
    """True se esse id_evento (interaction.id ou message.id) já foi visto
    nos últimos JANELA_SEGUNDOS — nesse caso o chamador deve abortar SEM
    fazer o efeito colateral de novo. Se for a primeira vez, registra o id
    e retorna False (pode seguir normalmente)."""
    agora = time.monotonic()

    # limpeza oportunista: tira do dicionário tudo que já saiu da janela,
    # pra não crescer pra sempre em memória.
    for k in [k for k, t in _vistos.items() if agora - t > JANELA_SEGUNDOS]:
        _vistos.pop(k, None)

    if id_evento in _vistos:
        return True

    _vistos[id_evento] = agora
    return False
