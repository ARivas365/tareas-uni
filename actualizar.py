#!/usr/bin/env python3
"""Actualiza la lista de tareas pendientes de tus aulas Moodle.

Lee la configuración de la variable de entorno TAREAS_CONFIG (texto JSON) o,
si no existe, del archivo config.json junto a este script. Descarga el
calendario de cada universidad y escribe docs/tareas.json, que es lo que
muestra la página docs/index.html.

Cada universidad se lee según su "tipo":
  - moodle + "ical": el enlace de exportación del calendario (sin contraseña).
    Trae todas las fechas límite, entregadas o no.
  - moodle + "sitio" + "usuario"/"clave" (o "token"): la API de la app Moodle,
    la misma del bloque "Línea de tiempo". Solo trae lo que sigue pendiente.
  - chamilo + "sitio" + "usuario"/"clave": inicia sesión, recorre la sección
    Tareas de cada curso (sin las que ya enviaste) y suma los exámenes de la agenda.

Si una universidad falla, se conservan sus tareas de la última vez que
funcionó y la página avisa que están desactualizadas.

Uso:  python actualizar.py
Solo usa la biblioteca estándar de Python (3.9 o más nuevo).
"""
from __future__ import annotations

import html
import http.cookiejar
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None  # type: ignore

BASE = Path(__file__).resolve().parent
SALIDA = BASE / "docs" / "tareas.json"
DIAS_ATRAS = 14       # tareas vencidas que se siguen mostrando
DIAS_ADELANTE = 150   # hasta dónde mirar hacia adelante
TIMEOUT = 90  # UNISAL a veces tarda más de 40 s en responder
UA = "tareas-uni/1.0"
COLORES = ["#2f6fde", "#d9480f", "#2b8a3e", "#ae3ec9"]

# Eventos que NO son una fecha límite (apertura de cuestionarios, etc.)
RE_APERTURA = re.compile(
    r"(\b(opens|abre|se abre|apertura|habilitad[oa]|disponible desde)\s*\)?\s*$)"
    r"|(^\s*(apertura|inicio|abre)\b)"
    r"|(due to be graded|para calificar|fecha de calificaci[oó]n|recordatorio de calificaci[oó]n)",
    re.I,
)
# Coletillas que Moodle agrega al nombre de la actividad
RE_SUFIJO = re.compile(
    r"\s*[-–:(]?\s*(is due|closes|due|vence|cierra|se cierra|cierre|pendiente|"
    r"est[aá] en fecha de entrega|debe entregarse|fecha de entrega|"
    r"fecha l[ií]mite|finaliza|termina|submissions deadline|"
    r"fin del plazo de env[ií]os?)\s*\)?\s*$",
    re.I,
)
RE_PREFIJO = re.compile(
    r"^\s*(vencimiento de|cierre de|fecha l[ií]mite( de)?|fecha de entrega( de)?|"
    r"entrega( de)?|plazo( de)?)\s*:?\s+",
    re.I,
)
RE_CIERRE = re.compile(r"\b(closes|cierra|se cierra|cierre)\b", re.I)


# ----------------------------------------------------------------- utilidades
def ahora() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slug(texto: str) -> str:
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-") or "uni"


def ocultar_secretos(texto: str, *secretos: str) -> str:
    """Quita tokens y claves de los mensajes de error antes de guardarlos o mostrarlos."""
    texto = re.sub(r"((?:auth|ws)?token=|password=|api_key=)[^&\s'\"]+", r"\1***", str(texto), flags=re.I)
    for s in secretos:
        if s and len(s) >= 3:
            texto = texto.replace(s, "***")
    return texto


def descargar(fuente: str, datos: dict | None = None, opener=None) -> str:
    """Descarga una URL (GET, o POST si hay datos) o lee un archivo local."""
    if not re.match(r"^https?://", fuente, re.I):
        return Path(fuente).expanduser().read_text(encoding="utf-8-sig")
    cuerpo = urllib.parse.urlencode(datos).encode() if datos is not None else None
    req = urllib.request.Request(fuente, data=cuerpo, headers={"User-Agent": UA})
    abrir = opener.open if opener else urllib.request.urlopen
    with abrir(req, timeout=TIMEOUT) as r:
        return r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")


def texto_plano(s: str, maximo: int = 280) -> str:
    s = re.sub(r"<(br|/p|/div|/li)\b[^>]*>", "\n", s or "", flags=re.I)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s).strip()
    return s if len(s) <= maximo else s[: maximo - 1].rstrip() + "…"


