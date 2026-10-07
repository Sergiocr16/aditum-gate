"""Estructura de admin.html (el editor local): sin leerlo entero ni a mano.

admin.html se edita con scripts de reemplazo y pesa ~150 KB, asi que un
<div> abierto dos veces pasa desapercibido en el diff y en jsdom si no se
mira justo esa seccion. Paso en produccion: la tarjeta de la bitacora quedo
abierta dos veces y Red, Hikvision, Respaldo y Seguridad quedaron anidadas
dentro de ella, ocultas cada vez que la bitacora se ocultaba (siempre).
"""
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

ADMIN_HTML = Path(__file__).resolve().parents[1] / "aditum_gate" / "static" / "admin.html"
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
         "source", "track", "wbr"}


class _SectionParser(HTMLParser):
    """Registra, para cada tarjeta de seccion (div.card#sec-*), que otras
    tarjetas de seccion tiene como ancestros."""

    def __init__(self):
        super().__init__()
        self.stack = []        # (tag, id de seccion o None)
        self.sections = {}     # id -> [ids de secciones ancestras]

    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            return
        attrs = dict(attrs)
        sec = None
        if (tag == "div" and (attrs.get("id") or "").startswith("sec-")
                and "card" in (attrs.get("class") or "").split()):
            sec = attrs["id"]
            self.sections[sec] = [s for _, s in self.stack if s]
        self.stack.append((tag, sec))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return


def section_problems(html):
    """Ids repetidos y tarjetas de seccion anidadas en otra. Vacio si todo
    esta bien."""
    problems = []
    ids = re.findall(r'\sid="([^"]+)"', html)
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        problems.append("id repetido: %s (x%d)" % (dup, ids.count(dup)))
    parser = _SectionParser()
    parser.feed(html)
    for sec, ancestors in parser.sections.items():
        if ancestors:
            problems.append("seccion %s anidada dentro de %s" % (sec, ancestors))
    return problems


class AdminHtmlTests(unittest.TestCase):
    def test_sin_ids_repetidos_ni_secciones_anidadas(self):
        self.assertEqual(section_problems(ADMIN_HTML.read_text(encoding="utf-8")), [])

    def test_el_chequeo_detecta_una_tarjeta_abierta_dos_veces(self):
        html = ('<div class="card" id="sec-a"></div>\n'
                '<div class="card" id="sec-b">\n<div class="card" id="sec-b"><input id="x"></div>\n'
                '<div class="card" id="sec-c"></div>')
        self.assertEqual(section_problems(html),
                         ["id repetido: sec-b (x2)",
                          "seccion sec-b anidada dentro de ['sec-b']",
                          "seccion sec-c anidada dentro de ['sec-b']"])
