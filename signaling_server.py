"""
Servidor de sinalização WebRTC - Verificação de Tela (FFZ SYSTEM)
-------------------------------------------------------------------
App separado, roda como app TYPE=site no Discloud (não é o bot).

Serve duas páginas:
  GET /share/<token>  -> quem vai compartilhar a tela (getDisplayMedia)
  GET /watch/<token>  -> quem vai assistir (mediador)

E dois endpoints extras:
  GET /ws/<token>?role=share|watch  -> WebSocket, troca de SDP/ICE
  GET /status/<token>               -> o BOT consulta isso periodicamente
                                        pra saber se alguém já entrou

O vídeo em si NUNCA passa por esse servidor - é WebRTC peer-to-peer,
o servidor só ajuda os dois navegadores a se acharem (sinalização).

Discloud exige, pra TYPE=site: porta 8080 e host 0.0.0.0.
"""

import os
import json
import logging
from aiohttp import web, WSMsgType

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("signaling")

PORT = 8080  # obrigatório pelo Discloud pra apps TYPE=site

# token -> {"share": WebSocketResponse | None, "watchers": {id: ws}, "joined": bool}
salas: dict[str, dict] = {}


def _sala(token: str) -> dict:
    if token not in salas:
        salas[token] = {"share": None, "watchers": {}, "joined": False}
    return salas[token]


# ---------------------------------------------------------------------
# Páginas HTML (embutidas aqui pra ser um único arquivo, fácil de subir)
# ---------------------------------------------------------------------

SHARE_HTML = """<!DOCTYPE html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Verificação de Tela</title>
<style>
  body {{ background:#1e1f22; color:#e3e5e8; font-family:sans-serif; text-align:center; padding:40px 16px; }}
  button {{ background:#5865f2; color:#fff; border:none; padding:14px 28px; font-size:16px; border-radius:8px; cursor:pointer; }}
  button:disabled {{ background:#3a3c42; }}
  #status {{ margin-top:20px; color:#949ba4; }}
  video {{ width:90%; max-width:480px; border-radius:8px; margin-top:20px; }}
</style>
</head>
<body>
  <h2>🔎 Verificação de Tela</h2>
  <p>Clique abaixo e escolha a tela ou janela pra compartilhar.</p>
  <button id="btn">Compartilhar Tela</button>
  <p id="status">aguardando...</p>
  <video id="preview" autoplay muted playsinline></video>

<script>
const token = "{token}";
const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
const wsUrl = proto + "//" + window.location.host + "/ws/" + token + "?role=share";
const ws = new WebSocket(wsUrl);
const statusEl = document.getElementById("status");
const btn = document.getElementById("btn");
const preview = document.getElementById("preview");

let localStream = null;
const peers = {{}}; // watcher_id -> RTCPeerConnection

ws.onopen = () => statusEl.textContent = "conectado, pronto pra compartilhar";
ws.onclose = () => statusEl.textContent = "conexão encerrada";

ws.onmessage = async (ev) => {{
  const msg = JSON.parse(ev.data);

  if (msg.type === "new_watcher") {{
    if (!localStream) return; // ainda não compartilhou nada
    const pc = criarPeer(msg.watcher_id);
    localStream.getTracks().forEach(t => pc.addTrack(t, localStream));
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    ws.send(JSON.stringify({{ type: "offer", watcher_id: msg.watcher_id, sdp: offer }}));
  }}

  if (msg.type === "answer") {{
    const pc = peers[msg.watcher_id];
    if (pc) await pc.setRemoteDescription(msg.sdp);
  }}

  if (msg.type === "candidate") {{
    const pc = peers[msg.watcher_id];
    if (pc && msg.candidate) await pc.addIceCandidate(msg.candidate);
  }}
}};

function criarPeer(watcherId) {{
  const pc = new RTCPeerConnection({{ iceServers: [{{ urls: "stun:stun.l.google.com:19302" }}] }});
  pc.onicecandidate = (e) => {{
    if (e.candidate) {{
      ws.send(JSON.stringify({{ type: "candidate", watcher_id: watcherId, candidate: e.candidate }}));
    }}
  }};
  peers[watcherId] = pc;
  return pc;
}}

btn.onclick = async () => {{
  try {{
    localStream = await navigator.mediaDevices.getDisplayMedia({{ video: true, audio: false }});
    preview.srcObject = localStream;
    btn.disabled = true;
    btn.textContent = "Compartilhando";
    statusEl.textContent = "tela sendo compartilhada";

    localStream.getVideoTracks()[0].onended = () => {{
      statusEl.textContent = "compartilhamento encerrado";
      btn.disabled = false;
      btn.textContent = "Compartilhar Tela";
    }};
  }} catch (err) {{
    statusEl.textContent = "permissão negada ou cancelada";
  }}
}};
</script>
</body>
</html>
"""

