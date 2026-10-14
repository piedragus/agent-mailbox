import os
import tempfile

import pytest

os.environ["MENSAJERIA_API_KEY"] = "test-key"

import app as broker_app  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    db_fd, db_path = tempfile.mkstemp()
    monkeypatch.setattr(broker_app, "DB_PATH", db_path)
    broker_app.init_db()
    broker_app.app.config["TESTING"] = True
    with broker_app.app.test_client() as c:
        yield c
    os.close(db_fd)
    os.unlink(db_path)


HEADERS = {"X-API-Key": "test-key"}


def crear(client, **kwargs):
    body = {"de": "nodo-b", "cuerpo": "hola"}
    body.update(kwargs)
    return client.post("/api/mensajes", json=body, headers=HEADERS)


def test_requiere_api_key(client):
    r = client.post("/api/mensajes", json={"de": "x", "cuerpo": "y"})
    assert r.status_code == 401


def test_crear_mensaje_broadcast(client):
    r = crear(client)
    assert r.status_code == 201
    assert r.get_json()["estado"] == "pendiente"


def test_requiere_confirmacion_no_es_reclamable(client):
    crear(client, requiere_confirmacion=True, para="nodo-a")
    r = client.get("/api/mensajes/pendientes?maquina=nodo-a", headers=HEADERS)
    assert r.get_json()["mensajes"] == []
    esperando = client.get("/api/mensajes/esperando_humano", headers=HEADERS)
    assert len(esperando.get_json()["mensajes"]) == 1


def test_maquina_no_puede_tomar_su_propio_mensaje(client):
    crear(client, de="nodo-a", para="nodo-a")
    r = client.get("/api/mensajes/pendientes?maquina=nodo-a", headers=HEADERS)
    assert r.get_json()["mensajes"] == []


def test_broadcast_lo_puede_tomar_cualquiera_menos_el_emisor(client):
    crear(client, de="nodo-a", para=None)
    r = client.get("/api/mensajes/pendientes?maquina=nodo-b", headers=HEADERS)
    assert len(r.get_json()["mensajes"]) == 1
    r2 = client.get("/api/mensajes/pendientes?maquina=nodo-a", headers=HEADERS)
    assert r2.get_json()["mensajes"] == []


def test_reclamo_marca_tomado_y_no_se_repite(client):
    crear(client, para="nodo-a")
    r1 = client.get("/api/mensajes/pendientes?maquina=nodo-a", headers=HEADERS)
    assert len(r1.get_json()["mensajes"]) == 1
    r2 = client.get("/api/mensajes/pendientes?maquina=nodo-a", headers=HEADERS)
    assert r2.get_json()["mensajes"] == []


def test_anti_loop_corta_hilo_largo(client):
    hilo_id = None
    for i in range(11):
        r = crear(client, de="nodo-b" if i % 2 == 0 else "nodo-a",
                   para="nodo-a" if i % 2 == 0 else "nodo-b",
                   hilo_id=hilo_id)
        hilo_id = r.get_json()["hilo_id"]
    hilo = client.get(f"/api/mensajes/hilo/{hilo_id}", headers=HEADERS).get_json()
    assert hilo["mensajes"][-1]["estado"] == "esperando_humano"


def test_marcar_fallido_reintenta_hasta_tope(client):
    r = crear(client)
    mensaje_id = r.get_json()["id"]
    for _ in range(2):
        resp = client.post(f"/api/mensajes/{mensaje_id}/fallar", json={"error": "x"},
                            headers=HEADERS)
        assert resp.get_json()["estado"] == "pendiente"
    resp = client.post(f"/api/mensajes/{mensaje_id}/fallar", json={"error": "x"},
                        headers=HEADERS)
    assert resp.get_json()["estado"] == "fallido"


def test_capacidades_upsert(client):
    client.put("/api/capacidades/nodo-a", json={"descripcion": "v1"}, headers=HEADERS)
    client.put("/api/capacidades/nodo-a", json={"descripcion": "v2"}, headers=HEADERS)
    r = client.get("/api/capacidades", headers=HEADERS)
    caps = r.get_json()["capacidades"]
    assert len(caps) == 1
    assert caps[0]["descripcion"] == "v2"