def limpiar_titulo(nombre: str) -> tuple[str, str]:
    """Devuelve (título limpio, tipo) a partir del nombre del evento de Moodle."""
    tipo = "Cierre" if RE_CIERRE.search(nombre) else "Entrega"
    t = RE_PREFIJO.sub("", RE_SUFIJO.sub("", nombre.strip())).strip(" -–:")
    return (t or nombre.strip()), tipo


# --------------------------------------------------------------------- iCal
def desplegar_lineas(ics: str) -> list[str]:
    lineas: list[str] = []
    for linea in ics.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not linea.strip():
            continue
        if linea[:1] in (" ", "\t") and lineas:
            lineas[-1] += linea[1:]
        else:
            lineas.append(linea)
    return lineas


def desescapar(v: str) -> str:
    return (v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",")
             .replace("\\;", ";").replace("\\\\", "\\"))


def leer_fecha(params: dict, valor: str, zona_def: str):
    """Devuelve (datetime UTC o date, es_todo_el_dia)."""
    valor = valor.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", valor):
        return datetime.strptime(valor[:8], "%Y%m%d").date(), True
    utc = valor.endswith("Z")
    dt = datetime.strptime(valor.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if utc:
        return dt.replace(tzinfo=timezone.utc), False
    zona = params.get("TZID") or zona_def
    tz = timezone.utc
    if zona and ZoneInfo:
        try:
            tz = ZoneInfo(zona.strip('"'))
        except Exception:
            pass
    return dt.replace(tzinfo=tz).astimezone(timezone.utc), False


def parsear_ics(ics: str, zona_def: str) -> list[dict]:
    eventos, actual = [], None
    for linea in desplegar_lineas(ics):
        if linea == "BEGIN:VEVENT":
            actual = {}
        elif linea == "END:VEVENT":
            if actual is not None:
                eventos.append(actual)
            actual = None
        elif actual is not None and ":" in linea:
            izq, valor = linea.split(":", 1)
            nombre, *ps = izq.split(";")
            params = dict(p.split("=", 1) for p in ps if "=" in p)
            nombre = nombre.upper()
            if nombre in ("DTSTART", "DTEND", "DUE"):
                try:
                    actual[nombre] = leer_fecha(params, valor, zona_def)
                except ValueError:
                    pass
            else:
                actual[nombre] = desescapar(valor)
    return eventos


def sitio_desde_ical(url: str) -> str | None:
    m = re.match(r"^(https?://.+?)/calendar/", url, re.I)
    return m.group(1) if m else None


def tareas_desde_ical(uni: dict, desde: datetime, hasta: datetime) -> list[dict]:
    zona = uni.get("zona_horaria", "")
    sitio = sitio_desde_ical(uni["ical"])
    tareas = []
    for ev in parsear_ics(descargar(uni["ical"]), zona):
        fecha = ev.get("DUE") or ev.get("DTSTART")
        nombre = ev.get("SUMMARY", "").strip()
        if not fecha or not nombre or RE_APERTURA.search(nombre):
            continue
        valor, todo_el_dia = fecha
        if todo_el_dia:
            if not (desde.date() <= valor <= hasta.date()):
                continue
            vence = valor.isoformat()
            ts = int(datetime(valor.year, valor.month, valor.day, tzinfo=timezone.utc).timestamp())
        else:
            if not (desde <= valor <= hasta):
                continue
            vence, ts = iso(valor), int(valor.timestamp())
        uid = ev.get("UID", "") or f"{nombre}-{vence}"
        id_evento = uid.split("@", 1)[0]
        titulo, tipo = limpiar_titulo(nombre)
        url = ev.get("URL") or (f"{sitio}/calendar/view.php?view=day&time={ts}" if sitio else None)
        tareas.append({
            "id": f"{slug(uni['nombre'])}:{id_evento}",
            "uni": uni["nombre"],
            "curso": (ev.get("CATEGORIES") or "").strip(),
            "titulo": titulo,
            "tipo": tipo,
            "vence": vence,
            "todo_el_dia": todo_el_dia,
            "descripcion": texto_plano(ev.get("DESCRIPTION", "")),
            "url": url,
        })
    return tareas


# --------------------------------------------------------------- API Moodle
def token_moodle(sitio: str, usuario: str, clave: str) -> str:
    """Pide el token de la app Moodle con usuario y contraseña (lo mismo que hace la app)."""
    resp = json.loads(descargar(f"{sitio}/login/token.php",
                                {"username": usuario, "password": clave, "service": "moodle_mobile_app"}))
    if not resp.get("token"):
        raise RuntimeError(f"Moodle no dio acceso: {resp.get('error') or resp.get('errorcode') or 'sin token'}")
    return resp["token"]


def tareas_desde_api(uni: dict, desde: datetime, hasta: datetime) -> list[dict]:
    sitio = uni["sitio"].rstrip("/")
    token = uni.get("token") or token_moodle(sitio, uni["usuario"], uni["clave"])
    url = f"{sitio}/webservice/rest/server.php"
    eventos, despues_de = [], None
    for _ in range(20):  # como máximo 1000 eventos
        datos = {
            "wstoken": token,
            "wsfunction": "core_calendar_get_action_events_by_timesort",
            "moodlewsrestformat": "json",
            "timesortfrom": int(desde.timestamp()),
            "timesortto": int(hasta.timestamp()),
            "limitnum": 50,
        }
        if despues_de:
            datos["aftereventid"] = despues_de
        resp = json.loads(descargar(url, datos))
        if isinstance(resp, dict) and resp.get("exception"):
            raise RuntimeError(f"Moodle respondió: {resp.get('message') or resp.get('errorcode')}")
        lote = resp.get("events", [])
        eventos += lote
        if len(lote) < 50:
            break
        despues_de = resp.get("lastid") or lote[-1]["id"]

    tareas = []
    for ev in eventos:
        nombre = ev.get("name", "")
        if RE_APERTURA.search(nombre):
            continue
        titulo, tipo = limpiar_titulo(nombre)
        accion = ev.get("action") or {}
        momento = datetime.fromtimestamp(ev.get("timesort") or ev.get("timestart"), timezone.utc)
        tareas.append({
            "id": f"{slug(uni['nombre'])}:{ev['id']}",
            "uni": uni["nombre"],
            "curso": (ev.get("course") or {}).get("fullname", ""),
            "titulo": ev.get("activityname") or titulo,
            "tipo": tipo,
            "vence": iso(momento),
            "todo_el_dia": False,
            "descripcion": texto_plano(ev.get("description", "")),
            "url": ev.get("url") or accion.get("url"),
        })
    return tareas


# ------------------------------------------------------------------ Chamilo
RE_CLASE = re.compile(r"^\s*clase\b", re.I)
RE_EXAMEN = re.compile(r"\b(examen|parcial|final|evaluaci[oó]n|recuperatorio|prueba)\b", re.I)
RE_ENTREGA = re.compile(r"^\s*entrega de tarea\s*", re.I)
RE_CURSO = re.compile(
    r'<a\b[^>]*href="[^"]*/courses/([^/"]+)/index\.php(?:\?id_session=(\d+))?[^"]*"[^>]*>(.*?)</a>',
    re.I | re.S,
)
RE_SESION = re.compile(r'session(?:_view|/index)\.php\?session_id=(\d+)"[^>]*>(.*?)</a>', re.I | re.S)
# Tareas de Prácticas Supervisadas: solo cuentan si están en la sección de PPS
# (en los cursos normales aparecen con un formato antiguo que ya no se usa).
RE_TAREA_PPS = re.compile(r"\bpps\b|practicas? (profesionales? )?supervisadas?", re.I)
RE_SECCION_PPS = re.compile(r"\bpps\b|practica", re.I)


def normalizar(texto: str) -> str:
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", t.lower())).strip()


def coincide(curso: str, filtros: list[str]) -> bool:
    """¿El nombre del curso contiene alguno de los filtros? (sin tildes ni mayúsculas)"""
    n = normalizar(curso)
    return any(f in n for f in filtros)


def zona(nombre: str, horas_respaldo: int):
    """Zona horaria por nombre; si el sistema no la conoce, un desfase fijo."""
    if ZoneInfo:
        try:
            return ZoneInfo(nombre)
        except Exception:
            pass
    return timezone(timedelta(hours=horas_respaldo))


def fecha_chamilo(texto: str) -> datetime:
    # "2026-12-01T11:08:00-0300"
    return datetime.strptime(texto[:24], "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc)


def json_chamilo(url: str, opener) -> dict:
    texto = descargar(url, opener=opener)
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        raise RuntimeError("Chamilo no devolvió datos (¿se cerró la sesión?)") from None


def tareas_desde_chamilo(uni: dict, desde: datetime, hasta: datetime) -> list[dict]:
    """Inicia sesión en Chamilo y recorre la sección Tareas de cada curso.

    La agenda de Chamilo solo trae las tareas que el profesor agregó al
    calendario; la lista de cada curso las trae todas, con tu envío (si existe).
    De la agenda se toman además los exámenes y eventos de los cursos.
    """
    sitio = uni["sitio"].rstrip("/")
    tz = zona(uni.get("zona_horaria") or "America/Asuncion", -3)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    descargar(f"{sitio}/index.php", opener=opener)  # cookie de sesión
    pagina = descargar(f"{sitio}/index.php", {
        "login": uni["usuario"], "password": uni["clave"],
        "submitAuth": "", "_qf__form-login": "",
    }, opener)
    if 'id="form-login"' in pagina:
        raise RuntimeError("Chamilo rechazó el usuario o la contraseña")

    # 1) Cursos en los que estás inscrito (con su sesión / período)
    portal = descargar(f"{sitio}/user_portal.php", opener=opener)
    secciones = {sid: texto_plano(t, 120) for sid, t in RE_SESION.findall(portal)}
    cursos: dict[tuple[str, str], str] = {}
    for codigo, sesion, texto in RE_CURSO.findall(portal):
        nombre = texto_plano(texto, 120)
        if nombre and (codigo, sesion or "0") not in cursos:
            cursos[(codigo, sesion or "0")] = nombre
    if not cursos:
        raise RuntimeError("No se encontraron cursos en la página principal de Chamilo")
    filtros = [normalizar(f) for f in uni.get("cursos") or [] if normalizar(f)]
    if filtros:
        cursos = {k: v for k, v in cursos.items() if coincide(v, filtros)}
        if not cursos:
            raise RuntimeError("Ninguno de tus cursos coincide con la lista 'cursos' de la configuración")

    tareas = []
    # 2) Tareas de cada curso
    for (codigo, sesion), curso in cursos.items():
        es_seccion_pps = bool(RE_SECCION_PPS.search(normalizar(secciones.get(sesion, ""))))
        q = urllib.parse.urlencode({
            "a": "get_work_student", "cidReq": codigo, "id_session": sesion, "gidReq": 0,
            "gradebook": 0, "origin": "", "_search": "false", "rows": 500, "page": 1,
            "sidx": "", "sord": "asc",
        })
        datos = json_chamilo(f"{sitio}/main/inc/ajax/model.ajax.php?{q}", opener)
        for fila in datos.get("rows") or []:
            celdas = [str(c or "") for c in (fila.get("cell") or [])]
            if len(celdas) < 3:
                continue
            # columnas: ícono, título (enlace), fecha límite, fecha final, mi envío, ...
            fecha_txt = next((c.strip() for c in celdas[2:4]
                              if re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", c.strip())), "")
            if not fecha_txt:
                continue  # tarea sin fecha límite
            momento = datetime.strptime(fecha_txt, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz).astimezone(timezone.utc)
            if not (desde <= momento <= hasta):
                continue
            mi_envio = texto_plano(celdas[4], 200) if len(celdas) > 4 else ""
            if re.search(r"\d{4}-\d\d-\d\d", mi_envio):
                continue  # ya la enviaste
            titulo = texto_plano(celdas[1], 200)
            if RE_TAREA_PPS.search(normalizar(titulo)) and not es_seccion_pps:
                continue  # PPS fuera de su sección: formato antiguo
            m = re.search(r'href="([^"]+)"', celdas[1])
            tareas.append({
                "id": f"{slug(uni['nombre'])}:t{fila.get('id')}",
                "uni": uni["nombre"],
                "curso": f"{curso} · PPS" if es_seccion_pps else curso,
                "titulo": titulo,
                "tipo": "Entrega",
                "vence": iso(momento),
                "todo_el_dia": False,
                "descripcion": "",
                "url": html.unescape(m.group(1)) if m else f"{sitio}/courses/{codigo}/index.php?id_session={sesion}",
            })

    # 3) Exámenes y otros eventos de la agenda (las tareas ya salieron arriba)
    for ev in json_chamilo(f"{sitio}/main/inc/ajax/agenda.ajax.php?a=get_events&type=personal", opener):
        if ev.get("type") in ("admin", "platform"):  # avisos generales del sitio
            continue
        nombre = html.unescape(ev.get("title", "")).strip()
        if not nombre or RE_CLASE.search(nombre) or RE_ENTREGA.search(nombre):
            continue
        if filtros and not coincide(ev.get("course_name") or "", filtros):
            continue
        if (RE_TAREA_PPS.search(normalizar(nombre))
                and not RE_SECCION_PPS.search(normalizar(ev.get("session_name") or ""))):
            continue
        try:
            momento = fecha_chamilo(ev["start"])
        except (KeyError, ValueError):
            continue
        if not (desde <= momento <= hasta):
            continue
        tareas.append({
            "id": f"{slug(uni['nombre'])}:e{ev.get('real_id') or ev.get('id')}",
            "uni": uni["nombre"],
            "curso": (ev.get("course_name") or "").strip(),
            "titulo": nombre,
            "tipo": "Examen" if RE_EXAMEN.search(nombre) else "Evento",
            "vence": iso(momento),
            "todo_el_dia": False,
            "descripcion": texto_plano(ev.get("description") or ""),
            "url": f"{sitio}/main/calendar/agenda_js.php?type=personal",
        })
    return tareas


# --------------------------------------------------------------- principal
def cargar_config() -> dict:
    texto = os.environ.get("TAREAS_CONFIG", "").strip()
    if not texto:
        archivo = BASE / "config.json"
        if not archivo.exists():
            sys.exit("Falta la configuración: crea config.json (copia config.ejemplo.json) "
                     "o define la variable TAREAS_CONFIG.")
        texto = archivo.read_text(encoding="utf-8-sig")
    try:
        config = json.loads(texto)
    except json.JSONDecodeError as e:
        sys.exit(f"La configuración no es JSON válido: {e}")
    unis = config.get("universidades") or []
    if not unis:
        sys.exit("La configuración no tiene 'universidades'.")
    for i, u in enumerate(unis):
        if not u.get("nombre"):
            sys.exit(f"A la universidad #{i + 1} le falta 'nombre'.")
        u["tipo"] = (u.get("tipo") or "moodle").lower()
        con_login = bool(u.get("sitio") and u.get("usuario") and u.get("clave"))
        if u["tipo"] == "chamilo":
            u["modo"] = "chamilo"
            if not con_login:
                sys.exit(f"'{u['nombre']}' (Chamilo) necesita 'sitio', 'usuario' y 'clave'.")
        elif u["tipo"] == "moodle":
            if u.get("sitio") and (u.get("token") or con_login):
                u["modo"] = "api"
            elif u.get("ical"):
                u["modo"] = "ical"
            else:
                sys.exit(f"'{u['nombre']}' (Moodle) necesita 'ical', o 'sitio' + 'token', "
                         "o 'sitio' + 'usuario' + 'clave'.")
        else:
            sys.exit(f"'{u['nombre']}': tipo '{u['tipo']}' desconocido (usa 'moodle' o 'chamilo').")
        u.setdefault("color", COLORES[i % len(COLORES)])
    return config


LECTORES = {"ical": tareas_desde_ical, "api": tareas_desde_api, "chamilo": tareas_desde_chamilo}


def main() -> int:
    config = cargar_config()
    t0 = ahora()
    desde, hasta = t0 - timedelta(days=DIAS_ATRAS), t0 + timedelta(days=DIAS_ADELANTE)

    previo = {}
    if SALIDA.exists():
        try:
            previo = json.loads(SALIDA.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previo = {}
    previo_unis = {u.get("nombre"): u for u in previo.get("universidades", [])}

    todas, estado_unis = [], []
    for uni in config["universidades"]:
        nombre = uni["nombre"]
        modo = uni["modo"]
        info = {"nombre": nombre, "color": uni["color"], "modo": modo}
        try:
            tareas = LECTORES[modo](uni, desde, hasta)
            info.update(ok=True, error=None, ultimo_ok=iso(t0), total=len(tareas))
            print(f"[ok]    {nombre}: {len(tareas)} tareas ({modo})")
        except Exception as e:  # una universidad caída no detiene a la otra
            error = ocultar_secretos(f"{type(e).__name__}: {e}",
                                     uni.get("clave", ""), uni.get("token", ""), uni.get("usuario", ""))
            tareas = [t for t in previo.get("tareas", []) if t.get("uni") == nombre]
            ant = previo_unis.get(nombre, {})
            info.update(ok=False, error=error, ultimo_ok=ant.get("ultimo_ok"), total=len(tareas))
            print(f"[error] {nombre}: {error} (se mantienen {len(tareas)} tareas anteriores)")
        estado_unis.append(info)
        todas.extend(tareas)

    # sin duplicados y ordenadas por fecha
    unicas = {t["id"]: t for t in todas}
    ordenadas = sorted(unicas.values(), key=lambda t: (t["vence"] if not t["todo_el_dia"]
                                                       else t["vence"] + "T23:59:59Z"))
    salida = {"actualizado": iso(t0), "universidades": estado_unis, "tareas": ordenadas}
    SALIDA.parent.mkdir(parents=True, exist_ok=True)
    SALIDA.write_text(json.dumps(salida, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Guardado {SALIDA.relative_to(BASE)} con {len(ordenadas)} tareas.")
    return 0 if any(u["ok"] for u in estado_unis) else 1


if __name__ == "__main__":
    sys.exit(main())