WATCH_HTML = """<!DOCTYPE html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Análise de Tela</title>
<style>
  body {{ background:#1e1f22; color:#e3e5e8; font-family:sans-serif; text-align:center; padding:20px 16px; }}
  #status {{ margin-top:12px; color:#949ba4; }}
  video {{ width:95%; max-width:800px; border-radius:8px; margin-top:16px; background:#000; }}
</style>
</head>
<body>
  <h2>🔎 Análise de Tela</h2>
  <p id="status">conectando...</p>
  <video id="remote" autoplay playsinline controls></video>

<script>
const token = "{token}";
const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
const wsUrl = proto + "//" + window.location.host + "/ws/" + token + "?role=watch";
const ws = new WebSocket(wsUrl);
const statusEl = document.getElementById("status");
const remoteVideo = document.getElementById("remote");

let pc = null;
let myId = null;
let jaAvisou = false;

ws.onopen = () => statusEl.textContent = "aguardando quem vai compartilhar...";

ws.onmessage = async (ev) => {{
  const msg = JSON.parse(ev.data);

  if (msg.type === "welcome") {{
    myId = msg.id;
  }}

  if (msg.type === "offer") {{
    pc = new RTCPeerConnection({{ iceServers: [{{ urls: "stun:stun.l.google.com:19302" }}] }});

    pc.ontrack = (e) => {{
      remoteVideo.srcObject = e.streams[0];
      statusEl.textContent = "recebendo transmissão";
    }};

    pc.onicecandidate = (e) => {{
      if (e.candidate) {{
        ws.send(JSON.stringify({{ type: "candidate", watcher_id: myId, candidate: e.candidate }}));
      }}
    }};

    pc.onconnectionstatechange = () => {{
      if (pc.connectionState === "connected" && !jaAvisou) {{
        jaAvisou = true;
        ws.send(JSON.stringify({{ type: "joined", watcher_id: myId }}));
      }}
    }};

    await pc.setRemoteDescription(msg.sdp);
    const answer = await pc.createAnswer();
    await pc.setLocalDescription(answer);
    ws.send(JSON.stringify({{ type: "answer", watcher_id: myId, sdp: answer }}));
  }}

  if (msg.type === "candidate") {{
    if (pc && msg.candidate) await pc.addIceCandidate(msg.candidate);
  }}
}};

ws.onclose = () => statusEl.textContent = "conexão encerrada";
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------
# Rotas HTTP
# ---------------------------------------------------------------------

async def handle_share(request: web.Request) -> web.Response:
    token = request.match_info["token"]
    return web.Response(text=SHARE_HTML.format(token=token), content_type="text/html")


async def handle_watch(request: web.Request) -> web.Response:
    token = request.match_info["token"]
    return web.Response(text=WATCH_HTML.format(token=token), content_type="text/html")


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    token = request.match_info["token"]
    role = request.query.get("role")
    if role not in ("share", "watch"):
        return web.Response(status=400, text="role inválido")

    ws = web.WebSocketResponse()
    await ws.prepare(request)
    sala = _sala(token)

    watcher_id = None
    if role == "share":
        sala["share"] = ws
        # avisa a quem já estava esperando (se algum watcher entrou antes do share)
        for wid in sala["watchers"]:
            await ws.send_str(json.dumps({"type": "new_watcher", "watcher_id": wid}))
    else:
        import uuid as _uuid
        watcher_id = _uuid.uuid4().hex[:8]
        sala["watchers"][watcher_id] = ws
        await ws.send_str(json.dumps({"type": "welcome", "id": watcher_id}))
        if sala["share"]:
            await sala["share"].send_str(json.dumps({"type": "new_watcher", "watcher_id": watcher_id}))

    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            tipo = data.get("type")
            wid = data.get("watcher_id")

            if tipo == "joined":
                sala["joined"] = True
                continue

            # repassa offer/answer/candidate pro outro lado da mesma sala
            if role == "share" and wid in sala["watchers"]:
                await sala["watchers"][wid].send_str(json.dumps(data))
            elif role == "watch" and sala["share"]:
                await sala["share"].send_str(json.dumps(data))

    finally:
        if role == "share":
            sala["share"] = None
        elif watcher_id:
            sala["watchers"].pop(watcher_id, None)

    return ws


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def handle_status(request: web.Request) -> web.Response:
    """Endpoint que o bot consulta periodicamente pra saber se alguém
    já entrou na call daquele token (substitui o webhook, já que bots
    no Discloud não têm porta externa pra RECEBER aviso)."""
    token = request.match_info["token"]
    sala = salas.get(token)
    entrou = bool(sala and sala.get("joined"))
    return web.json_response({"joined": entrou})


def criar_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/share/{token}", handle_share)
    app.router.add_get("/watch/{token}", handle_watch)
    app.router.add_get("/ws/{token}", handle_ws)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/status/{token}", handle_status)
    return app


if __name__ == "__main__":
    web.run_app(criar_app(), host="0.0.0.0", port=PORT)
