"""Busca os dados de Meta Ads da conta ABPI (Bora pra Indaiatuba) no Windsor.ai e grava em metrics.json.

Roda no GitHub Actions (ver .github/workflows/atualizar-dados.yml), de hora em hora.
A chave da API vem do secret WINDSOR_API_KEY — nunca coloque a chave no código.

O painel da ABPI é organizado por IMÓVEL, não por campanha: o código do imóvel
(ex.: CA00230, AP00035) é o nome do criativo no Meta. Um imóvel está "no ar" quando
tem pelo menos um anúncio com status efetivo ACTIVE (anúncio, conjunto e campanha ativos).

Formato de saída (lido pelo index.html):
{
  "updatedAt": "2026-11-05T14:07:00Z",            # UTC
  "today": "2026-11-05",                           # data de hoje em São Paulo
  "daily": {"AAAA-MM-DD": [spend, impr, clicks, leads]},      # conta inteira
  "props": {"<código>": {
      "live": true|false,                          # tem anúncio ativo agora
      "price": "R$ 1.900.000",                     # tirado do nome da campanha "[CÓDIGO] - R$ ...", se houver
      "daily": {"AAAA-MM-DD": [spend, impr, clicks, leads]},
      "videos": [{"thumb", "preview", "ig"}]       # criativos distintos (ativos primeiro)
  }},
  "reach": {"last7"|"prev7"|"month": n},           # alcance consolidado da conta (não somar dia a dia)
  "engagement": {"last7"|"month": {"spend", "impr", "reach", "videoViews"}}
}
"""
import glob
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

ACCOUNT_ID = "648023391646107"  # [LUZIAC] ABPI - BORA PRA INDAIATUBA (Meta Ads)
# Lead = "Leads (formulário)" do Gerenciador de Anúncios. NÃO usar actions_lead
# (soma os leads do pixel do site e não bate com o Gerenciador).
LEAD_FIELD = "actions_onsite_conversion_lead_grouped"
ENGAGEMENT_CAMPAIGN = "[LUZIAC] Engajamento (engajamento)"
START_DATE = "2026-01-01"       # primeiro dia da série diária
PROP_DAYS = 120                  # histórico por imóvel (dias)
MAX_VIDEOS = 3                   # criativos guardados por imóvel
CODE_RE = re.compile(r"\b([A-Z]{2}\d{4,6})\b")
PRICE_RE = re.compile(r"\[([A-Z]{2}\d{4,6})\]\s*-?\s*(R\$\s*[\d.]+)")
TZ = timezone(timedelta(hours=-3))  # America/Sao_Paulo
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
OUT = os.path.join(ROOT, "metrics.json")
ADS_DIR = os.path.join(ROOT, "assets", "ads")


class WindsorError(Exception):
    pass


def windsor(api_key, fields, date_from, date_to):
    params = urllib.parse.urlencode({
        "api_key": api_key,
        "date_from": date_from,
        "date_to": date_to,
        "fields": ",".join(fields),
        "select_accounts": ACCOUNT_ID,
    })
    url = f"https://connectors.windsor.ai/facebook?{params}"
    try:
        with urllib.request.urlopen(url, timeout=300) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:800]
        raise WindsorError(f"HTTP {e.code}: {body}")
    except Exception as e:  # rede, timeout, JSON inválido
        raise WindsorError(f"{type(e).__name__}: {e}"[:800])
    rows = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise WindsorError(f"resposta inesperada: {str(payload)[:500]}")
    return rows