def test_primera_request_real_inicializa_el_schema_sola(monkeypatch):
    """Reproduce el bug real del deploy (14/sep): bajo gunicorn nadie llama
    init_db() a mano (eso solo pasaba bajo `if __name__ == "__main__"`, que
    gunicorn nunca ejecuta). Este test simula exactamente eso -- levanta el
    client SIN llamar init_db() explícito -- y confirma que la primera
    request real que toca la DB la inicializa sola."""
    db_fd, db_path = tempfile.mkstemp()
    monkeypatch.setattr(broker_app, "DB_PATH", db_path)
    monkeypatch.setattr(broker_app, "_db_inicializada", False)
    broker_app.app.config["TESTING"] = True
    with broker_app.app.test_client() as c:
        r = c.put("/api/capacidades/nodo-a", json={"descripcion": "x"}, headers=HEADERS)
        assert r.status_code == 200
    os.close(db_fd)
    os.unlink(db_path)


def test_invocacion_valida_resultado(client):
    r = crear(client)
    mensaje_id = r.get_json()["id"]
    bad = client.post("/api/invocaciones",
                       json={"maquina": "nodo-a", "mensaje_id": mensaje_id,
                             "resultado": "no-existe"},
                       headers=HEADERS)
    assert bad.status_code == 400
    ok = client.post("/api/invocaciones",
                      json={"maquina": "nodo-a", "mensaje_id": mensaje_id,
                            "resultado": "exito", "resumen": "listo"},
                      headers=HEADERS)
    assert ok.status_code == 201


