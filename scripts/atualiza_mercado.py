#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Atualiza os dados DINAMICOS de mercado do portal ZORO.

FONTES (nesta ordem)
  1. Status Invest - completo, mas bloqueia requisicoes de datacenter (nao
     responde aos runners do GitHub). Quando responde, tambem regrava
     dados/bases.json com o numero de cotas e o valor patrimonial por cota.
  2. Yahoo Finance - responde de qualquer lugar. Uma chamada por ticker traz a
     cotacao E o historico de proventos de 12 meses, o que permite calcular:
         valor de mercado = preco x numero de cotas   (cotas vem de bases.json)
         P/VP             = preco / valor patrimonial (vem de bases.json)
         DY 12m           = proventos 12m / preco     (do proprio Yahoo)
     O IFIX sai de IFIX.SA no Yahoo (nivel, variacao do dia e do mes, faixa
     de 52 semanas).

O QUE ESTE SCRIPT NUNCA SOBRESCREVE (curadoria manual / mensal)
  casas, casasParcial, casasTickers, consensoMT, consensoMatriz[].casas,
  rec.fundos[] (recomendacao, cota_target, cf_pvpa, cf_dy12m...),
  clubefii[].pvpa / y1m / y12m (sao o ranking mensal do Clube FII),
  teses, news, segMes, seg12, ifixEvo.

Os indicadores calculados no dia ficam em D.mercado.fundos[TICKER]:
  {preco, vm, pvp, dy12m, prov12m, cotas, vpa}

Uso
    python3 scripts/atualiza_mercado.py
    python3 scripts/atualiza_mercado.py --dry-run
    python3 scripts/atualiza_mercado.py --diagnostico
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

