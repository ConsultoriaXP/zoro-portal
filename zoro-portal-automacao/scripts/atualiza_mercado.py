#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Atualiza os dados DINAMICOS de mercado do portal ZORO.

Fonte: Status Invest (endpoint publico de busca avancada de FIIs) + pagina do IFIX.

O que este script ATUALIZA (diariamente):
  - mercado          : bloco novo, ticker -> preco, p/vp, dy12m, valor de mercado,
                       patrimonio liquido, nº de cotas, liquidez media diaria
  - ifixComp[].preco , ifixComp[].vm
  - clubefii[].preco
  - consensoMatriz[].preco
  - rec.fundos[].preco_atual , rec.fundos[].pot_atual
  - ifix.fech / ifix.data / ifix.var_dia_pct / ifix.max_52s / ifix.min_52s
  - meta.atualizado , meta.data_ref_mercado , precoFonte

O que este script NAO TOCA (curadoria manual / mensal):
  - casas, casasParcial, casasTickers, consensoMT, consensoMatriz[].casas
  - rec.fundos[] (recomendacao, cota_target, cf_pvpa, cf_dy12m, ...)
  - teses, news, segMes, seg12

Uso:
    python3 scripts/atualiza_mercado.py           # atualiza dados/zoro.json e dados/zoro.js
    python3 scripts/atualiza_mercado.py --dry-run # so mostra o que mudaria
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BRT = timezone(timedelta(hours=-3))
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSON_PATH = os.path.join(RAIZ, "dados", "zoro.json")
JS_PATH = os.path.join(RAIZ, "dados", "zoro.js")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
SI_FIIS = (
    "https://statusinvest.com.br/category/advancedsearchresultpaginated"
    "?search=%7B%22my%22%3A%7B%7D%7D&orderColumn=&isAsc=&page=0&take=1000&CategoryType=2"
)
SI_IFIX = "https://statusinvest.com.br/indices/ifix"

avisos = []


def log(msg):
    print(msg, flush=True)


def aviso(msg):
    avisos.append(msg)
    print("AVISO: " + msg, flush=True)


def baixar(url, tentativas=4):
    """GET com User-Agent de navegador e retry exponencial."""
    ultimo = None
    for n in range(tentativas):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": UA,
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
                "Accept-Language": "pt-BR,pt;q=0.9",
                "Referer": "https://statusinvest.com.br/fundos-imobiliarios/busca-avancada",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            ultimo = e
            espera = 3 * (2 ** n)
            log("  tentativa %d/%d falhou (%s); aguardando %ds" % (n + 1, tentativas, e, espera))
            time.sleep(espera)
    raise RuntimeError("nao foi possivel baixar %s: %s" % (url, ultimo))


# ---------------------------------------------------------------- Status Invest

def buscar_fiis():
    """Retorna dict ticker -> indicadores do Status Invest."""
    bruto = json.loads(baixar(SI_FIIS))
    lista = bruto.get("list") if isinstance(bruto, dict) else bruto
    if not isinstance(lista, list) or not lista:
        raise RuntimeError("resposta do Status Invest sem a lista de fundos")

    out = {}
    for r in lista:
        tk = (r.get("ticker") or "").strip().upper()
        if not tk:
            continue
        preco = r.get("price")
        cotas = r.get("numerocotas")
        vm = round(preco * cotas, 2) if (preco and cotas) else None
        out[tk] = {
            "nome": r.get("companyname"),
            "preco": preco,
            "pvp": r.get("p_vp"),
            "dy12m": r.get("dy"),
            "vpa": r.get("valorpatrimonialcota"),
            "pl": r.get("patrimonio"),
            "cotas": cotas,
            "vm": vm,
            "liq_diaria": r.get("liquidezmediadiaria"),
            "ult_dividendo": r.get("lastdividend"),
            "segmento": r.get("segment"),
        }
    log("Status Invest: %d fundos recebidos" % len(out))
    if len(out) < 300:
        aviso("apenas %d fundos retornados (esperado ~600) - verificar o endpoint" % len(out))
    return out


def num_br(txt):
    """'3.731,62' -> 3731.62 ; '-0,45' -> -0.45"""
    try:
        return float(txt.replace(".", "").replace(",", "."))
    except (ValueError, AttributeError):
        return None


def buscar_ifix():
    """Extrai valor, variacao do dia e faixa de 52 semanas da pagina do IFIX."""
    html = baixar(SI_IFIX)
    texto = re.sub(r"<[^>]+>", " ", html)
    texto = re.sub(r"\s+", " ", texto)

    res = {}
    # valor do indice: primeiro numero no formato X.XXX,XX dentro da faixa plausivel
    for m in re.finditer(r"\d\.\d{3},\d{2}", texto):
        v = num_br(m.group(0))
        if v and 1500 <= v <= 9000:
            res["fech"] = v
            break
    # faixa de 52 semanas (maior e menor valores plausiveis da pagina)
    candidatos = sorted(
        {v for v in (num_br(m.group(0)) for m in re.finditer(r"\d\.\d{3},\d{2}", texto))
         if v and 1500 <= v <= 9000}
    )
    if len(candidatos) >= 2:
        res["min_52s"] = candidatos[0]
        res["max_52s"] = candidatos[-1]
    # variacao do dia: primeiro percentual com sinal
    m = re.search(r"(-?\d{1,2},\d{2})\s*%", texto)
    if m:
        res["var_dia_pct"] = num_br(m.group(1))

    if "fech" not in res:
        aviso("nao foi possivel ler o valor do IFIX na pagina - valores anteriores mantidos")
    else:
        log("IFIX: %s (dia %s%%)" % (res["fech"], res.get("var_dia_pct")))
    return res


