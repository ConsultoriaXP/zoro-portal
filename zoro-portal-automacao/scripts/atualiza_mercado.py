#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Atualiza os dados DINAMICOS de mercado do portal ZORO.

Fontes, tentadas nesta ordem:
  1. Status Invest  - completo (preco, P/VP, DY 12m, patrimonio, no de cotas,
                      liquidez, valor de mercado). Pode ser bloqueado quando a
                      requisicao sai de um datacenter, como os runners do GitHub.
  2. Yahoo Finance  - so o preco da cota, mas responde de qualquer lugar.

Se a fonte 1 funcionar, tudo e atualizado. Se apenas a 2 funcionar, so os precos
sao atualizados e os demais indicadores permanecem como estavam (a origem fica
registrada em D.mercado.fonte e em D.meta.data_ref_mercado).

O que este script NUNCA sobrescreve (curadoria manual / mensal):
  casas, casasParcial, casasTickers, consensoMT, consensoMatriz[].casas,
  rec.fundos[] (recomendacao, cota_target, cf_pvpa, cf_dy12m...),
  teses, news, segMes, seg12.

Uso:
    python3 scripts/atualiza_mercado.py                # atualiza os dados
    python3 scripts/atualiza_mercado.py --dry-run      # nao grava nada
    python3 scripts/atualiza_mercado.py --diagnostico  # so testa as fontes
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

BRT = timezone(timedelta(hours=-3))
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSON_PATH = os.path.join(RAIZ, "dados", "zoro.json")
JS_PATH = os.path.join(RAIZ, "dados", "zoro.js")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

SI_FIIS = ("https://statusinvest.com.br/category/advancedsearchresultpaginated"
           "?search=%7B%22my%22%3A%7B%7D%7D&orderColumn=&isAsc=&page=0&take=1000"
           "&CategoryType=2")
SI_IFIX = "https://statusinvest.com.br/indices/ifix"
YF = "https://query1.finance.yahoo.com/v8/finance/chart/{tk}.SA?interval=1d&range=5d"

avisos = []


def log(msg):
    print(msg, flush=True)


def aviso(msg):
    avisos.append(msg)
    print("AVISO: " + msg, flush=True)


def baixar(url, tentativas=2, timeout=45, referer=None):
    """GET com cabecalhos de navegador. Levanta a ultima excecao se falhar."""
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    ultimo = None
    for n in range(tentativas):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            ultimo = e
            if n + 1 < tentativas:
                time.sleep(2 * (n + 1))
    raise ultimo


# --------------------------------------------------------------- fonte 1: SI

def buscar_statusinvest():
    """ticker -> indicadores completos. Levanta excecao se a fonte falhar."""
    bruto = json.loads(baixar(
        SI_FIIS,
        referer="https://statusinvest.com.br/fundos-imobiliarios/busca-avancada"))
    lista = bruto.get("list") if isinstance(bruto, dict) else bruto
    if not isinstance(lista, list) or len(lista) < 100:
        raise RuntimeError("resposta inesperada do Status Invest (%s itens)"
                           % (len(lista) if isinstance(lista, list) else "?"))
    out = {}
    for r in lista:
        tk = (r.get("ticker") or "").strip().upper()
        if not tk:
            continue
        preco, cotas = r.get("price"), r.get("numerocotas")
        out[tk] = {
            "nome": r.get("companyname"),
            "preco": preco,
            "pvp": r.get("p_vp"),
            "dy12m": r.get("dy"),
            "vpa": r.get("valorpatrimonialcota"),
            "pl": r.get("patrimonio"),
            "cotas": cotas,
            "vm": round(preco * cotas, 2) if (preco and cotas) else None,
            "liq_diaria": r.get("liquidezmediadiaria"),
            "ult_dividendo": r.get("lastdividend"),
            "segmento": r.get("segment"),
        }
    log("Status Invest: %d fundos" % len(out))
    return out


# ------------------------------------------------------- fonte 2: Yahoo Finance

