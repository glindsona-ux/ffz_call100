"""Mini servidor web (Flask) só pra o Render ter uma porta HTTP pra responder
e o UptimeRobot ter uma URL pra pingar a cada 5 min. O bot roda normal; isso
só roda numa thread ao lado."""
import os
import threading
import logging

from flask import Flask

app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)  # não enche o log com cada ping


@app.route("/")
def home():
    return "FFZ Call online", 200


@app.route("/health")
def health():
    return "ok", 200


def _rodar():
    # O Render injeta a porta na variável PORT -- precisa escutar nela, em 0.0.0.0
    porta = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=porta)


def keep_alive():
    threading.Thread(target=_rodar, daemon=True).start()