def n(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def code_of(name):
    m = CODE_RE.search((name or "").upper())
    return m.group(1) if m else None


def save_thumb(url, key):
    """Baixa a miniatura do criativo para assets/ads (o link do Meta expira em poucos dias)."""
    if not url:
        return None
    name = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    existing = glob.glob(os.path.join(ADS_DIR, name + ".*"))
    if existing:
        return "assets/ads/" + os.path.basename(existing[0])
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (painel)"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            data = resp.read()
    except Exception as e:
        print(f"::warning::miniatura não baixada ({key}): {type(e).__name__}")
        return None
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(ctype, "jpg")
    os.makedirs(ADS_DIR, exist_ok=True)
    fn = f"{name}.{ext}"
    with open(os.path.join(ADS_DIR, fn), "wb") as f:
        f.write(data)
    return "assets/ads/" + fn


def main() -> int:
    api_key = (os.environ.get("WINDSOR_API_KEY") or "").strip()
    if not api_key:
        print("::error::WINDSOR_API_KEY não definido")
        return 1

    today = datetime.now(TZ).date()
    yesterday = today - timedelta(days=1)
    last7 = ((today - timedelta(days=7)).isoformat(), yesterday.isoformat())
    prev7 = ((today - timedelta(days=14)).isoformat(), (today - timedelta(days=8)).isoformat())
    month = (yesterday.replace(day=1).isoformat(), yesterday.isoformat())
    today_s = today.isoformat()
    prop_from = (today - timedelta(days=PROP_DAYS)).isoformat()

    try:
        daily_rows = windsor(api_key, ["date", "spend", "impressions", "link_clicks", LEAD_FIELD],
                             START_DATE, today_s)
        ad_daily = windsor(api_key, ["date", "ad_name", "spend", "impressions", "link_clicks", LEAD_FIELD],
                           prop_from, today_s)
        # status atual e criativos (30 dias para pegar também anúncios ativos com pouca entrega)
        ad_info = windsor(api_key, ["ad_id", "ad_name", "ad_effective_status", "campaign", "spend",
                                    "thumbnail_url", "ad_preview_shareable_link", "instagram_permalink_url"],
                          (today - timedelta(days=30)).isoformat(), today_s)
        camp_names = windsor(api_key, ["campaign"], START_DATE, today_s)

        reach = {}
        for key, (frm, to) in (("last7", last7), ("prev7", prev7), ("month", month)):
            rows = windsor(api_key, ["account_name", "reach"], frm, to)
            reach[key] = int(sum(n(r.get("reach")) for r in rows))

        engagement = {}
        for key, (frm, to) in (("last7", last7), ("month", month)):
            rows = windsor(api_key, ["campaign", "spend", "impressions", "reach", "actions_video_view"], frm, to)
            rows = [r for r in rows if r.get("campaign") == ENGAGEMENT_CAMPAIGN]
            engagement[key] = {
                "spend": round(sum(n(r.get("spend")) for r in rows), 2),
                "impr": int(sum(n(r.get("impressions")) for r in rows)),
                "reach": int(sum(n(r.get("reach")) for r in rows)),  # uma linha só (campanha no período)
                "videoViews": int(sum(n(r.get("actions_video_view")) for r in rows)),
            }
    except WindsorError as e:
        print(f"::error::ERRO Windsor {e}")
        return 1

    if not daily_rows:
        # Não sobrescreve dados bons com um retorno vazio (falha temporária da API).
        print("Windsor retornou 0 linhas; mantendo metrics.json anterior")
        return 0

    def vec(r):
        return [n(r.get("spend")), int(n(r.get("impressions"))), int(n(r.get("link_clicks"))), int(n(r.get(LEAD_FIELD)))]

    def addv(a, b):
        for i in range(4):
            a[i] += b[i]

    def clean(days):
        return {d: [round(v[0], 2), v[1], v[2], v[3]] for d, v in sorted(days.items()) if any(v)}

    # ---- conta ----
    acc = defaultdict(lambda: [0.0, 0, 0, 0])
    for r in daily_rows:
        if r.get("date"):
            addv(acc[r["date"]], vec(r))

    # ---- por imóvel (código no nome do criativo) ----
    pdaily = defaultdict(lambda: defaultdict(lambda: [0.0, 0, 0, 0]))
    for r in ad_daily:
        c, d = code_of(r.get("ad_name")), r.get("date")
        if c and d:
            addv(pdaily[c][d], vec(r))

    prices = {}
    for r in camp_names:
        m = PRICE_RE.search(r.get("campaign") or "")
        if m:
            prices[m.group(1)] = m.group(2).replace("R$", "R$ ").replace("  ", " ")

    live, creatives = defaultdict(bool), defaultdict(dict)
    for r in ad_info:
        c = code_of(r.get("ad_name"))
        if not c:
            continue
        active = r.get("ad_effective_status") == "ACTIVE"
        live[c] = live[c] or active
        thumb = r.get("thumbnail_url")
        # agrupa o mesmo criativo usado em vários conjuntos pela miniatura (sem os parâmetros da URL)
        key = (thumb or "").split("?")[0] or r.get("ad_id")
        cr = creatives[c].setdefault(key, {"active": False, "spend": 0.0, "thumb": None, "preview": None,
                                           "ig": None, "id": r.get("ad_id"), "_video": False})
        cr["active"] = cr["active"] or active
        cr["spend"] += n(r.get("spend"))
        cr["thumb"] = cr["thumb"] or thumb
        cr["preview"] = cr["preview"] or r.get("ad_preview_shareable_link")
        cr["ig"] = cr["ig"] or r.get("instagram_permalink_url")
        cr["_video"] = cr["_video"] or ("t15.5256-10" in (thumb or ""))  # miniatura de vídeo do Meta

    keep_files = set()
    props = {}
    for c in sorted(set(pdaily) | set(live)):
        crs = sorted(creatives.get(c, {}).values(), key=lambda x: (not x["active"], not x["_video"], -x["spend"]))
        vids = []
        for cr in crs[:MAX_VIDEOS]:
            t = save_thumb(cr["thumb"], cr["id"] or c)
            if t:
                keep_files.add(os.path.basename(t))
            vids.append({"thumb": t, "preview": cr["preview"], "ig": cr["ig"], "active": cr["active"]})
        props[c] = {"live": bool(live.get(c)), "price": prices.get(c), "daily": clean(pdaily.get(c, {})),
                    "videos": vids}

    for f in glob.glob(os.path.join(ADS_DIR, "*")):
        if os.path.basename(f) not in keep_files:
            os.remove(f)

    out = {
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "today": today_s,
        "daily": clean(acc),
        "props": props,
        "reach": reach,
        "engagement": engagement,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    n_live = sum(1 for p in props.values() if p["live"])
    print(f"{len(daily_rows)} dias na conta, {len(props)} imóveis ({n_live} no ar) em metrics.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