def _yahoo_um(tk):
    try:
        j = json.loads(baixar(YF.format(tk=tk), tentativas=2, timeout=25))
        res = (j.get("chart") or {}).get("result")
        if not res:
            return tk, None
        m = res[0].get("meta") or {}
        p = m.get("regularMarketPrice")
        return tk, (round(float(p), 2) if p else None)
    except Exception:
        return tk, None


def buscar_yahoo(tickers):
    """ticker -> {'preco': x}. Tolera falhas individuais."""
    tickers = sorted(set(tickers))
    out = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for tk, preco in ex.map(_yahoo_um, tickers):
            if preco:
                out[tk] = {"preco": preco}
    log("Yahoo Finance: %d de %d tickers com preco" % (len(out), len(tickers)))
    if not out:
        raise RuntimeError("Yahoo Finance nao respondeu para nenhum ticker")
    return out


# ------------------------------------------------------------------- IFIX

def num_br(txt):
    try:
        return float(txt.replace(".", "").replace(",", "."))
    except (ValueError, AttributeError):
        return None


def buscar_ifix():
    """Melhor esforco: se falhar, devolve {} e os valores anteriores ficam."""
    try:
        html = baixar(SI_IFIX, tentativas=2, timeout=30)
    except Exception as e:
        aviso("IFIX indisponivel (%s) - valor anterior mantido" % e)
        return {}
    texto = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    valores = sorted({v for v in (num_br(m.group(0))
                                  for m in re.finditer(r"\d\.\d{3},\d{2}", texto))
                      if v and 1500 <= v <= 9000})
    res = {}
    for m in re.finditer(r"\d\.\d{3},\d{2}", texto):
        v = num_br(m.group(0))
        if v and 1500 <= v <= 9000:
            res["fech"] = v
            break
    if len(valores) >= 2:
        res["min_52s"], res["max_52s"] = valores[0], valores[-1]
    m = re.search(r"(-?\d{1,2},\d{2})\s*%", texto)
    if m:
        res["var_dia_pct"] = num_br(m.group(1))
    if "fech" not in res:
        aviso("nao foi possivel ler o valor do IFIX - valor anterior mantido")
        return {}
    log("IFIX: %s (dia %s%%)" % (res["fech"], res.get("var_dia_pct")))
    return res


# ------------------------------------------------------------------ atualizacao

def universo(D):
    tks = set()
    for chave in ("ifixComp", "clubefii", "consensoMatriz"):
        tks.update((r.get("t") or "").upper() for r in D.get(chave, []))
    tks.update((r.get("ticker") or "").upper()
               for r in D.get("rec", {}).get("fundos", []))
    tks.discard("")
    return tks


