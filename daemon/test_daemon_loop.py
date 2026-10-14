import os

os.environ.setdefault("MENSAJERIA_MAQUINA", "test-loop")
os.environ.setdefault("MENSAJERIA_BROKER_URL", "http://127.0.0.1:1")
os.environ.setdefault("MENSAJERIA_API_KEY", "test-key")

import daemon_loop  # noqa: E402


def test_loop_llama_a_daemon_main_y_espera_el_intervalo(monkeypatch):
    llamadas = []

    def main_falso():
        llamadas.append("pasada")
        if len(llamadas) >= 3:
            raise KeyboardInterrupt

    dormidas = []
    monkeypatch.setattr(daemon_loop.daemon, "main", main_falso)
    monkeypatch.setattr(daemon_loop.time, "sleep", lambda s: dormidas.append(s))

    try:
        daemon_loop.main()
    except KeyboardInterrupt:
        pass

    assert llamadas == ["pasada", "pasada", "pasada"]
    assert dormidas == [daemon_loop.INTERVALO_SEG] * 2


def test_loop_no_se_cae_si_una_pasada_tira_excepcion(monkeypatch):
    llamadas = []

    def main_falso():
        llamadas.append(1)
        if len(llamadas) == 1:
            raise RuntimeError("boom")
        raise KeyboardInterrupt

    monkeypatch.setattr(daemon_loop.daemon, "main", main_falso)
    monkeypatch.setattr(daemon_loop.time, "sleep", lambda s: None)

    try:
        daemon_loop.main()
    except KeyboardInterrupt:
        pass

    # Si hubiera muerto en la primera excepcion, llamadas tendria un solo elemento.
    assert len(llamadas) == 2
