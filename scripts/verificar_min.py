"""
Comprueba que cada *.min.js siga correspondiendo a su fuente *.js.

La app móvil sirve las versiones minificadas, así que un .min desactualizado hace
que una corrección publicada no llegue nunca al usuario y sea muy difícil de
diagnosticar. No hay build automático, de ahí esta verificación.

Se comparan solo los literales de texto (comillas simples, dobles y plantillas):
los minificadores del proyecto renombran variables, pero ninguno toca el
contenido de las cadenas. Sirve para detectar mensajes, selectores, claves de
almacenamiento o rutas que se cambiaron en la fuente y no se regeneraron.

Uso: python scripts/verificar_min.py    (código de salida 1 si algo divergió)
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DIRECTORIOS = [RAIZ / "app" / "static", RAIZ / "andes_mobile" / "static"]

# Los huecos ${...} de una plantilla contienen código, no texto.
INTERPOLACION = re.compile(r"\$\{.*?\}", re.S)

# Tras estos caracteres una `/` abre una expresión regular, no una división.
# Sin esta distinción, una regex como /['"]/ haría creer al escáner que empieza
# una cadena y desalinearía todo lo que viene después (crítico en un .min, que
# es una sola línea gigante).
ANTES_DE_REGEX = set("(,=:[!&|?{};+-*%~^<>")
PALABRAS_ANTES_DE_REGEX = ("return", "typeof", "case", "in", "of", "new", "delete", "throw", "do", "else")


def literales(texto: str) -> Counter[str]:
    """
    Extrae las cadenas del código con un recorrido lineal.

    Hace falta escanear en vez de usar expresiones regulares sueltas: un archivo
    minificado ocupa una sola línea, así que borrar los comentarios `//` con un
    regex previo se llevaría por delante el resto del archivo en cuanto aparezca
    un `//` dentro de una cadena o de una expresión regular.
    """
    cuenta: Counter[str] = Counter()
    i, n = 0, len(texto)
    anterior = ""  # último carácter significativo, para reconocer las regex
    while i < n:
        c = texto[i]
        if c == "/" and i + 1 < n:
            siguiente = texto[i + 1]
            if siguiente == "/":
                salto = texto.find("\n", i)
                i = n if salto == -1 else salto + 1
                continue
            if siguiente == "*":
                fin = texto.find("*/", i + 2)
                i = n if fin == -1 else fin + 2
                continue
            if _abre_regex(texto, i, anterior):
                i = _fin_de_regex(texto, i)
                anterior = "/"
                continue
            anterior = c
            i += 1
            continue
        if c in "'\"`":
            comilla = c
            i += 1
            inicio = i
            while i < n:
                if texto[i] == "\\":
                    i += 2
                    continue
                if texto[i] == comilla:
                    break
                if comilla != "`" and texto[i] == "\n":
                    break  # cadena sin cerrar: no es un literal válido
                i += 1
            valor = texto[inicio:i]
            i += 1
            if comilla == "`":
                valor = INTERPOLACION.sub("${}", valor)
            valor = valor.strip()
            if valor:
                cuenta[valor] += 1
            anterior = comilla
            continue
        if not c.isspace():
            anterior = c
        i += 1
    return cuenta


def _abre_regex(texto: str, i: int, anterior: str) -> bool:
    if anterior == "" or anterior in ANTES_DE_REGEX:
        return True
    if anterior.isalnum() or anterior in "_$)]":
        # Puede ser el final de una palabra clave (`return /re/`) o una división.
        previo = texto[max(0, i - 12) : i].rstrip()
        return any(previo.endswith(p) for p in PALABRAS_ANTES_DE_REGEX)
    return False


def _fin_de_regex(texto: str, i: int) -> int:
    """Devuelve el índice siguiente al cierre de la expresión regular que abre en `i`."""
    j = i + 1
    n = len(texto)
    en_clase = False
    while j < n:
        c = texto[j]
        if c == "\\":
            j += 2
            continue
        if c == "\n":
            return i + 1  # no era una regex; se retoma justo después de la barra
        if c == "[":
            en_clase = True
        elif c == "]":
            en_clase = False
        elif c == "/" and not en_clase:
            j += 1
            while j < n and texto[j].isalpha():  # banderas: g, i, m, s, u, y
                j += 1
            return j
        j += 1
    return i + 1


def main() -> int:
    problemas = 0
    revisados = 0
    for directorio in DIRECTORIOS:
        if not directorio.is_dir():
            continue
        for minificado in sorted(directorio.rglob("*.min.js")):
            fuente = minificado.with_name(minificado.name[: -len(".min.js")] + ".js")
            if not fuente.exists():
                continue  # librería de terceros distribuida ya minificada
            revisados += 1
            rel = minificado.relative_to(RAIZ)
            a = literales(fuente.read_text(encoding="utf-8", errors="replace"))
            b = literales(minificado.read_text(encoding="utf-8", errors="replace"))
            # Solo importa lo que la fuente tiene y el .min no: es la corrección
            # que no llegaría al usuario. Lo contrario suele ser ruido del minificador.
            faltantes = a - b
            if not faltantes:
                print(f"  OK       {rel}")
                continue
            problemas += 1
            print(f"  DESFASE  {rel}  ({sum(faltantes.values())} literales sin reflejar)")
            for valor, n in list(faltantes.most_common(8)):
                print(f"      x{n}  {valor[:110]!r}")

    print(f"\n{revisados} pares revisados, {problemas} con desfase")
    if problemas:
        print("Regenera los .min afectados antes de publicar: la app sirve el .min.")
    return 1 if problemas else 0


if __name__ == "__main__":
    sys.exit(main())