# ------------------------------------------------------------------ atualizacao

def atualizar(D, fiis, ifix):
    hoje = datetime.now(BRT)
    data_br = hoje.strftime("%d/%m/%Y")
    trocas = {"ifixComp": 0, "clubefii": 0, "consensoMatriz": 0, "rec": 0, "sem_dado": []}

    # bloco novo com tudo que vem do Status Invest, indexado por ticker
    usados = set()
    for chave, campo in (("ifixComp", "t"), ("clubefii", "t"), ("consensoMatriz", "t")):
        usados.update((r.get(campo) or "").upper() for r in D.get(chave, []))
    usados.update((r.get("ticker") or "").upper() for r in D.get("rec", {}).get("fundos", []))
    usados.discard("")
    D["mercado"] = {
        "fonte": "Status Invest",
        "fonte_url": "https://statusinvest.com.br/fundos-imobiliarios/busca-avancada",
        "atualizado_em": hoje.strftime("%d/%m/%Y %H:%M") + " (BRT)",
        "fundos": {tk: fiis[tk] for tk in sorted(usados) if tk in fiis},
    }

    def preco_de(tk):
        d = fiis.get((tk or "").upper())
        return d["preco"] if d and d.get("preco") else None

    # composicao do IFIX: preco + valor de mercado
    for r in D.get("ifixComp", []):
        p = preco_de(r.get("t"))
        if p:
            if r.get("preco") != p:
                trocas["ifixComp"] += 1
            r["preco"] = p
            d = fiis[r["t"].upper()]
            r["vm"] = d.get("vm")
        else:
            trocas["sem_dado"].append(r.get("t"))

    # aba Clube FII: so o preco e diario; pvpa/y1m/y12m seguem a curadoria mensal
    for r in D.get("clubefii", []):
        p = preco_de(r.get("t"))
        if p:
            if r.get("preco") != p:
                trocas["clubefii"] += 1
            r["preco"] = p

    # matriz do consenso das casas: so o preco
    for r in D.get("consensoMatriz", []):
        p = preco_de(r.get("t"))
        if p:
            if r.get("preco") != p:
                trocas["consensoMatriz"] += 1
            r["preco"] = p

    # recomendacoes XP: preco atual e potencial recalculado contra o preco-alvo
    for r in D.get("rec", {}).get("fundos", []):
        p = preco_de(r.get("ticker"))
        if not p:
            continue
        if r.get("preco_atual") != p:
            trocas["rec"] += 1
        r["preco_atual"] = p
        alvo = r.get("cota_target")
        r["pot_atual"] = int(round((alvo / p - 1) * 100)) if alvo else None

    # IFIX
    if ifix.get("fech"):
        D.setdefault("ifix", {})
        D["ifix"]["fech"] = ifix["fech"]
        D["ifix"]["data"] = data_br
        for k in ("var_dia_pct", "max_52s", "min_52s"):
            if ifix.get(k) is not None:
                D["ifix"][k] = ifix[k]
        D["ifix"]["obs"] = "IFIX em %s pontos no fechamento de %s (fonte: Status Invest)" % (
            ("%.2f" % ifix["fech"]).replace(".", ","),
            data_br,
        )

    # metadados
    D.setdefault("meta", {})
    D["meta"]["atualizado"] = data_br
    D["meta"]["data_ref_mercado"] = (
        "Precos, P/VP, DY e valor de mercado: Status Invest, %s. "
        "Research e consenso das casas: %s."
        % (data_br, D["meta"].get("data_ref_research", "curadoria manual"))
    )
    D["precoFonte"] = {"fonte": "StatusInvest", "data": data_br}

    log(
        "Atualizados -> ifixComp: %d | clubefii: %d | consensoMatriz: %d | rec: %d"
        % (trocas["ifixComp"], trocas["clubefii"], trocas["consensoMatriz"], trocas["rec"])
    )
    sem = [t for t in trocas["sem_dado"] if t]
    if sem:
        log("Sem preco no Status Invest (%d): %s" % (len(sem), ", ".join(sem)))
    return D


def gravar(D, dry_run=False):
    js = "/* Gerado por scripts/atualiza_mercado.py - nao editar a mao. */\nvar D = %s;\n" % (
        json.dumps(D, ensure_ascii=False, separators=(",", ":"))
    )
    if dry_run:
        log("--dry-run: nada gravado (%d bytes de JS gerados)" % len(js))
        return
    os.makedirs(os.path.dirname(JSON_PATH), exist_ok=True)
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(D, f, ensure_ascii=False, indent=1)
    with open(JS_PATH, "w", encoding="utf-8") as f:
        f.write(js)
    log("Gravados dados/zoro.json e dados/zoro.js")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(JSON_PATH, encoding="utf-8") as f:
        D = json.load(f)

    fiis = buscar_fiis()
    try:
        ifix = buscar_ifix()
    except Exception as e:  # noqa: BLE001
        aviso("falha ao ler o IFIX (%s) - valores anteriores mantidos" % e)
        ifix = {}

    D = atualizar(D, fiis, ifix)
    gravar(D, args.dry_run)

    if avisos:
        log("\n%d aviso(s):" % len(avisos))
        for a in avisos:
            log("  - " + a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
