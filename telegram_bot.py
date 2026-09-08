"""Alertas por Telegram (texto plano, sin HTML)."""

import json
import requests
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def send_message(text: str) -> bool:
    cfg = load_config()
    token, chat_id = cfg.get("telegram_token", ""), cfg.get("telegram_chat_id", "")
    if not token or not chat_id or token.startswith("TU_"):
        print("Telegram no configurado")
        return False
    try:
        # Telegram limita a 4096 caracteres
        for chunk in [text[i:i + 3900] for i in range(0, len(text), 3900)]:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": chat_id, "text": chunk}, timeout=10)
            r.raise_for_status()
        return True
    except Exception as e:
        print(f"Error Telegram: {e}")
        return False


def _fp(p):
    if p is None:
        return "—"
    if abs(p) >= 1000:
        return f"${p:,.0f}"
    if abs(p) >= 10:
        return f"${p:,.2f}"
    return f"${p:,.4f}"


def _levels_block(r: dict) -> list:
    lv = r.get("levels", {})
    lines = ["NIVELES:"]
    for x in lv.get("resistances", [])[:3][::-1]:
        lines.append(f"  R {_fp(x['price'])}  ({x['label']}, {x['distance_pct']:+.1f}%)  {x['sources'][0] if x['sources'] else ''}")
    lines.append(f"  >> precio {_fp(r['price'])}  [{r.get('position', '')}]")
    for x in lv.get("supports", [])[:3]:
        lines.append(f"  S {_fp(x['price'])}  ({x['label']}, {x['distance_pct']:+.1f}%)  {x['sources'][0] if x['sources'] else ''}")
    return lines


def _trend_block(r: dict) -> list:
    t = r.get("trends", {})
    order = ["1w", "1d", "4h", "1h"]
    return ["TENDENCIA: " + " | ".join(f"{tf.upper()} {t[tf]['label']} ({t[tf]['score']:+d})" for tf in order if tf in t)]


def format_signal_message(r: dict) -> str:
    plan = r.get("plan") or {}
    name = r.get("ticker") or r["name"]
    lines = [
        f"=== {r['signal']} {name} — {r.get('setup', '')} ===",
        f"Confianza: {r['confidence']} | Regimen: {r['regime']} (sesgo {r['bias']:+d})",
        f"Accion: {r['action']}",
        "",
    ] + _trend_block(r) + [""] + _levels_block(r)
    if r.get("reasons"):
        lines += ["", "POR QUE:"] + [f"  * {x}" for x in r["reasons"]]
    if r.get("warnings"):
        lines += ["", "CUIDADO:"] + [f"  * {x}" for x in r["warnings"]]
    if plan:
        lines += ["", "──────────────────",
                  f"PLAN ({plan['direction']})",
                  f"  Entrada: {_fp(plan['entry'])}",
                  f"  Stop:    {_fp(plan['stop'])}  (riesgo {plan['risk_pct']}%, detras de: {plan.get('stop_basis', '')})",
                  f"  TP1:     {_fp(plan['tp1'])}  (R/R 1:{plan['rr_tp1']})",
                  f"  TP2:     {_fp(plan['tp2'])}  (R/R 1:{plan['rr_tp2']})",
                  f"  TP3:     {_fp(plan['tp3'])}",
                  f"  Tamano:  {_fp(plan['position_usd'])} ({plan['units']} {name}) arriesgando {_fp(plan['risk_usd'])}",
                  "──────────────────"]
    f = r.get("funding") or {}
    if f.get("available"):
        lines.append(f"Funding {f['rate']:+.3f}% ({f['sentiment']}) [{f['source']}]")
    lines += ["", r.get("summary", ""), "", r.get("updated_at", "")]
    return "\n".join(lines)


def format_action_change_message(r: dict, prev_action: str) -> str:
    name = r.get("ticker") or r["name"]
    lines = [f"=== CAMBIO DE LECTURA — {name} ===",
             f"Precio: {_fp(r['price'])} | Regimen: {r['regime']} (sesgo {r['bias']:+d})",
             f"ANTES: {prev_action}",
             f"AHORA: {r['action']}",
             "", r.get("action_detail", ""), ""] + _trend_block(r) + [""] + _levels_block(r)
    if r.get("exhaustion", {}).get("reversing"):
        lines += ["", f"AGOTAMIENTO {r['exhaustion']['score']}/100 — posible giro {r['exhaustion']['direction']}:"]
        lines += [f"  * {s}" for s in r["exhaustion"]["signals"]]
    lines += ["", r.get("updated_at", "")]
    return "\n".join(lines)


def format_level_message(r: dict, kind: str, level: dict) -> str:
    """Aviso de precio llegando a un nivel FUERTE (soporte o resistencia)."""
    name = r.get("ticker") or r["name"]
    what = "SOPORTE" if kind == "S" else "RESISTENCIA"
    lines = [f"=== {name} EN {what} FUERTE ===",
             f"Precio: {_fp(r['price'])} | Nivel: {_fp(level['price'])} (fuerza {level['strength']}/100, {level['touches']} toques)",
             f"Origen: {', '.join(level['sources'][:3])}",
             f"Regimen: {r['regime']} (sesgo {r['bias']:+d}) | Timing 1H: {r.get('timing', {}).get('direction', '—')}",
             "", f"Lectura: {r['action']} — {r.get('action_detail', '')}",
             "", r.get("updated_at", "")]
    return "\n".join(lines)


def format_stocks_digest(results: list, summary: dict) -> str:
    lines = [f"=== ACCIONES / CEDEARs — {summary.get('headline', '')} ===", summary.get("detail", ""), ""]
    for r in results:
        if r.get("error"):
            lines.append(f"{r.get('ticker')}: error ({r['error'][:40]})")
            continue
        ced = r.get("cedear") or {}
        ced_txt = f" | CEDEAR ${ced['price_ars']:,.0f} CCL {ced['ccl_implicito']}" if ced.get("ccl_implicito") else ""
        sup = (r["levels"].get("nearest_support") or {}).get("price")
        res = (r["levels"].get("nearest_resistance") or {}).get("price")
        lines.append(f"{r['ticker']:<8} {_fp(r['price']):>10} {r['regime']:<12} {r['position']:<18} -> {r['action']}"
                     f"  [S {_fp(sup)} / R {_fp(res)}]{ced_txt}")
    return "\n".join(lines)