def atualizar(D, fiis, ifix, fonte, completo):
    hoje = datetime.now(BRT)
    data_br = hoje.strftime("%d/%m/%Y")
    n = {"ifixComp": 0, "clubefii": 0, "consensoMatriz": 0, "rec": 0}
    faltando = []

    D["mercado"] = {
        "fonte": fonte,
        "completo": completo,
        "atualizado_em": hoje.strftime("%d/%m/%Y %H:%M") + " (BRT)",
        "fundos": {tk: fiis[tk] for tk in sorted(universo(D)) if tk in fiis},
    }

    def preco_de(tk):
        d = fiis.get((tk or "").upper())
        return d.get("preco") if d else None

    for r in D.get("ifixComp", []):
        p = preco_de(r.get("t"))
        if p:
            if r.get("preco") != p:
                n["ifixComp"] += 1
            r["preco"] = p
            if completo:
                r["vm"] = fiis[r["t"].upper()].get("vm")
        else:
            faltando.append(r.get("t"))

    for r in D.get("clubefii", []):
        p = preco_de(r.get("t"))
        if p:
            if r.get("preco") != p:
                n["clubefii"] += 1
            r["preco"] = p

    for r in D.get("consensoMatriz", []):
        p = preco_de(r.get("t"))
        if p:
            if r.get("preco") != p:
                n["consensoMatriz"] += 1
            r["preco"] = p

    for r in D.get("rec", {}).get("fundos", []):
        p = preco_de(r.get("ticker"))
        if not p:
            continue
        if r.get("preco_atual") != p:
            n["rec"] += 1
        r["preco_atual"] = p
        alvo = r.get("cota_target")
        r["pot_atual"] = int(round((alvo / p - 1) * 100)) if alvo else None

    if ifix.get("fech"):
        D.setdefault("ifix", {})
        D["ifix"]["fech"] = ifix["fech"]
        D["ifix"]["data"] = data_br
        for k in ("var_dia_pct", "max_52s", "min_52s"):
            if ifix.get(k) is not None:
                D["ifix"][k] = ifix[k]
        D["ifix"]["obs"] = ("IFIX em %s pontos no fechamento de %s"
                            % (("%.2f" % ifix["fech"]).replace(".", ","), data_br))

    D.setdefault("meta", {})
    D["meta"]["atualizado"] = data_br
    D["meta"]["data_ref_mercado"] = (
        "Precos%s: %s, %s. Research e consenso das casas: %s."
        % ("" if completo else " (somente cotacao)",
           fonte, data_br,
           D["meta"].get("data_ref_research", "curadoria manual")))
    D["precoFonte"] = {"fonte": fonte, "data": data_br}

    log("Atualizados -> ifixComp: %d | clubefii: %d | consensoMatriz: %d | rec: %d"
        % (n["ifixComp"], n["clubefii"], n["consensoMatriz"], n["rec"]))
    faltando = [t for t in faltando if t]
    if faltando:
        log("Sem cotacao (%d): %s" % (len(faltando), ", ".join(faltando)))
    return D


def gravar(D, dry_run=False):
    js = ("/* Gerado por scripts/atualiza_mercado.py - nao editar a mao. */\n"
          "var D = %s;\n" % json.dumps(D, ensure_ascii=False, separators=(",", ":")))
    if dry_run:
        log("--dry-run: nada gravado (%d bytes de JS)" % len(js))
        return
    os.makedirs(os.path.dirname(JSON_PATH), exist_ok=True)
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(D, f, ensure_ascii=False, indent=1)
    with open(JS_PATH, "w", encoding="utf-8") as f:
        f.write(js)
    log("Gravados dados/zoro.json e dados/zoro.js")


def diagnostico():
    log("=== Diagnostico de acesso as fontes (a partir deste runner) ===")
    ok = False
    for nome, fn in (("Status Invest (busca avancada)", buscar_statusinvest),
                     ("Status Invest (pagina do IFIX)", buscar_ifix),
                     ("Yahoo Finance", lambda: buscar_yahoo(["XPML11", "KNCR11", "HGLG11"]))):
        try:
            r = fn()
            log("  OK       %s -> %s itens" % (nome, len(r) if hasattr(r, "__len__") else "?"))
            ok = True
        except Exception as e:
            log("  FALHOU   %s -> %s: %s" % (nome, type(e).__name__, e))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--diagnostico", action="store_true")
    args = ap.parse_args()

    if args.diagnostico:
        return diagnostico()

    with open(JSON_PATH, encoding="utf-8") as f:
        D = json.load(f)

    fiis, fonte, completo = None, None, False
    try:
        fiis = buscar_statusinvest()
        fonte, completo = "Status Invest", True
    except Exception as e:
        aviso("Status Invest indisponivel (%s: %s) - usando o Yahoo Finance"
              % (type(e).__name__, e))
        try:
            fiis = buscar_yahoo(universo(D))
            fonte, completo = "Yahoo Finance", False
        except Exception as e2:
            log("ERRO: nenhuma fonte de cotacao respondeu.")
            log("  Status Invest: %s" % e)
            log("  Yahoo Finance: %s" % e2)
            return 1

    D = atualizar(D, fiis, buscar_ifix(), fonte, completo)
    gravar(D, args.dry_run)

    log("\nFonte usada: %s (%s)" % (fonte, "completa" if completo else "somente cotacao"))
    if avisos:
        log("%d aviso(s):" % len(avisos))
        for a in avisos:
            log("  - " + a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