def _envejecer(db_path, mensaje_id, minutos):
    import sqlite3
    from datetime import datetime, timedelta, timezone
    viejo = (datetime.now(timezone.utc) - timedelta(minutes=minutos)
             ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    c = sqlite3.connect(db_path)
    c.execute("UPDATE mensajes SET actualizado_en = ? WHERE id = ?", (viejo, mensaje_id))
    c.commit()
    c.close()


def test_tomado_vencido_vuelve_a_la_cola(client):
    mid = crear(client, para="nodo-a").get_json()["id"]
    client.get("/api/mensajes/pendientes?maquina=nodo-a", headers=HEADERS)  # lo toma
    # todavia fresco: nadie mas lo ve
    r = client.get("/api/mensajes/pendientes?maquina=otra", headers=HEADERS)
    assert r.get_json()["mensajes"] == []
    _envejecer(broker_app.DB_PATH, mid, 31)
    r = client.get("/api/mensajes/pendientes?maquina=nodo-a", headers=HEADERS)
    ms = r.get_json()["mensajes"]
    assert [m["id"] for m in ms] == [mid]
    assert ms[0]["intentos"] == 1


def test_tomado_vencido_tres_veces_pasa_a_fallido(client):
    mid = crear(client, para="nodo-a").get_json()["id"]
    for _ in range(3):
        client.get("/api/mensajes/pendientes?maquina=nodo-a", headers=HEADERS)
        _envejecer(broker_app.DB_PATH, mid, 31)
    r = client.get("/api/mensajes/pendientes?maquina=nodo-a", headers=HEADERS)
    assert r.get_json()["mensajes"] == []
    import sqlite3
    c = sqlite3.connect(broker_app.DB_PATH)
    assert c.execute("SELECT estado FROM mensajes WHERE id=?", (mid,)).fetchone()[0] == "fallido"
    c.close()


def test_estado_resume_por_identidad(client):
    crear(client, para="nodo-a", asunto="uno")
    crear(client, para="nodo-a", asunto="dos")
    crear(client, para=None, asunto="todos")
    client.get("/api/mensajes/pendientes?maquina=nodo-a&limite=1", headers=HEADERS)
    crear(client, requiere_confirmacion=True, asunto="delicado")
    e = client.get("/api/estado", headers=HEADERS).get_json()
    assert len(e["identidades"]["nodo-a"]["tomados"]) == 1
    assert e["identidades"]["nodo-a"]["tomados"][0]["minutos"] < 1
    assert e["por_estado"]["esperando_humano"] == 1
    assert len(e["esperando_humano"]) == 1
    assert e["pendientes_broadcast"] == 1


def test_estado_requiere_api_key(client):
    assert client.get("/api/estado").status_code == 401


def test_recibo_leido_y_respondido(client):
    m = crear(client, de="nodo-a", para="nodo-c", asunto="x").get_json()
    r = client.get(f"/api/mensajes/{m['id']}/recibo", headers=HEADERS).get_json()
    assert r["leido"] is False and r["leido_en"] is None and r["estado"] == "pendiente"
    client.get("/api/mensajes/pendientes?maquina=nodo-c", headers=HEADERS)
    r = client.get(f"/api/mensajes/{m['id']}/recibo", headers=HEADERS).get_json()
    assert r["leido"] is True and r["leido_por"] == "nodo-c" and r["leido_en"]
    crear(client, de="nodo-c", para="nodo-a", responde_a_id=m["id"], hilo_id=m["hilo_id"])
    r = client.get(f"/api/mensajes/{m['id']}/recibo", headers=HEADERS).get_json()
    assert r["estado"] == "respondido" and r["respondido_en"]


def test_error_de_fallar_queda_guardado(client):
    m = crear(client, de="nodo-a", para="nodo-c").get_json()
    client.get("/api/mensajes/pendientes?maquina=nodo-c", headers=HEADERS)
    client.post(f"/api/mensajes/{m['id']}/fallar", json={"error": "login vencido"}, headers=HEADERS)
    r = client.get(f"/api/mensajes/{m['id']}/recibo", headers=HEADERS).get_json()
    assert r["ultimo_error"] == "login vencido"


def test_enviados_y_sin_leer(client, monkeypatch):
    crear(client, de="nodo-a", para="nodo-c", asunto="a")
    crear(client, de="nodo-a", para="nodo-c", asunto="b")
    client.get("/api/mensajes/pendientes?maquina=nodo-c&limite=1", headers=HEADERS)
    todos = client.get("/api/mensajes/enviados?de=nodo-a", headers=HEADERS).get_json()["mensajes"]
    assert len(todos) == 2
    sin = client.get("/api/mensajes/enviados?de=nodo-a&solo_sin_leer=1", headers=HEADERS).get_json()["mensajes"]
    assert len(sin) == 1 and sin[0]["asunto"] == "b"
    monkeypatch.setattr(broker_app, "SIN_LEER_MIN", -1)
    e = client.get("/api/estado", headers=HEADERS).get_json()
    assert [m["asunto"] for m in e["sin_leer"]] == ["b"]


def test_alerta_al_remitente_si_no_leen(client, monkeypatch):
    m = crear(client, de="nodo-a", para="nodo-c", asunto="hola").get_json()
    monkeypatch.setattr(broker_app, "SIN_LEER_MIN", -1)
    a = client.get("/api/alertas?para=nodo-a&marcar=1", headers=HEADERS).get_json()["alertas"]
    assert len(a) == 1 and "no leyo" in a[0]["asunto"] and a[0]["hilo_id"] == m["hilo_id"]
    # una sola alerta, y marcada como vista no se repite
    assert client.get("/api/alertas?para=nodo-a", headers=HEADERS).get_json()["alertas"] == []


def test_alerta_por_fallo_incluye_error_y_no_alerta_sobre_alertas(client):
    m = crear(client, de="nodo-a", para="nodo-c").get_json()
    for _ in range(3):
        client.get("/api/mensajes/pendientes?maquina=nodo-c", headers=HEADERS)
        client.post(f"/api/mensajes/{m['id']}/fallar", json={"error": "login vencido"}, headers=HEADERS)
    a = client.get("/api/alertas?para=nodo-a", headers=HEADERS).get_json()["alertas"]
    assert len(a) == 1 and "login vencido" in a[0]["cuerpo"]
    # nada para el humano ni alertas de alertas
    crear(client, de="humano", para="nodo-c")
    assert client.get("/api/alertas?para=humano", headers=HEADERS).get_json()["alertas"] == []


def test_alertas_requiere_para(client):
    assert client.get("/api/alertas", headers=HEADERS).status_code == 400


# --- Autenticacion por identidad ---------------------------------------------------

@pytest.fixture
def seguro(client, monkeypatch, tmp_path):
    import json
    f = tmp_path / "keys.json"
    f.write_text(json.dumps({"alfa": "ka", "beta": "kb", "humano": "kh"}))
    monkeypatch.setattr(broker_app, "KEYS_FILE", str(f))
    monkeypatch.setattr(broker_app, "_keys_cache", {"mtime": None, "keys": {}})
    return client


def H(k):
    return {"X-API-Key": k}


def test_clave_fija_la_identidad_y_bloquea_suplantacion(seguro):
    r = seguro.post("/api/mensajes", json={"de": "beta", "cuerpo": "x", "para": "alfa"}, headers=H("ka"))
    assert r.status_code == 403
    r = seguro.post("/api/mensajes", json={"cuerpo": "x", "para": "beta"}, headers=H("ka"))
    assert r.status_code == 201  # sin 'de': lo pone el broker
    hilo = seguro.get("/api/mensajes/enviados?de=alfa", headers=H("ka")).get_json()["mensajes"]
    assert hilo[0]["de"] == "alfa"


def test_no_se_lee_la_cola_de_otro(seguro):
    assert seguro.get("/api/mensajes/pendientes?maquina=beta", headers=H("ka")).status_code == 403
    assert seguro.get("/api/mensajes/pendientes?maquina=alfa", headers=H("ka")).status_code == 200


def test_capacidades_solo_las_propias(seguro):
    assert seguro.put("/api/capacidades/beta", json={"descripcion": "x"}, headers=H("ka")).status_code == 403
    assert seguro.put("/api/capacidades/alfa", json={"descripcion": "x"}, headers=H("ka")).status_code == 200


def test_hilo_solo_para_participantes(seguro):
    m = seguro.post("/api/mensajes", json={"cuerpo": "secreto", "para": "beta"}, headers=H("ka")).get_json()
    seguro.post("/api/mensajes", json={"cuerpo": "otro", "para": "beta"}, headers=H("ka"))
    # gamma no existe como clave; usamos la humana para verificar acceso total y beta como participante
    assert seguro.get(f"/api/mensajes/hilo/{m['hilo_id']}", headers=H("kb")).status_code == 200
    assert seguro.get(f"/api/mensajes/hilo/{m['hilo_id']}", headers=H("kh")).status_code == 200


def test_hilo_ajeno_prohibido(seguro, monkeypatch):
    import json
    monkeypatch.setattr(broker_app, "ADMINS", ())
    m = seguro.post("/api/mensajes", json={"cuerpo": "x", "para": "beta"}, headers=H("ka")).get_json()
    # una tercera identidad que no participa
    (open(broker_app.KEYS_FILE, "w")).write(json.dumps({"alfa": "ka", "beta": "kb", "humano": "kh", "gamma": "kg"}))
    monkeypatch.setattr(broker_app, "_keys_cache", {"mtime": None, "keys": {}})
    assert seguro.get(f"/api/mensajes/hilo/{m['hilo_id']}", headers=H("kg")).status_code == 403
    assert seguro.get(f"/api/mensajes/{m['id']}/recibo", headers=H("kg")).status_code == 403
    assert seguro.get("/api/estado", headers=H("kg")).status_code == 403


def test_fallar_solo_quien_tomo(seguro):
    m = seguro.post("/api/mensajes", json={"cuerpo": "x", "para": "beta"}, headers=H("ka")).get_json()
    seguro.get("/api/mensajes/pendientes?maquina=beta", headers=H("kb"))
    assert seguro.post(f"/api/mensajes/{m['id']}/fallar", json={}, headers=H("ka")).status_code == 403
    assert seguro.post(f"/api/mensajes/{m['id']}/fallar", json={}, headers=H("kb")).status_code == 200


def test_legacy_off_rechaza_clave_compartida(seguro, monkeypatch):
    assert seguro.get("/api/capacidades", headers=HEADERS).status_code == 200
    monkeypatch.setattr(broker_app, "LEGACY_KEY", False)
    assert seguro.get("/api/capacidades", headers=HEADERS).status_code == 401
    assert seguro.get("/api/capacidades", headers=H("ka")).status_code == 200


def test_limite_de_cuerpo_y_rate(seguro, monkeypatch):
    monkeypatch.setattr(broker_app, "MAX_CUERPO", 10)
    r = seguro.post("/api/mensajes", json={"cuerpo": "x" * 11, "para": "beta"}, headers=H("ka"))
    assert r.status_code == 413
    monkeypatch.setattr(broker_app, "MAX_CUERPO", 1000)
    monkeypatch.setattr(broker_app, "RATE_POST_MIN", 2)
    broker_app._posts_recientes.clear()
    codigos = [seguro.post("/api/mensajes", json={"cuerpo": "x", "para": "beta"}, headers=H("ka")).status_code for _ in range(3)]
    assert codigos == [201, 201, 429]


def test_admin_tampoco_puede_suplantar(seguro, monkeypatch):
    monkeypatch.setattr(broker_app, "ADMINS", ("alfa",))
    r = seguro.post("/api/mensajes", json={"de": "beta", "cuerpo": "x", "para": "humano"}, headers=H("ka"))
    assert r.status_code == 403