BRT = timezone(timedelta(hours=-3))
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSON_PATH = os.path.join(RAIZ, "dados", "zoro.json")
JS_PATH = os.path.join(RAIZ, "dados", "zoro.js")
BASES_PATH = os.path.join(RAIZ, "dados", "bases.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

SI_FIIS = ("https://statusinvest.com.br/category/advancedsearchresultpaginated"
           "?search=%7B%22my%22%3A%7B%7D%7D&orderColumn=&isAsc=&page=0&take=1000"
           "&CategoryType=2")
SI_IFIX = "https://statusinvest.com.br/indices/ifix"
YF = ("https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
      "?interval=1d&range=1y&events=div")

avisos = []


def log(msg):
    print(msg, flush=True)


def aviso(msg):
    avisos.append(msg)
    print("AVISO: " + msg, flush=True)


def baixar(url, tentativas=2, timeout=45, referer=None):
    headers = {"User-Agent": UA,
               "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
               "Accept-Language": "pt-BR,pt;q=0.9"}
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


def r2(x):
    return None if x is None else round(float(x), 2)


# ------------------------------------------------------------ bases semi-fixas

def ler_bases():
    try:
        with open(BASES_PATH, encoding="utf-8") as f:
            b = json.load(f)
        return b.get("fundos", {}), b
    except Exception as e:
        aviso("dados/bases.json nao pudo ser lido (%s) - sem valor de mercado "
              "nem P/VP calculados" % e)
        return {}, {}


def gravar_bases(fiis, dry_run):
    fundos = {tk: {"c": d.get("cotas"), "v": r2(d.get("vpa"))}
              for tk, d in sorted(fiis.items())
              if d.get("cotas") or d.get("vpa")}
    if not fundos or dry_run:
        return
    b = {"fonte": "Status Invest",
         "data": datetime.now(BRT).strftime("%d/%m/%Y"),
         "nota": ("Numero de cotas (c) e valor patrimonial por cota (v). "
                  "Campos semi-estaticos: mudam em nova emissao ou no informe "
                  "mensal. Usados para calcular valor de mercado (preco x c) e "
                  "P/VP (preco / v) quando a fonte diaria devolve apenas a cotacao."),
         "fundos": fundos}
    with open(BASES_PATH, "w", encoding="utf-8") as f:
        json.dump(b, f, ensure_ascii=False, indent=1)
    log("dados/bases.json regravado (%d fundos)" % len(fundos))


# --------------------------------------------------------------- fonte 1: SI

def buscar_statusinvest():
    bruto = json.loads(baixar(
        SI_FIIS,
        referer="https://statusinvest.com.br/fundos-imobiliarios/busca-avancada"))
    lista = bruto.get("list") if isinstance(bruto, dict) else bruto
    if not isinstance(lista, list) or len(lista) < 100:
        raise RuntimeError("resposta inesperada do Status Invest")
    out = {}
    for r in lista:
        tk = (r.get("ticker") or "").strip().upper()
        if not tk:
            continue
        preco, cotas, vpa, dy = (r.get("price"), r.get("numerocotas"),
                                 r.get("valorpatrimonialcota"), r.get("dy"))
        out[tk] = {
            "preco": r2(preco),
            "pvp": r.get("p_vp"),
            "dy12m": dy,
            "prov12m": r2(dy * preco / 100) if (dy and preco) else None,
            "vpa": r2(vpa),
            "cotas": cotas,
            "vm": r2(preco * cotas) if (preco and cotas) else None,
            "pl": r.get("patrimonio"),
            "liq_diaria": r.get("liquidezmediadiaria"),
            "segmento": r.get("segment"),
        }
    log("Status Invest: %d fundos" % len(out))
    return out


# ------------------------------------------------------- fonte 2: Yahoo Finance

def _yahoo_um(tk):
    """Devolve (ticker, {'preco':x,'prov12m':y}) ou (ticker, None)."""
    try:
        j = json.loads(baixar(YF.format(sym=tk + ".SA"), tentativas=2, timeout=30))
        res = (j.get("chart") or {}).get("result")
        if not res:
            return tk, None
        r = res[0]
        m = r.get("meta") or {}
        preco = m.get("regularMarketPrice")
        if not preco:  # fundo sem negocio hoje: usa o ultimo fechamento valido
            try:
                fechs = [c for c in r["indicators"]["quote"][0]["close"]
                         if c is not None and c > 0]
                preco = fechs[-1] if fechs else None
            except Exception:
                preco = None
        if not preco:
            return tk, None
        corte = time.time() - 370 * 86400
        divs = ((r.get("events") or {}).get("dividends") or {}).values()
        prov = sum(d.get("amount") or 0 for d in divs
                   if (d.get("date") or 0) >= corte)
        return tk, {"preco": r2(preco), "prov12m": r2(prov) if prov else None}
    except Exception:
        return tk, None


def buscar_yahoo(tickers):
    tickers = sorted({t for t in tickers if t})
    out = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for tk, d in ex.map(_yahoo_um, tickers):
            if d:
                out[tk] = d
    log("Yahoo Finance: %d de %d tickers com cotacao" % (len(out), len(tickers)))
    if not out:
        raise RuntimeError("Yahoo Finance nao respondeu para nenhum ticker")
    return out


def completar_com_bases(fiis, bases):
    """Calcula vm e pvp a partir do preco do dia e das bases semi-fixas."""
    n_vm = n_pvp = 0
    for tk, d in fiis.items():
        b = bases.get(tk) or {}
        cotas, vpa, preco = b.get("c"), b.get("v"), d.get("preco")
        d.setdefault("cotas", cotas)
        d.setdefault("vpa", vpa)
        if preco and cotas:
            d["vm"] = r2(preco * cotas)
            n_vm += 1
        if preco and vpa and vpa >= 1:
            d["pvp"] = round(preco / vpa, 4)
            n_pvp += 1
        if preco and d.get("prov12m"):
            d["dy12m"] = round(d["prov12m"] / preco * 100, 2)
    log("Calculados a partir das bases: %d valores de mercado, %d P/VP" % (n_vm, n_pvp))
    return fiis


# ------------------------------------------------------------------- IFIX

def num_br(txt):
    try:
        return float(txt.replace(".", "").replace(",", "."))
    except (ValueError, AttributeError):
        return None


def ifix_yahoo():
    j = json.loads(baixar(YF.format(sym="IFIX.SA").replace("&events=div", ""),
                          tentativas=2, timeout=30))
    res = (j.get("chart") or {}).get("result")
    if not res:
        raise RuntimeError("IFIX.SA sem dados no Yahoo")
    r = res[0]
    m = r.get("meta") or {}
    ts = r.get("timestamp") or []
    fechs = (r["indicators"]["quote"][0].get("close") or [])
    serie = [(t, c) for t, c in zip(ts, fechs) if c]
    if not serie:
        raise RuntimeError("serie do IFIX vazia")
    nivel = m.get("regularMarketPrice") or serie[-1][1]
    out = {"fech": r2(nivel)}
    ant = m.get("chartPreviousClose") or (serie[-2][1] if len(serie) > 1 else None)
    if ant:
        out["var_dia_pct"] = round((nivel / ant - 1) * 100, 2)
    valores = [c for _, c in serie]
    out["max_52s"], out["min_52s"] = r2(max(valores)), r2(min(valores))
    # variacao no mes: contra o ultimo fechamento do mes anterior
    mes_atual = datetime.fromtimestamp(serie[-1][0], BRT).month
    anterior = [c for t, c in serie
                if datetime.fromtimestamp(t, BRT).month != mes_atual]
    if anterior:
        out["var_mes_pct"] = round((nivel / anterior[-1] - 1) * 100, 2)
    return out


def ifix_statusinvest():
    html = baixar(SI_IFIX, tentativas=2, timeout=30)
    texto = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    vals = sorted({v for v in (num_br(mm.group(0))
                               for mm in re.finditer(r"\d\.\d{3},\d{2}", texto))
                   if v and 1500 <= v <= 9000})
    primeiro = next((num_br(mm.group(0)) for mm in re.finditer(r"\d\.\d{3},\d{2}", texto)
                     if num_br(mm.group(0)) and 1500 <= num_br(mm.group(0)) <= 9000), None)
    if not primeiro:
        raise RuntimeError("nao achei o valor do IFIX na pagina")
    out = {"fech": primeiro}
    if len(vals) >= 2:
        out["min_52s"], out["max_52s"] = vals[0], vals[-1]
    mm = re.search(r"(-?\d{1,2},\d{2})\s*%", texto)
    if mm:
        out["var_dia_pct"] = num_br(mm.group(1))
    return out


def buscar_ifix():
    for nome, fn in (("Yahoo (IFIX.SA)", ifix_yahoo),
                     ("Status Invest", ifix_statusinvest)):
        try:
            r = fn()
            log("IFIX por %s: %s (dia %s%%, mes %s%%)"
                % (nome, r.get("fech"), r.get("var_dia_pct"), r.get("var_mes_pct")))
            r["fonte"] = nome
            return r
        except Exception as e:
            log("  IFIX via %s falhou: %s" % (nome, e))
    aviso("nenhuma fonte de IFIX respondeu - valor anterior mantido")
    return {}


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
    sem = []

    uni = sorted(universo(D))
    D["mercado"] = {
        "fonte": fonte,
        "completo": completo,
        "atualizado_em": hoje.strftime("%d/%m/%Y %H:%M") + " (BRT)",
        "sem_cotacao": [tk for tk in uni if tk not in fiis],
        "fundos": {tk: fiis[tk] for tk in uni if tk in fiis},
    }

    def dado(tk):
        return fiis.get((tk or "").upper())

    for r in D.get("ifixComp", []):
        d = dado(r.get("t"))
        if not d or not d.get("preco"):
            sem.append(r.get("t"))
            continue
        if r.get("preco") != d["preco"]:
            n["ifixComp"] += 1
        r["preco"] = d["preco"]
        if d.get("vm"):
            r["vm"] = d["vm"]

    for r in D.get("clubefii", []):
        d = dado(r.get("t"))
        if d and d.get("preco"):
            if r.get("preco") != d["preco"]:
                n["clubefii"] += 1
            r["preco"] = d["preco"]

    for r in D.get("consensoMatriz", []):
        d = dado(r.get("t"))
        if d and d.get("preco"):
            if r.get("preco") != d["preco"]:
                n["consensoMatriz"] += 1
            r["preco"] = d["preco"]

    for r in D.get("rec", {}).get("fundos", []):
        d = dado(r.get("ticker"))
        if not d or not d.get("preco"):
            continue
        if r.get("preco_atual") != d["preco"]:
            n["rec"] += 1
        r["preco_atual"] = d["preco"]
        alvo = r.get("cota_target")
        r["pot_atual"] = int(round((alvo / d["preco"] - 1) * 100)) if alvo else None

    if ifix.get("fech"):
        D.setdefault("ifix", {})
        D["ifix"]["fech"] = ifix["fech"]
        D["ifix"]["data"] = data_br
        for k in ("var_dia_pct", "var_mes_pct", "max_52s", "min_52s"):
            if ifix.get(k) is not None:
                D["ifix"][k] = ifix[k]
        D["ifix"]["obs"] = ("IFIX em %s pontos no fechamento de %s (fonte: %s)"
                            % (("%.2f" % ifix["fech"]).replace(".", ","),
                               data_br, ifix.get("fonte", fonte)))

    D.setdefault("meta", {})
    D["meta"]["atualizado"] = data_br
    D["meta"]["data_ref_mercado"] = (
        "Cotacoes, valor de mercado, P/VP e DY 12m: %s, %s. "
        "Ranking Clube FII e consenso das casas: %s."
        % (fonte, data_br, D["meta"].get("data_ref_research", "curadoria manual")))
    D["precoFonte"] = {"fonte": fonte, "data": data_br}

    log("Atualizados -> ifixComp: %d | clubefii: %d | consensoMatriz: %d | rec: %d"
        % (n["ifixComp"], n["clubefii"], n["consensoMatriz"], n["rec"]))
    sem = sorted({t for t in sem if t})
    if sem:
        log("Sem cotacao em nenhuma fonte (%d): %s" % (len(sem), ", ".join(sem)))
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
    log("=== Diagnostico de acesso as fontes ===")
    ok = False
    testes = (("Status Invest (busca avancada)", buscar_statusinvest),
              ("IFIX via Yahoo (IFIX.SA)", ifix_yahoo),
              ("IFIX via Status Invest", ifix_statusinvest),
              ("Yahoo Finance (3 tickers)",
               lambda: buscar_yahoo(["XPML11", "KNCR11", "HGLG11"])))
    for nome, fn in testes:
        try:
            r = fn()
            log("  OK       %s -> %s" % (nome, len(r) if hasattr(r, "__len__") else r))
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
    bases, _ = ler_bases()

    try:
        fiis = buscar_statusinvest()
        fonte, completo = "Status Invest", True
        gravar_bases(fiis, args.dry_run)
    except Exception as e:
        aviso("Status Invest indisponivel (%s: %s) - usando o Yahoo Finance"
              % (type(e).__name__, e))
        try:
            fiis = completar_com_bases(buscar_yahoo(universo(D)), bases)
            fonte, completo = "Yahoo Finance", False
        except Exception as e2:
            log("ERRO: nenhuma fonte de cotacao respondeu.")
            log("  Status Invest: %s" % e)
            log("  Yahoo Finance: %s" % e2)
            return 1

    D = atualizar(D, fiis, buscar_ifix(), fonte, completo)
    gravar(D, args.dry_run)

    log("\nFonte das cotacoes: %s" % fonte)
    if avisos:
        log("%d aviso(s):" % len(avisos))
        for a in avisos:
            log("  - " + a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
